"""Concept-generation job runner: an async, logged Ollama pipeline
grounded in a call's real evidence and the researcher's real portfolio.

The generation packet gives every confirmed portfolio claim its own evidence
id (PROFILE_001, PROFILE_002, ...), so a concept can cite, and the UI can link
to, the specific claim it relied on. The critique and revision builders, the
output schemas and the validator live in ideation.py. The grounding rules in
product_grounding.py wrap the draft, critique and revision prompts; trial context is retained
through all stages.

The five stages shown to the user map onto:
  1. generate            -- one Ollama call (draft).
  2. check_eligibility... -- entry requires an 'eligible' researcher review
                             unless the run is an exploratory trial; this stage
                             validates output and citation IDs, not semantic
                             entailment of each claim.
  3. critique             -- one Ollama call.
  4. revise               -- one Ollama call + validate_concept_set again.
  5. diversity_check      -- deterministic pairwise keyword overlap between
                             the three final concepts. No LLM-based diversity
                             ranker exists, so this is labelled a check, not
                             a rank.

Single local user, same identity boundary as portfolio.py: one job store, no
auth. On any failure the job stops (status=failed, stage+error recorded) --
no silent auto-repair or auto-retry; the caller can start a fresh job with
retry_of=<job_id>, which advances the seed.

# Known limit: an in-memory index over per-job JSON snapshots, one daemon thread
# per job. Fine for one user; move to a queue/worker if concurrent multi-user
# load ever matters.
"""

import hashlib
import json
import os
import platform
import re
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import requests

from grant_recommender import archive_rag, eligibility, generation_settings
from grant_recommender.ideation import (
	CRITIQUE_SCHEMA,
	SYSTEM_PROMPT,
	build_critique_packet,
	build_revision_packet,
	concept_set_schema,
	split_call_evidence,
	validate_concept_set,
)
from grant_recommender.portfolio import researcher_evidence as profile_evidence
from grant_recommender.portfolio import usable
from grant_recommender.product_grounding import VERSION as GROUNDING_POLICY_VERSION
from grant_recommender.product_grounding import (
	advisory_critique,
	apply_grounding_rules,
	preserve_intent,
	safe_revision_critique,
)
from grant_recommender.retrieval import keyword_overlap_scores

PIPELINE = 'grantbridge-concept-studio-v1'
STAGE_NAMES = [
	'generate',
	'check_eligibility_and_grounding',
	'critique',
	'revise',
	'diversity_check',
]

OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
# MLX builds run only on Apple silicon; elsewhere, a GGUF build of a small instruct model.
OLLAMA_MODEL = os.environ.get(
	'OLLAMA_MODEL',
	'gemma4:12b-mlx'
	if sys.platform == 'darwin' and platform.machine() == 'arm64'
	else 'qwen3:4b-instruct-2507-q4_K_M',
)
STAGE_TIMEOUT_S = 10 * 60
DECODING_PARAMETERS = {'temperature': 0.7, 'top_p': 0.8, 'top_k': 20, 'repeat_penalty': 1.0}
CONTEXT_WINDOW = 32768
# How much of the in-flight model output the UI shows. A tail, not a log: the
# full text is the stage result once it parses.
LIVE_TAIL_CHARS = 6000
# Safety stops, not budgets: a model that finishes early never reaches them, so
# raising one changes no run that already fit. Measured on the bundled example,
# gemma4:12b-mlx writes ~1500/650/1550 tokens; the Linux default
# qwen3:4b-instruct-2507 wrote 2378-3341 for a draft and 2339 for a revision,
# so the old 2432 cut every other draft off mid-JSON and failed the run.
NUM_PREDICT_BY_STAGE = {'draft': 6144, 'critique': 2048, 'final': 6144}
BASE_SEED = 42

JOBS_DIR = Path(__file__).resolve().parent.parent.parent / 'data' / 'private' / 'concept_jobs'
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _format_evidence(evidence: list[dict[str, str]]) -> str:
	return '\n\n'.join(
		f'[{item["source_id"]}] {item["section"]}\n{item["text"]}' for item in evidence
	)


