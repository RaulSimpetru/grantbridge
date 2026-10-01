import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from grant_recommender import api, concepts, proposals
from grant_recommender.product_grounding import CORE_FIELDS, preserve_intent, safe_revision_critique


@pytest.fixture
def workspace(monkeypatch, tmp_path):
	monkeypatch.setattr(proposals, 'ROOT', tmp_path / 'proposals')
	concept = {
		'concept_id': '1',
		'title': 'Coaching for glycaemic control',
		'problem': 'Poor glycaemic control',
		'central_hypothesis': 'Dietary coaching improves control',
		'objectives': ['Develop coaching', 'Test glycaemic outcomes'],
		'novelty': 'New coaching design',
		'principal_feasibility_risk': 'Participant access unknown',
		'required_assets_and_collaborators': ['Needs ethics review'],
		'call_evidence': [{'source_id': 'CALL_001', 'claim_supported': 'Funds original research'}],
		'researcher_evidence': [
			{'source_id': 'PROFILE_001', 'claim_supported': 'Has CGM experience'},
			{'source_id': 'PROFILE_001', 'claim_supported': ' '},
		],
	}
	job = {
		'id': 'test-job',
		'status': 'succeeded',
		'topic_code': 'DFG-TEST',
		'trial': True,
		'draft': {'concepts': [concept]},
		'concepts': [{**concept, 'title': 'Signal-only revision'}],
		'evidence': {
			'CALL_001': {'section': 'scope', 'text': 'Support original research.'},
			'PROFILE_001': {'section': 'methods', 'text': 'Confirmed CGM experience.'},
			'ARCHIVE_001': {
				'section': 'historical',
				'text': 'A proposed coaching study; not completed.',
			},
		},
		'archive_context': {'query': 'dietary coaching for type 2 diabetes', 'passages': []},
	}
	monkeypatch.setattr(
		concepts, 'get_job', lambda identifier: deepcopy(job) if identifier == 'test-job' else None
	)
	monkeypatch.setattr(
		concepts,
		'connection_settings',
		lambda: {'base_url': 'http://localhost:11435', 'model': 'test-model'},
	)
	monkeypatch.setattr(
		concepts, '_ollama_chat', lambda *a, **k: pytest.fail('No model contact without approval')
	)
	return proposals.create('test-job', '1', 'original')


def content(text='Proposed experiment [CALL_001]'):
	return {
		'text': text,
		'citations': [{'source_id': 'CALL_001', 'claim_supported': 'Research scope'}],
		'missing_info': ['Confirm participant access'],
	}


def preview(draft):
	return proposals.preview(
		draft['id'],
		'methods',
		draft['revision'],
		'Keep coaching',
		['CALL_001', 'ARCHIVE_001'],
	)


def finish(identifier):
	deadline = time.monotonic() + 5
	while time.monotonic() < deadline:
		record = proposals.get_preview(identifier)
		if record['status'] != 'running':
			return record
		time.sleep(0.01)
	raise AssertionError('Generation did not finish')


def test_core_scientific_intent_is_restored_not_rewritten(workspace):
	draft = {'concepts': [workspace['concept']]}
	revised = deepcopy(draft)
	for field in CORE_FIELDS:
		revised['concepts'][0][field] = (
			['Sensor calibration only'] if field == 'objectives' else 'Methodological work only'
		)
	final, audit = preserve_intent(draft, revised)
	assert final == draft
	assert len(audit['restored_fields']) == len(CORE_FIELDS)
	assert revised != draft  # inputs untouched
	# the notice must still say the critique was not applied on the user's behalf
	assert 'critique was not applied' in audit['notice']
	with pytest.raises(ValueError, match='identifiers'):
		preserve_intent(draft, {'concepts': [{**workspace['concept'], 'concept_id': '2'}]})
	assert 'concept_critiques' not in safe_revision_critique()


def test_workspace_preserves_original_sources_and_does_not_generate(workspace):
	assert 'Dietary coaching' in workspace['sections']['aims']['text']
	assert workspace['trial'] is True
	assert workspace['sections']['methods']['text'] == ''
	assert proposals.create('test-job', '1', 'original')['id'] == workspace['id']
	assert proposals.create('test-job', '1', 'revised')['title'] == 'Signal-only revision'
	assert proposals.get(workspace['id']) == workspace
	with pytest.raises(ValueError):
		proposals.create('unknown', '1', 'original')
	with pytest.raises(ValueError):
		proposals.create('test-job', 'unknown', 'original')


