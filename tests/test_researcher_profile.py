from fastapi.testclient import TestClient

from grant_recommender import api, concepts, eligibility
from grant_recommender.portfolio import researcher_evidence


def test_profile_preview_matches_generation_input_and_withholds_inferred(monkeypatch):
	assertions = [
		{
			'component': 'methods',
			'claim': 'CGM',
			'source': 'CV',
			'confidence': 'confirmed',
			'included': True,
		},
		{
			'component': 'methods',
			'claim': 'Unverified',
			'source': 'Import',
			'confidence': 'inferred',
			'included': True,
		},
		{
			'component': 'datasets',
			'claim': 'Excluded',
			'source': 'CV',
			'confidence': 'confirmed',
			'included': False,
		},
	]
	monkeypatch.setattr(api, 'load_portfolio', lambda: assertions)
	profile = TestClient(api.app).get('/researcher-profile').json()
	packet = concepts.build_generation_packet(
		job_id='test',
		call={'topic_code': 'T', 'scope_text': 'Scope: research'},
		portfolio=assertions,
	)
	assert profile['used_count'] == 1
	assert profile['not_used_count'] == 2
	assert profile['evidence'] == [
		e for e in packet['evidence'] if e['source_id'].startswith('PROFILE_')
	]
	assert 'Unverified' not in profile['context'] and 'Excluded' not in profile['context']


def test_full_phd_date_is_visible_in_actual_model_evidence():
	result = researcher_evidence(
		[
			{
				'component': 'career_eligibility',
				'claim': 'PhD',
				'confidence': 'confirmed',
				'phd_award_date': '2001-01-01',
			}
		]
	)
	assert '2001-01-01' in result[0]['text']


def test_confirmed_prior_proposal_is_labeled_as_intention_not_capability(monkeypatch):
	assertion = {
		'component': 'prior_proposals',
		'claim': 'Proposed a dietary coaching study with a future partner site',
		'confidence': 'confirmed',
		'source': 'Prior application, page 2',
		'included': True,
	}
	monkeypatch.setattr(api, 'load_portfolio', lambda: [assertion])
	profile = TestClient(api.app).get('/researcher-profile').json()
	packet = concepts.build_generation_packet(
		job_id='test',
		call={'topic_code': 'T', 'scope_text': 'Scope: research'},
		portfolio=[assertion],
	)
	assert 'not proof of funding' in profile['context']
	assert profile['evidence'] == [
		e for e in packet['evidence'] if e['source_id'].startswith('PROFILE_')
	]


def test_exploratory_trial_keeps_eligibility_uncertain(monkeypatch, tmp_path):
	call = {'topic_code': 'T', 'scope_text': 'Scope: research', 'deadlines': []}
	assertions = [{'component': 'methods', 'claim': 'CGM', 'confidence': 'confirmed'}]
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews.json')
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path / 'jobs')
	monkeypatch.setattr(concepts, 'JOBS', {})
	monkeypatch.setattr(concepts.threading.Thread, 'start', lambda self: None)
	job = concepts.start_job(call, assertions, trial=True)
	assert job['trial'] is True
	assert job['eligibility_snapshot']['state'] == 'uncertain'
	assert not eligibility.REVIEW_PATH.exists()
	try:
		concepts.start_job(call, assertions)
	except ValueError:
		pass
	else:
		raise AssertionError('Normal generation must still require eligibility')


def test_trial_api_does_not_record_develop_decision(monkeypatch, tmp_path):
	call = {'topic_code': 'T', 'scope_text': 'Scope: research', 'deadlines': []}
	monkeypatch.setattr(api, 'get_topic', lambda code: call)
	monkeypatch.setattr(
		api,
		'load_portfolio',
		lambda: [{'component': 'methods', 'claim': 'CGM', 'confidence': 'confirmed'}],
	)
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews.json')
	monkeypatch.setattr(concepts, 'connection_status', lambda: {'ready': True})
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path / 'jobs')
	monkeypatch.setattr(concepts, 'JOBS', {})
	monkeypatch.setattr(concepts, '_run_job', lambda *args: None)

	def forbidden(*args, **kwargs):
		raise AssertionError('Trial must not record a develop decision')

	monkeypatch.setattr(api.decisions, 'record', forbidden)
	client = TestClient(api.app)
	assert client.post('/concepts/jobs', json={'topic_code': 'T'}).status_code == 409
	response = client.post('/concepts/jobs', json={'topic_code': 'T', 'trial': True})
	assert response.status_code == 202
	assert response.json()['trial'] is True
