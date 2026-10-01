"""Mock review panel: several reviewers read one document at once, each with a
different remit, and each grounded in the same evidence the document cites.

This is a rehearsal for a real panel, not an imitation of one. A reviewer is a
*rubric role* (methods, feasibility, fit to this call), never a claimed
identity: nothing here pretends to be a real funding-panel member, because that
standing would be invented and the whole product exists to avoid inventing
things. Each reviewer sees the document plus the evidence passages and is asked
to name unsupported claims against those passages.

Reviewers run concurrently. Ollama serves parallel slots for one loaded model,
measured at three simultaneous requests returning first tokens within ~2s of
each other, so a round costs roughly one reviewer's wall time rather than N.

The consolidated report is assembled deterministically -- no chair model, no
second LLM pass. A summary of reviews should not itself be a thing that can
hallucinate.
"""

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from grant_recommender import concepts
from grant_recommender.product_grounding import RULES
from grant_recommender.refinement import finish

ROOT = Path(__file__).resolve().parents[2] / 'data/private/reviews'
# A safety stop, not a budget (see concepts.NUM_PREDICT_BY_STAGE). The Linux
# default qwen3:4b-instruct-2507 wrote 701-846 tokens per review, too near 1200.
NUM_PREDICT = 2048
# verdict_reason is written last, so a tight cap closes it mid-argument: at 400
# characters gemma4:12b-mlx was cut off in 8 of 9 reviews, and its finished
# reasons ran 455-533. qwen3:4b-instruct-2507 writes to any cap (1200 on 3 of 3),
# so run_round keeps its last full sentence.
REASON_CHARS = 1200

# Three remits, chosen because they are the axes a panel actually splits along
# and because they need no invented authority to be useful.
ROLES = {
	'methods': {
		'label': 'Methodology & evaluation',
		'remit': (
			'Judge whether the proposed work could answer its own question. Look at design, '
			'measurement, comparison conditions, analysis and what would count as a negative '
			'result. Flag claims stated as established that are only proposed.'
		),
	},
	'feasibility': {
		'label': 'Feasibility, resources & risk',
		'remit': (
			'Judge whether this is deliverable by this applicant. Look at required equipment, '
			'data access, collaborators, populations and effort. Treat anything not present in '
			'the researcher evidence as a gap to be secured, never as an existing asset.'
		),
	},
	'fit': {
		'label': 'Fit to this call',
		'remit': (
			'Judge the document against the call passages only. Look at scope, expected '
			'outcomes and stated conditions. Say plainly where the work is adjacent to the '
			'call rather than responsive to it.'
		),
	},
}

REVIEW_SCHEMA = {
	'type': 'object',
	'properties': {
		'strengths': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 320},
		},
		'weaknesses': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 320},
		},
		'unsupported_claims': {
			'type': 'array',
			'maxItems': 3,
			'items': {
				'type': 'object',
				'properties': {
					'claim': {'type': 'string', 'maxLength': 320},
					'why_unsupported': {'type': 'string', 'maxLength': 400},
				},
				'required': ['claim', 'why_unsupported'],
				'additionalProperties': False,
			},
		},
		'questions_for_applicant': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 320},
		},
		# Short enum rather than a numeric score: a 12B under strict constrained
		# decoding picks a label far more reliably than it calibrates a number,
		# and a fake 7.5/10 would imply precision nobody has.
		'verdict': {'type': 'string', 'enum': ['fund', 'revise', 'reject']},
		'verdict_reason': {'type': 'string', 'maxLength': REASON_CHARS},
	},
	'required': [
		'strengths',
		'weaknesses',
		'unsupported_claims',
		'questions_for_applicant',
		'verdict',
		'verdict_reason',
	],
	'additionalProperties': False,
}

CONCEPT_FIELDS = [
	('problem', 'Problem'),
	('central_hypothesis', 'Central hypothesis'),
	('call_fit', 'Claimed fit to the call'),
	('researcher_fit', 'Claimed fit to the researcher'),
	('objectives', 'Objectives'),
	('required_assets_and_collaborators', 'Required assets and collaborators'),
	('novelty', 'Novelty claim'),
	('principal_feasibility_risk', 'Principal feasibility risk'),
]