def test_save_keeps_version_history_and_rejects_stale_overwrites(workspace):
	next_draft = proposals.save(workspace['id'], 'methods', 1, content())
	assert next_draft['revision'] == 2
	prior = json.loads((proposals.ROOT / 'history' / workspace['id'] / '1.json').read_text())
	assert prior == workspace
	with pytest.raises(ValueError, match='changed'):
		proposals.save(workspace['id'], 'methods', 1, content('Lost update'))
	assert proposals.get(workspace['id']) == next_draft


def test_preview_contains_exact_selected_sources_and_destination_without_contact(workspace):
	p = preview(workspace)
	context = json.loads(p['packet']['user_prompt'])
	assert set(context['evidence']) == {'CALL_001', 'ARCHIVE_001'}
	assert context['research_question'] == workspace['research_question']
	assert context['concept'] == workspace['concept']
	assert context['trial'] is True
	assert p['connection']['base_url'] == 'http://localhost:11435'
	assert proposals.latest_preview(workspace['id']) == p
	assert proposals.get(workspace['id']) == workspace
	with pytest.raises(ValueError, match='approval'):
		proposals.generate(p['id'], False)


def test_preview_rejects_unknown_sources_or_missing_call(workspace):
	for source_ids in (['EVIL'], ['PROFILE_001'], [], ['CALL_001', 'CALL_001']):
		with pytest.raises(ValueError):
			proposals.preview(workspace['id'], 'methods', 1, '', source_ids)
	with pytest.raises(ValueError):
		proposals.get('../secret')
	with pytest.raises(ValueError):
		proposals.save(workspace['id'], 'unknown', 1, content())


@pytest.mark.parametrize('change', ['connection', 'expiry', 'draft'])
def test_stale_consent_cannot_send(workspace, monkeypatch, change):
	p = preview(workspace)
	if change == 'connection':
		monkeypatch.setattr(
			concepts,
			'connection_settings',
			lambda: {'base_url': 'https://different.example', 'model': 'test'},
		)
	elif change == 'expiry':
		p['created_at'] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
		proposals._write('previews', p)
	else:
		proposals.save(workspace['id'], 'aims', 1, content())
	with pytest.raises(ValueError):
		proposals.generate(p['id'], True)


def test_generation_is_idempotent_and_requires_acceptance(workspace, monkeypatch):
	seen = []

	def model(packet, **kwargs):
		seen.append(packet)
		return content(), {'request': 'private trace', 'response': 'private trace'}

	monkeypatch.setattr(concepts, '_ollama_chat', model)
	p = preview(workspace)
	proposals.generate(p['id'], True)
	done = finish(p['id'])
	assert done['status'] == 'succeeded'
	assert 'model_trace' not in done
	assert proposals.get(workspace['id']) == workspace
	proposals.generate(p['id'], True)
	assert len(seen) == 1
	assert seen[0]['_connection'] == p['connection']
	assert seen[0]['user_prompt'] == p['packet']['user_prompt']
	accepted = proposals.accept(p['id'])
	assert accepted['sections']['methods']['text'] == content()['text']
	assert accepted['revision'] == 2
	assert proposals.latest_preview(workspace['id']) is None
	with pytest.raises(ValueError):
		proposals.accept(p['id'])


def test_a_discarded_suggestion_stays_gone_and_cannot_be_accepted(workspace, monkeypatch):
	monkeypatch.setattr(concepts, '_ollama_chat', lambda *a, **k: (content(), {}))
	p = preview(workspace)
	with pytest.raises(ValueError):
		proposals.discard(p['id'])  # nothing to discard before the model answers
	proposals.generate(p['id'], True)
	assert finish(p['id'])['status'] == 'succeeded'
	client = TestClient(api.app)
	assert client.post(f'/proposals/previews/{p["id"]}/discard').status_code == 200
	# Reopening the draft asks for the latest preview; it must not bring the suggestion back.
	assert client.get(f'/proposals/{workspace["id"]}/latest-preview').json() is None
	assert client.post(f'/proposals/previews/{p["id"]}/accept').status_code == 409
	assert proposals.get(workspace['id']) == workspace


def test_aims_keep_the_citations_of_the_concept_they_come_from(workspace):
	aims = workspace['sections']['aims']
	assert aims['citations'] == [
		{'source_id': 'CALL_001', 'claim_supported': 'Funds original research'},
		{'source_id': 'PROFILE_001', 'claim_supported': 'Has CGM experience'},
	]
	saved = proposals.save(workspace['id'], 'aims', 1, {k: aims[k] for k in content()})
	assert saved['sections']['aims']['citations'] == aims['citations']


