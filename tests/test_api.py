import time

import pytest
import requests
from fastapi.testclient import TestClient

from grant_recommender import api, concepts, decisions, eligibility, german_funding, portfolio

CALL = {
	'topic_code': 'FAKE-CODE-2024',
	'title': 'A fake topic',
	'call_identifier': 'H2020-FAKE-2023-2024',
	'call_title': 'FAKE CALL TITLE',
	'deadlines': ['2099-09-13T00:00:00.000+0000'],
	'status': '31094503',
	'types_of_action': ['Research and Innovation action'],
	'scope_text': 'Do the work.',
	'topic_conditions': 'See General Annexes B and C.',
	'framework_programme': '31045243',
	'programme_period': '2021 - 2027',
	'source_url': 'https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/FAKE-CODE-2024.json',
	'fetched_at': '2030-01-04T00:00:00+00:00',
}


def _fake_get_topic(topic_code):
	return CALL if topic_code == CALL['topic_code'] else None


@pytest.fixture(autouse=True)
def isolated_eligibility(monkeypatch, tmp_path):
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'eligibility.json')
	monkeypatch.setattr(concepts, 'connection_status', lambda: {'ready': True, 'detail': 'ready'})


def test_list_calls_returns_only_the_watched_topics_that_resolve(monkeypatch, tmp_path):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(api, 'watched_topic_codes', lambda: [CALL['topic_code'], 'MISSING-CODE'])
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)

	response = TestClient(api.app).get('/calls')

	assert response.status_code == 200
	body = response.json()
	assert [c['topic_code'] for c in body] == [CALL['topic_code']]
	assert body[0]['eligibility']['state'] == 'uncertain'
	assert body[0]['scientific_fit'] is None  # empty portfolio -- not a 0


def test_read_call_returns_404_for_an_unknown_topic_code(monkeypatch):
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)

	response = TestClient(api.app).get('/calls/MISSING-CODE')

	assert response.status_code == 404


def test_an_unreachable_call_source_is_a_readable_503_not_a_500(monkeypatch):
	monkeypatch.setattr(
		api,
		'get_topic',
		lambda code: (_ for _ in ()).throw(requests.ConnectionError('SEDIA is down')),
	)
	client = TestClient(api.app)

	for response in (
		client.get('/calls/ANY-CODE'),
		client.post('/concepts/jobs', json={'topic_code': 'ANY-CODE'}),
	):
		assert response.status_code == 503
		assert 'Retry shortly' in response.json()['detail']


def test_the_watch_loop_keeps_going_during_an_eu_outage(monkeypatch):
	"""On a fresh clone nothing is imported yet, so an EU outage leaves no call
	at all. GET /calls says so, but the loop must carry on to discover() and the
	German feeds rather than fail every tick."""
	monkeypatch.setattr(
		api,
		'get_topic',
		lambda code: (_ for _ in ()).throw(requests.ConnectionError('SEDIA is down')),
	)

	assert TestClient(api.app).get('/calls').status_code == 503
	assert api._loop_calls() == []


def test_a_portfolio_that_no_longer_parses_is_a_readable_422(monkeypatch):
	portfolio.PORTFOLIO_PATH.write_text('[{"claim": "Metabolic health",}]')

	response = TestClient(api.app).get('/portfolio', headers={'Origin': 'http://127.0.0.1:5173'})

	assert response.status_code == 422
	assert 'portfolio.json is not valid JSON' in response.json()['detail']
	# The UI can only read the message if the CORS header survives.
	assert response.headers['access-control-allow-origin'] == 'http://127.0.0.1:5173'


def test_read_call_returns_the_full_record_for_a_known_topic_code(monkeypatch, tmp_path):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(api, 'watched_topic_codes', lambda: [CALL['topic_code']])
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)

	response = TestClient(api.app).get(f'/calls/{CALL["topic_code"]}')

	assert response.status_code == 200
	assert all(response.json()[key] == value for key, value in CALL.items())
	assert response.json()['eligibility']['state'] == 'uncertain'
	assert response.json()['scientific_fit'] is None


def test_scientific_fit_is_populated_and_ranked_once_a_portfolio_claim_exists(
	monkeypatch, tmp_path
):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	portfolio.add_assertion('methods', 'do the work', None, 'confirmed', 'private')
	other = {
		**CALL,
		'topic_code': 'OTHER-CODE',
		'scope_text': 'Unrelated scope about something else.',
	}
	monkeypatch.setattr(
		api, 'watched_topic_codes', lambda: [CALL['topic_code'], other['topic_code']]
	)
	monkeypatch.setattr(
		api,
		'get_topic',
		lambda code: {CALL['topic_code']: CALL, other['topic_code']: other}.get(code),
	)

	response = TestClient(api.app).get('/calls')

	assert response.status_code == 200
	body = {c['topic_code']: c for c in response.json()}
	assert body[CALL['topic_code']]['scientific_fit']['rank'] == 1
	assert body[other['topic_code']]['scientific_fit']['rank'] == 2