def _output_contract(call_ids: list[str], researcher_ids: list[str]) -> str:
	return f"""Produce exactly 3 meaningfully different concepts. Each concept must put at \
least one supplied call citation in call_evidence and at least one supplied researcher \
citation in researcher_evidence. Use this exact JSON shape:
{{
  "concepts": [
    {{
      "concept_id": "1",
      "title": "...",
      "problem": "...",
      "central_hypothesis": "...",
      "call_fit": "...",
      "researcher_fit": "...",
      "objectives": ["...", "..."],
      "required_assets_and_collaborators": ["..."],
      "novelty": "...",
      "principal_feasibility_risk": "...",
      "call_evidence": [{{"source_id": "{call_ids[0]}", "claim_supported": "..."}}],
      "researcher_evidence": [{{"source_id": "{researcher_ids[0]}", "claim_supported": "..."}}]
    }}
  ]
}}
Use two or three objectives per concept. Required but unproven assets or collaborators must \
be worded as needs, not existing facts. Favor substantive diversity over cosmetic rewording."""


def build_generation_packet(
	*,
	job_id: str,
	call: dict,
	portfolio: list[dict],
	trial: bool = False,
	archive_passages: list[dict] | None = None,
	research_question: str = '',
) -> dict:
	"""One evidence id per call passage and per portfolio assertion -- lets a
	concept (and the UI) cite the specific claim it relied on, rather than one
	blob for the whole profile."""
	call_evidence = split_call_evidence(call['scope_text'])
	if call.get('topic_conditions'):
		call_evidence.append(
			{
				'source_id': 'CALL_CONDITIONS',
				'section': 'topic_conditions',
				'text': call['topic_conditions'],
			}
		)
	# "missing" confidence records the absence of a fact -- it must never be
	# cited as supporting evidence for a concept.
	included = [a for a in portfolio if usable(a)]
	if not included:
		raise ValueError(
			'No usable portfolio assertions. GrantBridge will not develop concepts from '
			'capabilities you have not confirmed. Confirm at least one capability in your '
			'researcher profile, then start this run again.'
		)
	researcher_evidence = profile_evidence(portfolio)
	archive_evidence = archive_rag.evidence(archive_passages or [])

	call_ids = [item['source_id'] for item in call_evidence]
	researcher_ids = [item['source_id'] for item in researcher_evidence]
	evidence_context = '\n\n'.join(
		[
			f'CALL: {call["topic_code"]} -- {call.get("title") or ""}',
			'CALL IDENTITY AND RECORDED WINDOW (metadata, not proof of eligibility):\n'
			+ json.dumps(
				{
					key: call[key]
					for key in (
						'provider',
						'call_identifier',
						'call_title',
						'types_of_action',
						'deadlines',
						'source_url',
					)
					if call.get(key)
				},
				ensure_ascii=False,
			),
			(
				'EXPLORATORY TRIAL: applicant eligibility is NOT established. Do not claim '
				'eligibility, secured resources, or readiness to apply. Recorded past '
				'deadlines remain past; this is not a new submission opportunity.'
				if trial
				else 'Ground every claim in the supplied evidence; do not infer eligibility from fit.'
			),
			'CALL EVIDENCE:\n' + _format_evidence(call_evidence),
			'RESEARCHER EVIDENCE:\n' + _format_evidence(researcher_evidence),
		]
	)
	user_prompt = '\n\n'.join(
		[
			evidence_context,
			'Draft candidates for later critique and revision.',
			_output_contract(call_ids, researcher_ids),
		]
	)
	if archive_evidence:
		archive_context = (
			'RESEARCH QUESTION (user request):\n'
			+ research_question
			+ '\n\nHISTORICAL ARCHIVE INSPIRATION, NOT PERSONAL EVIDENCE:\n'
			+ _format_evidence(archive_evidence)
			+ '\n\n'
			+ archive_rag.RULES
		)
		evidence_context += '\n\n' + archive_context
		user_prompt += '\n\n' + archive_context
	return apply_grounding_rules(
		{
			'pipeline': PIPELINE,
			'job_id': job_id,
			'stage': 'draft',
			'system_prompt': SYSTEM_PROMPT,
			'evidence_context': evidence_context,
			'user_prompt': user_prompt,
			'allowed_evidence_ids': call_ids
			+ researcher_ids
			+ [e['source_id'] for e in archive_evidence],
			'require_researcher_evidence': True,
			'output_schema_version': 'concept-set-v2',
			# extra key, only read here -- build_critique_packet/build_revision_packet
			# only read their own named keys, so this rides along unused by them.
			'evidence': call_evidence + researcher_evidence + archive_evidence,
		}
	)