def test_suggestion_cannot_overwrite_edits_made_while_generating(workspace, monkeypatch):
	monkeypatch.setattr(concepts, '_ollama_chat', lambda *a, **k: (content(), {}))
	p = preview(workspace)
	proposals.generate(p['id'], True)
	assert finish(p['id'])['status'] == 'succeeded'
	proposals.save(workspace['id'], 'methods', 1, content('My newer text'))
	with pytest.raises(ValueError, match='changed'):
		proposals.accept(p['id'])
	assert proposals.get(workspace['id'])['sections']['methods']['text'] == 'My newer text'


@pytest.mark.parametrize(
	'bad',
	[
		{'text': 'Invented [CALL_999]', 'citations': [], 'missing_info': []},
		{
			'text': 'Claim',
			'citations': [{'source_id': 'PROFILE_001', 'claim_supported': 'Not selected'}],
			'missing_info': [],
		},
		{'text': 'No sources', 'citations': [], 'missing_info': []},
		{'text': 'Bad format', 'citations': [], 'missing_info': 'unknown'},
	],
)
def test_bad_model_content_fails_without_touching_draft(workspace, monkeypatch, bad):
	monkeypatch.setattr(concepts, '_ollama_chat', lambda *a, **k: (bad, {}))
	p = preview(workspace)
	proposals.generate(p['id'], True)
	assert finish(p['id'])['status'] == 'failed'
	assert proposals.get(workspace['id']) == workspace


def test_restart_resolves_spinner_without_resending(workspace):
	p = preview(workspace)
	p['status'] = 'running'
	proposals._write('previews', p)
	proposals.interrupt_pending()
	assert proposals.get_preview(p['id'])['status'] == 'failed'
	assert proposals.get(workspace['id']) == workspace


def test_api_local_only_no_store_and_approval_required(workspace):
	client = TestClient(api.app)
	response = client.get('/proposals', params={'job_id': 'test-job'})
	assert response.status_code == 200
	assert response.headers['cache-control'] == 'no-store'
	assert response.json()[0]['id'] == workspace['id']
	assert (
		client.get(
			'/proposals', params={'job_id': 'test-job'}, headers={'Origin': 'https://evil.example'}
		).status_code
		== 403
	)
	p = client.post(
		f'/proposals/{workspace["id"]}/preview',
		json={'section': 'methods', 'revision': 1, 'source_ids': ['CALL_001']},
	).json()
	assert (
		client.post(f'/proposals/previews/{p["id"]}/generate', json={'approved': False}).status_code
		== 422
	)
	assert client.post(f'/proposals/previews/{p["id"]}/generate', json={}).status_code == 422


def test_a_draft_opens_from_a_run_this_process_never_saw_start(tmp_path, monkeypatch):
	"""concepts.JOBS is filled once at import, so a run the watch loop started
	afterwards is on disk but not in memory. The terminal app could list and
	review such a concept but not open a draft from it."""
	import json

	from grant_recommender import concepts

	job = {
		'id': 'a1b2c3d4e5f6',
		'topic_code': 'OPEN-1',
		'status': 'succeeded',
		'created_at': '2030-01-22',
		'evidence': {},
		'concepts': [
			{
				'concept_id': '1',
				'title': 'From disk',
				'problem': 'a problem',
				'central_hypothesis': 'a hypothesis',
				'objectives': ['an objective'],
				'principal_feasibility_risk': 'a risk',
				'required_assets_and_collaborators': ['a collaborator'],
			}
		],
	}
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	(tmp_path / 'a1b2c3d4e5f6.json').write_text(json.dumps(job))
	assert concepts.get_job('a1b2c3d4e5f6') is None

	draft = proposals.create('a1b2c3d4e5f6', '1', 'revised')

	assert draft['title'] == 'From disk'
	assert draft['sections']['risks']['missing_info'][0] == 'a risk'


def test_a_job_id_that_is_not_a_job_id_never_reaches_the_filesystem(tmp_path, monkeypatch):
	"""load_job turns the id into a path, and it arrives from a request body."""
	from grant_recommender import concepts

	nested = tmp_path / 'nested' / 'concept_jobs'
	nested.mkdir(parents=True)
	(tmp_path / 'secret.json').write_text('{"id": "secret", "status": "succeeded"}')
	monkeypatch.setattr(concepts, 'JOBS_DIR', nested)

	assert concepts.load_job('../../secret') is None
	assert concepts.load_job('') is None
	assert concepts.load_job('NOTHEXADECIMAL') is None