def test_portfolio_lifecycle_create_list_and_patch(monkeypatch, tmp_path):
	from grant_recommender import portfolio

	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	client = TestClient(api.app)

	created = client.post(
		'/portfolio',
		json={'component': 'methods', 'claim': 'CGM analysis', 'confidence': 'confirmed'},
	)
	assert created.status_code == 201
	assertion_id = created.json()['id']

	listed = client.get('/portfolio')
	assert [a['id'] for a in listed.json()] == [assertion_id]

	patched = client.patch(f'/portfolio/{assertion_id}', json={'visibility': 'public'})
	assert patched.status_code == 200
	assert patched.json()['visibility'] == 'public'
	assert patched.json()['claim'] == 'CGM analysis'


def test_create_portfolio_assertion_rejects_an_unknown_component(monkeypatch, tmp_path):
	from grant_recommender import portfolio

	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	response = TestClient(api.app).post(
		'/portfolio', json={'component': 'not_a_real_component', 'claim': 'x'}
	)
	assert response.status_code == 422


def test_patch_portfolio_assertion_returns_404_for_an_unknown_id(monkeypatch, tmp_path):
	from grant_recommender import portfolio

	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	response = TestClient(api.app).patch(
		'/portfolio/does-not-exist', json={'confidence': 'confirmed'}
	)
	assert response.status_code == 404


def test_import_openalex_route_creates_inferred_assertions(monkeypatch, tmp_path):
	from grant_recommender import portfolio

	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(
		'grant_recommender.openalex.get_author',
		lambda author_id: {
			'display_name': 'Test Researcher',
			'topics': [{'display_name': 'Glucose Monitoring', 'count': 10}],
		},
	)

	response = TestClient(api.app).post('/portfolio/import/openalex', json={'author_id': 'A123'})

	assert response.status_code == 200
	body = response.json()
	assert [a['claim'] for a in body] == ['Glucose Monitoring']
	assert body[0]['confidence'] == 'inferred'


def _fake_ollama_post(url, json, timeout, allow_redirects=False, stream=False):
	import json as jsonlib

	schema = json['format']
	props = schema.get('properties', {})
	if 'concept_critiques' in props:
		content = {
			'concept_critiques': [
				{
					'concept_id': str(i + 1),
					'grounding_and_fit': 'ok',
					'novelty_and_feasibility': 'ok',
					'revision_action': 'none',
				}
				for i in range(3)
			],
			'unsupported_claims': [],
			'missing_requirements': [],
			'diversity_problems': [],
			'revision_priorities': [],
		}
	else:
		call_enum = schema['properties']['concepts']['items']['properties']['call_evidence'][
			'items'
		]['properties']['source_id'].get('enum', [])
		researcher_enum = schema['properties']['concepts']['items']['properties'][
			'researcher_evidence'
		]['items']['properties']['source_id'].get('enum', [])
		content = {
			'concepts': [
				{
					'concept_id': str(i + 1),
					'title': f'Concept {i}',
					'problem': 'p',
					'central_hypothesis': f'h{i}',
					'call_fit': 'fits',
					'researcher_fit': 'fits',
					'objectives': ['a', 'b'],
					'required_assets_and_collaborators': ['needs a partner'],
					'novelty': 'novel',
					'principal_feasibility_risk': 'risk',
					'call_evidence': [{'source_id': call_enum[0], 'claim_supported': 'x'}],
					'researcher_evidence': [
						{'source_id': researcher_enum[0], 'claim_supported': 'y'}
					],
				}
				for i in range(3)
			]
		}

	class _Resp:
		status_code = 200

		def raise_for_status(self):
			pass

		def json(self):
			return {'message': {'content': jsonlib.dumps(content)}}

		def iter_lines(self):
			# The product streams so the UI can show the model working; emit the
			# same document as newline-delimited chunks.
			yield jsonlib.dumps(
				{'message': {'content': jsonlib.dumps(content)}, 'done': False}
			).encode()
			yield jsonlib.dumps({'message': {'content': ''}, 'done': True}).encode()

	return _Resp()


def _wait_for_job(job_id, timeout=5):
	deadline = time.time() + timeout
	while time.time() < deadline:
		job = concepts.get_job(job_id)
		if job['status'] != 'running':
			return job
		time.sleep(0.02)
	raise TimeoutError('job did not finish in time')