def diversity_check(concepts: list[dict]) -> dict[str, dict[str, float]]:
	"""Real, deterministic pairwise keyword overlap between concepts' title +
	hypothesis -- a diversity *check*, not a ranking: no LLM-based diversity
	ranker exists here, and inventing one would fabricate a signal."""
	docs = {c['concept_id']: f'{c["title"]} {c["central_hypothesis"]}' for c in concepts}
	return {
		concept_id: {
			k: round(v, 4)
			for k, v in keyword_overlap_scores(
				text, {o: docs[o] for o in docs if o != concept_id}
			).items()
		}
		for concept_id, text in docs.items()
	}


def _request_hash(body: dict) -> str:
	payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(payload.encode()).hexdigest()


def _ollama_chat(
	packet: dict, *, schema: dict, num_predict: int, seed: int, on_text=None
) -> tuple[dict, dict]:
	"""Run one schema-constrained turn.

	With `on_text` the response is streamed and the callback is handed the text
	accumulated so far, so a caller can show the model working instead of a
	frozen spinner. The parsed result is identical either way: the schema is
	enforced by Ollama, and the concatenated stream is the same JSON document
	the non-streaming call would have returned in one piece.
	"""
	connection = packet.get('_connection') or connection_settings()
	body = {
		'model': connection['model'],
		'messages': [
			{'role': 'system', 'content': packet['system_prompt']},
			{'role': 'user', 'content': packet['user_prompt']},
		],
		'format': schema,
		'options': {
			**DECODING_PARAMETERS,
			'num_ctx': CONTEXT_WINDOW,
			'num_predict': num_predict,
			'seed': seed,
		},
		'stream': bool(on_text),
		'think': False,
		'keep_alive': '30m',
	}
	response = requests.post(
		f'{connection["base_url"]}/api/chat',
		json=body,
		timeout=STAGE_TIMEOUT_S,
		allow_redirects=False,
		stream=bool(on_text),
	)
	if 300 <= response.status_code < 400:
		raise requests.RequestException(
			'Model endpoint redirected; no content was forwarded. Check connection settings.'
		)
	response.raise_for_status()
	if not on_text:
		response_body = response.json()
		content = response_body['message']['content']
	else:
		pieces: list[str] = []
		response_body = {}
		for line in response.iter_lines():
			if not line:
				continue
			chunk = json.loads(line)
			if chunk.get('error'):
				raise requests.RequestException(str(chunk['error']))
			piece = (chunk.get('message') or {}).get('content') or ''
			if piece:
				pieces.append(piece)
				on_text(''.join(pieces))
			if chunk.get('done'):
				response_body = chunk
				break
		content = ''.join(pieces)
		# Keep the non-streaming response shape so logging and the request hash
		# stay comparable across both paths.
		response_body = {**response_body, 'message': {'role': 'assistant', 'content': content}}
	# strict=False: a model puts literal newlines and tabs inside JSON strings,
	# which the default parser rejects outright, and one such character in a
	# critique failed the whole concept job. This only ever turns a parse that used to
	# raise into one that succeeds; nothing that already parsed changes.
	parsed = json.loads(content, strict=False)
	return parsed, {'request': body, 'response': response_body}


def _model_digest(connection: dict) -> str | None:
	try:
		r = requests.get(f'{connection["base_url"]}/api/tags', timeout=10)
		r.raise_for_status()
		for m in r.json().get('models', []):
			if m.get('name') == connection['model']:
				return m.get('digest')
	except requests.RequestException:
		pass
	return None


def connection_settings() -> dict:
	return generation_settings.current(OLLAMA_BASE_URL, OLLAMA_MODEL)


