"""Local proposal workspace. Immutable source snapshots, explicit model consent.

Generic working sections, NOT a funder's official application template. Model
suggestions never overwrite an editor: accepting one requires its base revision.
Single-process/local-user storage, matching the existing product boundary.
"""

import json
import re
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from grant_recommender import archive_rag, concepts
from grant_recommender.product_grounding import RULES, VERSION

ROOT = Path(__file__).resolve().parents[2] / 'data/private/proposals'
LOCK = threading.RLock()
SECTIONS = {
	'aims': 'Research question & aims',
	'methods': 'Methods & evaluation',
	'work_packages': 'Work packages & milestones',
	'risks': 'Risks, ethics & dependencies',
	'impact': 'Expected contribution & impact',
}
NOTICE = (
	"A working draft, not the funder's template. Check the call for the headings and page "
	'limits it actually wants. The sources here were copied when the concept was generated '
	'and may have moved on.'
)


def _now() -> str:
	return datetime.now(UTC).isoformat()


def _path(kind: str, identifier: str) -> Path:
	if not re.fullmatch(r'[a-f0-9]{32}', identifier):
		raise ValueError('Unknown proposal or preview.')
	return ROOT / kind / f'{identifier}.json'


def _read(kind: str, identifier: str) -> dict:
	try:
		return json.loads(_path(kind, identifier).read_text())
	except FileNotFoundError:
		raise ValueError('Proposal or preview not found.') from None


def _write(kind: str, record: dict) -> None:
	path = _path(kind, record['id'])
	path.parent.mkdir(parents=True, exist_ok=True)
	temp = path.with_suffix('.tmp')
	temp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
	temp.replace(path)


def get(identifier: str) -> dict:
	with LOCK:
		return _read('drafts', identifier)


def list_drafts(job_id: str) -> list[dict]:
	with LOCK:
		items = [json.loads(p.read_text()) for p in (ROOT / 'drafts').glob('*.json')]
	return sorted([d for d in items if d['job_id'] == job_id], key=lambda d: d['created_at'])


def create(job_id: str, concept_id: str, variant: str) -> dict:
	if variant not in ('original', 'revised'):
		raise ValueError('Choose the original or revised concept.')
	job = concepts.load_job(job_id)
	if not job or job['status'] != 'succeeded':
		raise ValueError('Choose a completed concept run.')
	items = job['draft']['concepts'] if variant == 'original' else job['concepts']
	concept = next((c for c in items if c['concept_id'] == concept_id), None)
	if not concept:
		raise ValueError('Unknown concept.')
	with LOCK:
		# An accidental double-click reopens the existing workspace.
		for prior in list_drafts(job_id):
			if prior['concept_id'] == concept_id and prior['variant'] == variant:
				return prior
		sections = {
			key: {'text': '', 'citations': [], 'missing_info': [], 'origin': 'outline'}
			for key in SECTIONS
		}
		sections['aims']['text'] = '\n\n'.join(
			[
				concept['problem'],
				'Hypothesis to test: ' + concept['central_hypothesis'],
				*['- ' + aim for aim in concept['objectives']],
			]
		)
		sections['aims']['origin'] = 'concept (unverified AI text)'
		# The aims are the concept's own text, so they keep the concept's sources. An
		# empty claim is dropped: _validate_content would refuse every later save of it.
		cited = concept.get('call_evidence', []) + concept.get('researcher_evidence', [])
		sections['aims']['citations'] = [deepcopy(c) for c in cited if c['claim_supported'].strip()]
		sections['aims']['missing_info'] = [
			'Confirm the research question, aims and supporting literature.'
		]
		sections['methods']['missing_info'] = [
			'Specify design, measurements, sample size rationale and analysis.'
		]
		sections['work_packages']['missing_info'] = [
			'Confirm duration, people, effort, dependencies and milestones.'
		]
		sections['risks']['missing_info'] = [
			concept['principal_feasibility_risk'],
			*concept['required_assets_and_collaborators'],
		]
		sections['impact']['missing_info'] = [
			'Verify the novelty claim and distinguish anticipated effects from demonstrated results.'
		]
		draft = {
			'id': uuid.uuid4().hex,
			'job_id': job_id,
			'concept_id': concept_id,
			'variant': variant,
			'title': concept['title'],
			'topic_code': job['topic_code'],
			'created_at': _now(),
			'updated_at': _now(),
			'revision': 1,
			'trial': bool(job.get('trial')),
			'notice': NOTICE,
			'sections': sections,
			'concept': deepcopy(concept),
			'evidence': deepcopy(job['evidence']),
			'archive_context': deepcopy(job.get('archive_context')),
			'research_question': (job.get('archive_context') or {}).get('query', ''),
			'revision_audit': deepcopy(job.get('revision_audit')),
		}
		_write('drafts', draft)
		return draft