def from_concept(job: dict, concept_id: str, *, variant: str = 'final') -> dict:
	"""Adapt a succeeded concept run into the reviewable document shape.

	Concepts are used rather than proposal drafts because a draft is scaffolded
	with four empty sections until a human writes them, and a panel reviewing
	empty sections would be theatre. The same document shape accepts a filled
	draft later.
	"""
	if job.get('status') != 'succeeded':
		raise ValueError('Review a completed concept run.')
	items = job['concepts'] if variant == 'final' else (job.get('draft') or {}).get('concepts')
	concept = next((c for c in items or [] if c['concept_id'] == concept_id), None)
	if not concept:
		raise ValueError(f'Unknown concept {concept_id!r} in run {job["id"]}.')

	sections = {}
	for key, label in CONCEPT_FIELDS:
		value = concept.get(key)
		if isinstance(value, list):
			value = '\n'.join(f'- {item}' for item in value)
		if value:
			sections[label] = value
	cited = [
		item['source_id']
		for item in [*concept.get('call_evidence', []), *concept.get('researcher_evidence', [])]
	]
	return {
		'title': concept['title'],
		'origin': f'concept {concept_id} of run {job["id"]}',
		'topic_code': job['topic_code'],
		'trial': bool(job.get('trial')),
		'sections': sections,
		'evidence': job.get('evidence') or {},
		'cited_source_ids': cited,
	}


def _packet(document: dict, role_key: str) -> dict:
	role = ROLES[role_key]
	evidence = '\n\n'.join(
		f'[{source_id}] {item["section"]}\n{item["text"]}'
		for source_id, item in document['evidence'].items()
	)
	body = '\n\n'.join(f'## {name}\n{text}' for name, text in document['sections'].items())
	trial_note = (
		'This document came from an exploratory trial: applicant eligibility was NOT '
		'established. Do not treat eligibility as settled.'
		if document.get('trial')
		else ''
	)
	return {
		'system_prompt': '\n\n'.join(
			filter(
				None,
				[
					RULES,
					(
						f'You are reviewing one funding document under a single remit: '
						f'{role["label"]}. {role["remit"]}'
					),
					(
						'You are a rubric role, not a person: never claim to be a panel '
						'member, programme officer or named expert, and never invent funder '
						'rules, page limits, budgets or scores.'
					),
					(
						'Judge only against the supplied evidence passages. A claim with no '
						'supporting passage is unsupported even if it is plausible. Quote '
						'the claim you mean. Be specific and brief; a reviewer who says '
						'"more detail is needed" without saying which detail is useless.'
					),
					(
						'Return JSON with strengths, weaknesses, unsupported_claims '
						'[{claim, why_unsupported}], questions_for_applicant, verdict '
						'(fund | revise | reject) and verdict_reason.'
					),
				],
			)
		),
		'user_prompt': '\n\n'.join(
			filter(
				None,
				[
					f'CALL: {document["topic_code"]}',
					trial_note,
					f'DOCUMENT: {document["title"]}',
					body,
					'EVIDENCE PASSAGES (the only admissible support):\n' + evidence,
				],
			)
		),
	}


def _write(round_id: str, name: str, payload: dict) -> None:
	target = ROOT / round_id
	target.mkdir(parents=True, exist_ok=True)
	path = target / f'{name}.json'
	temp = path.with_suffix('.tmp')
	temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
	temp.replace(path)