def connection_status(connection: dict | None = None) -> dict:
	connection = connection or connection_settings()
	started = perf_counter()
	result = {
		**connection,
		'checked_at': datetime.now(UTC).isoformat(),
	}
	try:
		r = requests.get(f'{connection["base_url"]}/api/tags', timeout=5, allow_redirects=False)
		r.raise_for_status()
		models = [m['name'] for m in r.json().get('models', [])]
		if not all(isinstance(model, str) for model in models):
			raise ValueError('Invalid models response')
		ready = connection['model'] in models
		return {
			**result,
			'ready': ready,
			'models': models,
			'latency_ms': round((perf_counter() - started) * 1000),
			'detail': 'Connection successful. Model available.'
			if ready
			else f'Server reached, but model {connection["model"]} is not installed.',
		}
	except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
		if isinstance(exc, requests.Timeout):
			detail = 'Connection timed out. Check the server address, VPN or SSH tunnel.'
		elif isinstance(exc, requests.HTTPError):
			detail = f'Server returned HTTP {exc.response.status_code}. Check access and the Ollama base URL.'
		elif isinstance(exc, requests.ConnectionError):
			detail = (
				'Cannot reach this server. Check its address, Ollama service, VPN or SSH tunnel.'
			)
		else:
			detail = 'The server did not return a valid Ollama model list. Use its base URL, without /api.'
		return {
			**result,
			'ready': False,
			'models': [],
			'detail': detail,
		}


def _new_job_state(
	job_id: str, topic_code: str, seed_index: int, retry_of: str | None, evidence: list[dict]
) -> dict:
	return {
		'id': job_id,
		'topic_code': topic_code,
		'status': 'running',
		'created_at': datetime.now(UTC).isoformat(),
		'retry_of': retry_of,
		'seed_index': seed_index,
		'model': OLLAMA_MODEL,
		'model_digest': None,
		'stages': [
			{
				'name': name,
				'status': 'pending',
				'wall_time_seconds': None,
				'detail': None,
				'live': None,
				'started_at': None,
			}
			for name in STAGE_NAMES
		],
		'draft': None,
		'critique': None,
		'concepts': None,
		'diversity': None,
		'error': None,
		# source_id -> {section, text}, so a concept's evidence citation can be
		# resolved back to the actual call/portfolio passage it names -- the
		# reason build_generation_packet gives each assertion its own id.
		'evidence': {
			e['source_id']: {'section': e['section'], 'text': e['text']} for e in evidence
		},
	}


def _set_stage(
	job: dict,
	name: str,
	status: str,
	*,
	detail: str | None = None,
	wall_time_seconds: float | None = None,
) -> None:
	for stage in job['stages']:
		if stage['name'] == name:
			stage['status'] = status
			stage['detail'] = detail
			stage['wall_time_seconds'] = wall_time_seconds
			# Stamped by the server so a progress display measures the stage,
			# not how long the browser has had the panel open.
			if status == 'running':
				stage['started_at'] = datetime.now(UTC).isoformat()
			else:
				# The live tail is progress, not a result. Once the stage is
				# over the parsed output is the record, so drop it rather than
				# persist a half-written duplicate of it.
				stage['live'] = None
			break
	_persist_job(job)


def _set_live(job: dict, name: str, text: str) -> None:
	"""In-memory only: the poll endpoint reads JOBS, so the UI sees this without
	a disk write per token. Deliberately not persisted -- it is transient
	progress, and snapshotting on every chunk would hammer the filesystem."""
	for stage in job['stages']:
		if stage['name'] == name:
			stage['live'] = text[-LIVE_TAIL_CHARS:]
			break


def _persist_job(job: dict) -> None:
	"""Durable snapshot of the full job dict -- distinct from _log's raw
	per-stage request/response rows. Lets Activity survive an API restart
	instead of a job's history vanishing along with the in-memory JOBS dict."""
	JOBS_DIR.mkdir(parents=True, exist_ok=True)
	target = JOBS_DIR / f'{job["id"]}.json'
	temp = target.with_suffix('.tmp')
	temp.write_text(json.dumps(job, ensure_ascii=False, default=str))
	temp.replace(target)


def _load_persisted_jobs(*, mark_interrupted: bool = True) -> None:
	"""Reload durable job snapshots at import time. A job that was 'running'
	when the process last stopped has no worker running it any more -- that's
	a real, distinct state ('interrupted'), not the same as still running."""
	if not JOBS_DIR.exists():
		return
	for path in sorted(JOBS_DIR.glob('*.json')):
		# Skipping a bad file, like every other reader of this directory. This
		# one runs at import, so raising here took down the panel, the API, the
		# terminal app and the watch loop rather than losing one run.
		try:
			job = json.loads(path.read_text())
		except (OSError, json.JSONDecodeError):
			continue
		if not isinstance(job, dict) or not job.get('id'):
			continue
		if mark_interrupted and job.get('status') == 'running':
			job['status'] = 'interrupted'
			_persist_job(job)
		JOBS[job['id']] = job