def _revision(draft: dict, revision: int) -> None:
	if draft['revision'] != revision:
		raise ValueError(
			'This draft changed. Reopen the saved draft before editing or generating again.'
		)


def _section(section: str) -> None:
	if section not in SECTIONS:
		raise ValueError('Unknown draft section.')


def _validate_content(content: dict, evidence: dict) -> None:
	if not isinstance(content, dict) or set(content) != {'text', 'citations', 'missing_info'}:
		raise ValueError('Invalid section response.')
	if not isinstance(content['text'], str) or len(content['text']) > 30000:
		raise ValueError('Section text must be at most 30,000 characters.')
	if (
		not isinstance(content['missing_info'], list)
		or len(content['missing_info']) > 40
		or any(
			not isinstance(item, str) or not item.strip() or len(item) > 2000
			for item in content['missing_info']
		)
	):
		raise ValueError('Missing information must be a list of short questions.')
	if not isinstance(content['citations'], list) or len(content['citations']) > 40:
		raise ValueError('Invalid section citations.')
	for citation in content['citations']:
		if (
			not isinstance(citation, dict)
			or set(citation) != {'source_id', 'claim_supported'}
			or citation['source_id'] not in evidence
			or not isinstance(citation['claim_supported'], str)
			or not citation['claim_supported'].strip()
			or len(citation['claim_supported']) > 2000
		):
			raise ValueError('Citations must name a supplied source and the claim it supports.')
	inline = set(re.findall(r'\[((?:CALL|PROFILE|ARCHIVE)_[A-Za-z0-9_-]+)\]', content['text']))
	if not inline.issubset({c['source_id'] for c in content['citations']}):
		raise ValueError('Every inline source reference must have a matching citation.')


def save(
	identifier: str, section: str, revision: int, content: dict, *, origin='edited by user'
) -> dict:
	_section(section)
	with LOCK:
		draft = get(identifier)
		_revision(draft, revision)
		_validate_content(content, draft['evidence'])
		# Preserve every prior version, including text replaced by an accepted suggestion.
		history = ROOT / 'history' / identifier
		history.mkdir(parents=True, exist_ok=True)
		(history / f'{draft["revision"]}.json').write_text(json.dumps(draft, ensure_ascii=False))
		draft['sections'][section] = {**deepcopy(content), 'origin': origin}
		draft.update(revision=revision + 1, updated_at=_now())
		_write('drafts', draft)
		return draft


def preview(
	identifier: str, section: str, revision: int, instructions: str, source_ids: list[str]
) -> dict:
	_section(section)
	with LOCK:
		draft = get(identifier)
		_revision(draft, revision)
		if (
			not source_ids
			or len(set(source_ids)) != len(source_ids)
			or any(i not in draft['evidence'] for i in source_ids)
		):
			raise ValueError('Select valid source passages for this section.')
		if not any(i.startswith('CALL_') for i in source_ids):
			raise ValueError('Include at least one call passage to retain funding context.')
		evidence = {i: draft['evidence'][i] for i in source_ids}
		context = {
			'section': SECTIONS[section],
			'topic_code': draft['topic_code'],
			'notice': NOTICE,
			'trial': draft['trial'],
			'research_question': draft['research_question'],
			'concept': draft['concept'],
			'current_section': draft['sections'][section],
			'instructions': instructions,
			'evidence': evidence,
		}
		packet = {
			'system_prompt': RULES + '\n\n' + archive_rag.RULES + '\n\n'
			'Draft only the requested proposal section. Preserve the supplied research question, '
			'hypothesis and aims; do not treat the concept as verified evidence. '
			'The outline is generic, not an official funder template. Never invent funding '
			'restrictions, results, references, sample sizes, access, partners or budgets. '
			'Use [TO CONFIRM: ...] placeholders and missing_info for unknowns. '
			'Frame methods and effects as proposed work and hypotheses, not completed results. '
			'Use [SOURCE_ID] inline citations with a matching citations entry. CALL sources '
			'support call claims only; PROFILE supports personal claims; ARCHIVE supports '
			'historical inspiration, not personal ownership or completed work. '
			'Return JSON with text, citations [{source_id, claim_supported}], missing_info [string].',
			'user_prompt': json.dumps(context, ensure_ascii=False, indent=2),
		}
		record = {
			'id': uuid.uuid4().hex,
			'draft_id': identifier,
			'section': section,
			'revision': revision,
			'created_at': _now(),
			'status': 'preview',
			'connection': concepts.connection_settings(),
			'source_ids': source_ids,
			'packet': packet,
			'grounding_policy_version': VERSION,
			'result': None,
			'error': None,
		}
		_write('previews', record)
		return record