def consolidate(reviews: dict) -> dict:
	"""Deterministic merge. No model call: a summary of reviews must not be able
	to invent a criticism nobody made."""
	done = {key: r for key, r in reviews.items() if r.get('status') == 'succeeded'}
	verdicts = {key: r['result']['verdict'] for key, r in done.items()}
	tally = {v: sum(1 for x in verdicts.values() if x == v) for v in ('fund', 'revise', 'reject')}
	# The worst verdict carries: a panel does not average away a reject.
	outcome = next((v for v in ('reject', 'revise', 'fund') if tally[v]), None)
	unsupported = [
		{**claim, 'raised_by': key}
		for key, r in done.items()
		for claim in r['result']['unsupported_claims']
	]
	return {
		'verdicts': verdicts,
		'tally': tally,
		'outcome': outcome,
		'unsupported_claims': unsupported,
		'questions': [
			{'role': key, 'question': q}
			for key, r in done.items()
			for q in r['result']['questions_for_applicant']
		],
		'weaknesses': [
			{'role': key, 'weakness': w}
			for key, r in done.items()
			for w in r['result']['weaknesses']
		],
		'failed_roles': [key for key, r in reviews.items() if r.get('status') == 'failed'],
	}


def run_round(
	document: dict,
	*,
	roles: list[str] | None = None,
	on_text=None,
	on_done=None,
	round_id: str | None = None,
) -> dict:
	"""Run every reviewer concurrently and return the finished round.

	`on_text(role, text)` streams each reviewer's output as it arrives;
	`on_done(role, record)` fires as each one lands, so a UI can fill panes
	independently instead of waiting for the slowest.
	"""
	roles = roles or list(ROLES)
	unknown = [r for r in roles if r not in ROLES]
	if unknown:
		raise ValueError(f'Unknown reviewer roles: {unknown}')
	if not document.get('sections'):
		raise ValueError('Nothing to review: the document has no sections with text.')

	round_id = round_id or uuid.uuid4().hex[:12]
	started = datetime.now(UTC).isoformat()
	reviews: dict[str, dict] = {
		key: {'role': key, 'label': ROLES[key]['label'], 'status': 'running'} for key in roles
	}
	lock = threading.Lock()

	def worker(key: str) -> None:
		packet = _packet(document, key)
		record = reviews[key]
		try:
			parsed, meta = concepts._ollama_chat(
				{**packet, '_connection': concepts.connection_settings()},
				schema=REVIEW_SCHEMA,
				num_predict=NUM_PREDICT,
				seed=42,
				on_text=(lambda text: on_text(key, text)) if on_text else None,
			)
			parsed['verdict_reason'] = finish(parsed.get('verdict_reason') or '', REASON_CHARS)
			with lock:
				record.update(status='succeeded', result=parsed, model=meta['request']['model'])
		except Exception as exc:  # noqa: BLE001 -- one reviewer failing is a partial panel
			with lock:
				record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
		_write(round_id, f'reviewer_{key}', record)
		if on_done:
			on_done(key, record)

	threads = [threading.Thread(target=worker, args=(key,), daemon=True) for key in roles]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()

	result = {
		'id': round_id,
		'document': document,
		'started_at': started,
		'finished_at': datetime.now(UTC).isoformat(),
		'reviews': reviews,
		'consolidated': consolidate(reviews),
	}
	_write(round_id, 'round', result)
	return result


def load(round_id: str) -> dict:
	return json.loads((ROOT / round_id / 'round.json').read_text())


def list_rounds() -> list[dict]:
	if not ROOT.exists():
		return []
	rounds = []
	for path in ROOT.glob('*/round.json'):
		try:
			data = json.loads(path.read_text())
		except (OSError, json.JSONDecodeError):
			continue
		# A round killed before it finished has no consolidated block and no
		# finished_at. Skipping it beats taking the caller down.
		try:
			rounds.append(
				{
					'id': data['id'],
					'title': data['document']['title'],
					'topic_code': data['document']['topic_code'],
					# Which concept of which run, as from_concept phrased it. The
					# lifecycle view parses it to mark a reviewed concept.
					'origin': data['document'].get('origin', ''),
					'finished_at': data['finished_at'],
					'outcome': data['consolidated']['outcome'],
				}
			)
		except (KeyError, TypeError):
			continue
	return sorted(rounds, key=lambda r: r['finished_at'], reverse=True)