def _log(job_id: str, row: dict) -> None:
	JOBS_DIR.mkdir(parents=True, exist_ok=True)
	with (JOBS_DIR / f'{job_id}.jsonl').open('a') as f:
		f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')


def _fail(
	job: dict,
	job_id: str,
	stage: str,
	log_stage: str,
	error: str,
	wall_time_seconds: float | None = None,
) -> None:
	_set_stage(job, stage, 'failed', detail=error, wall_time_seconds=wall_time_seconds)
	_log(job_id, {'stage': log_stage, 'status': 'failed', 'error': error})
	completed = {**job, 'status': 'failed', 'error': error}
	_persist_job(completed)
	job.update(completed)


def _run_stage_call(
	job: dict,
	job_id: str,
	stage: str,
	log_stage: str,
	packet: dict,
	*,
	schema: dict,
	num_predict: int,
	seed: int,
) -> dict | None:
	_set_stage(job, stage, 'running')
	started = perf_counter()
	try:
		parsed, meta = _ollama_chat(
			{**packet, '_connection': job['connection']},
			schema=schema,
			num_predict=num_predict,
			seed=seed,
			on_text=lambda text: _set_live(job, stage, text),
		)
	except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
		_fail(
			job, job_id, stage, log_stage, f'{type(exc).__name__}: {exc}', perf_counter() - started
		)
		return None
	wall = perf_counter() - started
	_log(
		job_id,
		{
			'stage': log_stage,
			'status': 'success',
			'wall_time_seconds': wall,
			'seed': seed,
			'model': job['model'],
			'request_sha256': _request_hash(meta['request']),
			**meta,
		},
	)
	_set_stage(job, stage, 'succeeded', wall_time_seconds=wall)
	return parsed


def _run_job(job_id: str, packet: dict) -> None:
	"""Last-resort guard: any exception _run_job_body doesn't already turn
	into a failed stage (e.g. Ollama returning a null/malformed content
	field) must still resolve the job -- otherwise it stays "running"
	forever and the UI polls it forever, which is worse than an honest
	failure."""
	job = JOBS[job_id]
	try:
		_run_job_body(job, job_id, packet)
	except Exception as exc:  # noqa: BLE001 -- deliberate last-resort guard, see docstring
		stage = next(
			(s['name'] for s in job['stages'] if s['status'] in ('pending', 'running')), 'generate'
		)
		_fail(job, job_id, stage, 'unexpected', f'{type(exc).__name__}: {exc}')


def _validate_product_concepts(payload: dict, **kwargs) -> None:
	validate_concept_set(payload, **kwargs)
	archive_ids = {i for i in kwargs['allowed_evidence_ids'] if i.startswith('ARCHIVE_')}
	for concept in payload['concepts']:
		citations = concept.get('archive_evidence', [])
		if archive_ids and not citations:
			raise ValueError('Each archive-informed concept must cite its historical inspiration.')
		if not isinstance(citations, list) or any(
			not isinstance(c, dict)
			or set(c) != {'source_id', 'claim_supported'}
			or c.get('source_id') not in archive_ids
			or not isinstance(c.get('claim_supported'), str)
			or not c['claim_supported'].strip()
			for c in citations
		):
			raise ValueError('Invalid archive citation. Use only supplied archive passage IDs.')
	seen = set()
	for concept in payload['concepts']:
		for key in (
			'concept_id',
			'title',
			'problem',
			'central_hypothesis',
			'call_fit',
			'researcher_fit',
			'novelty',
			'principal_feasibility_risk',
		):
			if not isinstance(concept[key], str) or not concept[key].strip():
				raise ValueError(f'{key} must be a nonempty string')
		if concept['concept_id'] in seen:
			raise ValueError('Concept identifiers must be distinct')
		seen.add(concept['concept_id'])
		for key in ('objectives', 'required_assets_and_collaborators'):
			if not isinstance(concept[key], list) or any(
				not isinstance(v, str) or not v.strip() for v in concept[key]
			):
				raise ValueError(f'{key} must contain strings')