def test_concept_job_lifecycle_reaches_succeeded_and_records_a_develop_decision(
	monkeypatch, tmp_path
):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path / 'concept_jobs')
	concepts.JOBS.clear()
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)
	monkeypatch.setattr(concepts.requests, 'post', _fake_ollama_post)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)
	portfolio.add_assertion('methods', 'CGM analysis', None, 'confirmed', 'private')
	eligibility.record_review(
		CALL,
		portfolio.load_portfolio(),
		'eligible',
		'Test requirements met',
		'Test source section 4',
	)

	client = TestClient(api.app)
	created = client.post('/concepts/jobs', json={'topic_code': CALL['topic_code']})
	assert created.status_code == 202
	job_id = created.json()['id']

	final = _wait_for_job(job_id)
	assert final['status'] == 'succeeded'
	assert len(final['concepts']) == 3

	polled = client.get(f'/concepts/jobs/{job_id}')
	assert polled.status_code == 200
	assert polled.json()['status'] == 'succeeded'

	listed = client.get('/concepts/jobs', params={'topic_code': CALL['topic_code']})
	assert [j['id'] for j in listed.json()] == [job_id]

	decision_events = decisions.load()
	assert any(e['kind'] == 'develop' and e['reason'] == job_id for e in decision_events)


def test_concept_job_rejects_an_empty_portfolio_with_400(monkeypatch, tmp_path):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')
	concepts.JOBS.clear()
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)
	eligibility.record_review(
		CALL, [], 'eligible', 'Test requirements met', 'Test source section 4'
	)

	response = TestClient(api.app).post('/concepts/jobs', json={'topic_code': CALL['topic_code']})

	assert response.status_code == 400
	assert concepts.JOBS == {}


def test_read_concept_job_returns_404_for_an_unknown_id(monkeypatch):
	concepts.JOBS.clear()
	response = TestClient(api.app).get('/concepts/jobs/does-not-exist')
	assert response.status_code == 404


def test_decisions_lifecycle_records_and_lists(monkeypatch, tmp_path):
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')
	client = TestClient(api.app)

	created = client.post(
		'/decisions', json={'kind': 'save', 'target_type': 'call', 'target_id': 'CALL-1'}
	)
	assert created.status_code == 201

	listed = client.get('/decisions')
	assert [e['kind'] for e in listed.json()] == ['save']


def test_activity_merges_decisions_and_concept_runs_newest_first(monkeypatch, tmp_path):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path / 'concept_jobs')
	concepts.JOBS.clear()
	monkeypatch.setattr(api, 'get_topic', _fake_get_topic)
	monkeypatch.setattr(concepts.requests, 'post', _fake_ollama_post)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)
	portfolio.add_assertion('methods', 'CGM analysis', None, 'confirmed', 'private')
	eligibility.record_review(
		CALL,
		portfolio.load_portfolio(),
		'eligible',
		'Test requirements met',
		'Test source section 4',
	)

	client = TestClient(api.app)
	client.post('/decisions', json={'kind': 'save', 'target_type': 'call', 'target_id': 'CALL-1'})
	created = client.post('/concepts/jobs', json={'topic_code': CALL['topic_code']})
	job_id = created.json()['id']
	_wait_for_job(job_id)

	response = client.get('/activity')
	assert response.status_code == 200
	body = response.json()
	types = [item['type'] for item in body]
	# newest first: the develop decision + concept job were both created after
	# the plain save, so save must sort last.
	assert types[-1] == 'decision'
	assert body[-1]['decision']['kind'] == 'save'
	concept_items = [item for item in body if item['type'] == 'concept_run']
	assert concept_items[0]['concept_run']['id'] == job_id
	develop_items = [
		item
		for item in body
		if item['type'] == 'decision' and item['decision']['kind'] == 'develop'
	]
	assert develop_items[0]['decision']['reason'] == job_id


def test_user_added_feeds_are_listed_and_refreshable_beside_the_built_in_ones(
	monkeypatch, tmp_path
):
	# conftest isolates the source cache, not the registry of added feeds.
	monkeypatch.setattr(german_funding, 'CUSTOM_SOURCES', tmp_path / 'custom_sources.json')
	german_funding._write(
		german_funding.CUSTOM_SOURCES,
		{
			'custom-0123456789': {
				'name': 'Example Stiftung',
				'url': 'https://stiftung.example/feed.rss',
				'host': 'stiftung.example',
				'kind': 'rss',
				'coverage': 'User-added source.',
			}
		},
	)
	monkeypatch.setattr(
		german_funding,
		'fetch',
		lambda *a: (_ for _ in ()).throw(requests.ConnectionError('offline')),
	)
	client = TestClient(api.app)

	listed = client.get('/funding-sources').json()
	assert {'dfg', 'bmftr', 'custom-0123456789'} <= {source['id'] for source in listed}
	# A listed feed needs a working Refresh button: a failed fetch is reported
	# as the source's error, not rejected as an unknown provider.
	refreshed = client.post('/funding-sources/custom-0123456789/refresh')
	assert refreshed.status_code == 200
	assert 'offline' in refreshed.json()['error']


def test_a_rebound_hostname_cannot_read_the_private_portfolio():
	# DNS rebinding: a page on evil.example re-resolved to 127.0.0.1 reaches the
	# loopback server, but its requests still carry its own Host header.
	response = TestClient(api.app).get('/portfolio', headers={'Host': 'evil.example'})

	assert response.status_code == 403