def get_preview(identifier: str) -> dict:
	with LOCK:
		record = _read('previews', identifier)
		# Raw Ollama metadata is private audit data, not needed by the UI.
		return {k: v for k, v in record.items() if k != 'model_trace'}


def _schema(source_ids: list[str]) -> dict:
	return {
		'type': 'object',
		'additionalProperties': False,
		'required': ['text', 'citations', 'missing_info'],
		'properties': {
			'text': {'type': 'string'},
			'missing_info': {'type': 'array', 'items': {'type': 'string'}},
			'citations': {
				'type': 'array',
				'minItems': 1,
				'items': {
					'type': 'object',
					'additionalProperties': False,
					'required': ['source_id', 'claim_supported'],
					'properties': {
						'source_id': {'type': 'string', 'enum': source_ids},
						'claim_supported': {'type': 'string'},
					},
				},
			},
		},
	}


def _generate(identifier: str) -> None:
	with LOCK:
		record = _read('previews', identifier)
	try:
		result, trace = concepts._ollama_chat(
			{**record['packet'], '_connection': record['connection']},
			schema=_schema(record['source_ids']),
			num_predict=3000,
			seed=42,
		)
		record['model_trace'] = trace
		context = json.loads(record['packet']['user_prompt'])
		_validate_content(result, context['evidence'])
		if not result['text'].strip() or not result['citations']:
			raise ValueError('The model returned no draft text or source citations.')
		record.update(status='succeeded', result=result)
	except Exception as exc:  # noqa: BLE001 -- a failed model must not leave a permanent spinner
		record.update(
			status='failed',
			error=(
				str(exc)
				if isinstance(exc, ValueError)
				else 'Generation failed. Check the model connection, then make a new preview.'
			),
		)
	with LOCK:
		_write('previews', record)


def generate(identifier: str, approved: bool) -> dict:
	if approved is not True:
		raise ValueError('Explicit approval is required before sending any draft or sources.')
	with LOCK:
		record = _read('previews', identifier)
		if record['status'] != 'preview':
			return get_preview(identifier)  # idempotent; never resubmit the same approval
		_revision(get(record['draft_id']), record['revision'])
		if datetime.now(UTC) - datetime.fromisoformat(record['created_at']) > timedelta(minutes=30):
			raise ValueError('Preview expired. Preview the section again.')
		if record['connection'] != concepts.connection_settings():
			raise ValueError(
				'Model connection changed. Preview the new destination before sending.'
			)
		record.update(status='running', approved_at=_now())
		_write('previews', record)
		threading.Thread(target=_generate, args=(identifier,), daemon=True).start()
		return get_preview(identifier)


def accept(identifier: str) -> dict:
	with LOCK:
		record = _read('previews', identifier)
		if (
			record['status'] != 'succeeded'
			or record.get('accepted_at')
			or record.get('discarded_at')
		):
			raise ValueError('Only a new, successful suggestion can be accepted.')
		draft = save(
			record['draft_id'],
			record['section'],
			record['revision'],
			record['result'],
			origin='AI suggestion accepted by user',
		)
		record['accepted_at'] = _now()
		_write('previews', record)
		return draft


def discard(identifier: str) -> dict:
	# Only a finished suggestion: _generate holds its own copy of a running record
	# and would write it back without the mark.
	with LOCK:
		record = _read('previews', identifier)
		if (
			record['status'] != 'succeeded'
			or record.get('accepted_at')
			or record.get('discarded_at')
		):
			raise ValueError('Only a new, successful suggestion can be discarded.')
		record['discarded_at'] = _now()
		_write('previews', record)
		return get_preview(identifier)


def interrupt_pending() -> None:
	"""Called only by server lifespan startup, not module imports."""
	with LOCK:
		for path in (ROOT / 'previews').glob('*.json'):
			record = json.loads(path.read_text())
			if record['status'] == 'running':
				record.update(
					status='failed', error='Server restarted during generation. Make a new preview.'
				)
				_write('previews', record)


def latest_preview(draft_id: str) -> dict | None:
	with LOCK:
		get(draft_id)
		records = [json.loads(p.read_text()) for p in (ROOT / 'previews').glob('*.json')]
		records = [
			p
			for p in records
			if p['draft_id'] == draft_id and not p.get('accepted_at') and not p.get('discarded_at')
		]
		return get_preview(max(records, key=lambda p: p['created_at'])['id']) if records else None