def _run_job_body(job: dict, job_id: str, packet: dict) -> None:
	job['model_digest'] = _model_digest(job['connection'])
	seed = BASE_SEED + job['seed_index']
	schema = concept_set_schema(
		allowed_evidence_ids=packet['allowed_evidence_ids'],
		require_researcher_evidence=packet['require_researcher_evidence'],
	)
	archive_ids = [i for i in packet['allowed_evidence_ids'] if i.startswith('ARCHIVE_')]
	if archive_ids:
		item = schema['properties']['concepts']['items']
		item['properties']['archive_evidence'] = {
			'type': 'array',
			'minItems': 1,
			'items': {
				'type': 'object',
				'properties': {
					'source_id': {'type': 'string', 'enum': archive_ids},
					'claim_supported': {'type': 'string'},
				},
				'required': ['source_id', 'claim_supported'],
				'additionalProperties': False,
			},
		}
		item['required'].append('archive_evidence')

	draft = _run_stage_call(
		job,
		job_id,
		'generate',
		'draft',
		packet,
		schema=schema,
		num_predict=NUM_PREDICT_BY_STAGE['draft'],
		seed=seed,
	)
	if draft is None:
		return
	job['draft'] = draft

	_set_stage(job, 'check_eligibility_and_grounding', 'running')
	try:
		_validate_product_concepts(
			draft,
			allowed_evidence_ids=packet['allowed_evidence_ids'],
			require_researcher_evidence=True,
		)
	except (ValueError, TypeError) as exc:
		_fail(job, job_id, 'check_eligibility_and_grounding', 'draft_validation', str(exc))
		return
	_set_stage(
		job,
		'check_eligibility_and_grounding',
		'succeeded',
		detail=(
			(
				'Exploratory trial: eligibility is NOT established. '
				if job.get('trial')
				else 'Eligibility reviewed for the input snapshot. '
			)
			+ 'Citation identifiers and output '
			'structure validated; citation validity alone does not prove a claim is supported.'
		),
	)

	critique_packet = advisory_critique(apply_grounding_rules(build_critique_packet(packet, draft)))
	if archive_ids:
		critique_packet['system_prompt'] += '\n\n' + archive_rag.RULES
	critique = _run_stage_call(
		job,
		job_id,
		'critique',
		'critique',
		critique_packet,
		schema=CRITIQUE_SCHEMA,
		num_predict=NUM_PREDICT_BY_STAGE['critique'],
		seed=seed,
	)
	if critique is None:
		return
	try:
		rows = critique['concept_critiques']
		if {r['concept_id'] for r in rows} != {c['concept_id'] for c in draft['concepts']} or len(
			rows
		) != 3:
			raise ValueError('Critique must address each of the three concepts exactly once')
		for row in rows:
			for field in ('grounding_and_fit', 'novelty_and_feasibility', 'revision_action'):
				if not isinstance(row[field], str):
					raise TypeError(f'Critique {field} must be text')
		for field in (
			'unsupported_claims',
			'missing_requirements',
			'diversity_problems',
			'revision_priorities',
		):
			if not isinstance(critique[field], list) or any(
				not isinstance(v, str) for v in critique[field]
			):
				raise ValueError(f'Critique {field} must be a list of text')
	except (ValueError, KeyError, TypeError) as exc:
		_fail(job, job_id, 'critique', 'critique_validation', str(exc))
		return
	job['critique'] = critique

	revision_packet = apply_grounding_rules(
		build_revision_packet(packet, draft, safe_revision_critique())
	)
	if archive_ids:
		revision_packet['system_prompt'] += '\n\n' + archive_rag.RULES
		revision_packet['user_prompt'] += (
			'\n\nInclude the additional archive_evidence field per concept as specified in ARCHIVE INSPIRATION RULES.'
		)
	final = _run_stage_call(
		job,
		job_id,
		'revise',
		'final',
		revision_packet,
		schema=schema,
		num_predict=NUM_PREDICT_BY_STAGE['final'],
		seed=seed,
	)
	if final is None:
		return
	try:
		final, job['revision_audit'] = preserve_intent(draft, final)
		_validate_product_concepts(
			final,
			allowed_evidence_ids=packet['allowed_evidence_ids'],
			require_researcher_evidence=True,
		)
	except (ValueError, TypeError) as exc:
		_fail(job, job_id, 'revise', 'final_validation', str(exc))
		return
	job['concepts'] = final['concepts']

	_set_stage(job, 'diversity_check', 'running')
	job['diversity'] = diversity_check(final['concepts'])
	_set_stage(
		job,
		'diversity_check',
		'succeeded',
		detail='Pairwise keyword overlap between the three final concepts -- a diversity check, not a ranking.',
	)
	# Publish the terminal state only after its durable snapshot exists.
	completed = {**job, 'status': 'succeeded'}
	_persist_job(completed)
	job.update(completed)


def start_job(
	call: dict,
	portfolio: list[dict],
	retry_of: str | None = None,
	*,
	trial: bool = False,
	archive_context: dict | None = None,
) -> dict:
	"""Raises ValueError (caller should surface as 400) if the portfolio has
	no usable evidence -- no job is created for an input error."""
	assessment = eligibility.assess(call, portfolio)
	if assessment['state'] != 'eligible' and not trial:
		raise ValueError(assessment['reason'])
	if retry_of and (retry_of not in JOBS or JOBS[retry_of]['topic_code'] != call['topic_code']):
		raise ValueError('Retry must refer to an existing run for this call.')
	if retry_of and JOBS[retry_of].get('archive_context'):
		raise ValueError(
			'Archive-informed retries require a fresh passage preview in Past applications.'
		)
	job_id = uuid.uuid4().hex[:12]
	prior = [j for j in JOBS.values() if j['topic_code'] == call['topic_code']]
	seed_index = JOBS[retry_of]['seed_index'] + 1 if retry_of in JOBS else len(prior)
	packet = build_generation_packet(
		job_id=job_id,
		call=call,
		portfolio=portfolio,
		trial=trial,
		archive_passages=archive_context['passages'] if archive_context else None,
		research_question=archive_context['query'] if archive_context else '',
	)
	if archive_context:
		packet['system_prompt'] += '\n\n' + archive_rag.RULES
	job = _new_job_state(job_id, call['topic_code'], seed_index, retry_of, packet['evidence'])
	job['grounding_policy_version'] = GROUNDING_POLICY_VERSION
	job['trial'] = trial
	job['connection'] = archive_context['connection'] if archive_context else connection_settings()
	if archive_context:
		job['archive_context'] = {
			k: archive_context[k] for k in ('id', 'query', 'passages', 'connection', 'created_at')
		}
	job['model'] = job['connection']['model']
	job['eligibility_snapshot'] = assessment
	with _LOCK:
		JOBS[job_id] = job
	_persist_job(job)
	threading.Thread(target=_run_job, args=(job_id, packet), daemon=True).start()
	return job


def get_job(job_id: str) -> dict | None:
	return JOBS.get(job_id)


def load_job(job_id: str) -> dict | None:
	"""A job from memory, or from its file if this process never saw it start.

	JOBS is filled once at import, so a run the watch loop started afterwards
	is on disk but not in memory. The terminal app could list and review such a
	concept but not open a draft from it, because proposals.create asked
	get_job. Kept separate so get_job stays memory-only for the callers that
	poll a live run (api.py, discovery_panel.py).
	"""
	# Through get_job, not JOBS, so this stays the one seam a caller can patch.
	job = get_job(job_id)
	if job is not None:
		return job
	# This id now becomes a path segment, and it arrives from a request body.
	# Job ids are uuid4().hex[:12]; anything else never gets near the disk.
	if not re.fullmatch(r'[a-f0-9]{12}', job_id or ''):
		return None
	path = JOBS_DIR / f'{job_id}.json'
	try:
		return json.loads(path.read_text())
	except (OSError, json.JSONDecodeError):
		return None


def list_jobs(topic_code: str | None = None) -> list[dict]:
	# Oldest first. JOBS is filled by globbing the store, so its own order is by
	# job id -- callers that take the last entry as "the newest run" got an
	# arbitrary one, usually an interrupted leftover.
	jobs = sorted(JOBS.values(), key=lambda j: j['created_at'])
	return [j for j in jobs if j['topic_code'] == topic_code] if topic_code else jobs


_load_persisted_jobs(mark_interrupted=False)
