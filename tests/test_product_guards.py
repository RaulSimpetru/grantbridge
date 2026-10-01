from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from grant_recommender import api, concepts, eligibility, portfolio
from grant_recommender.dossier import build_dossier
from grant_recommender.scoring import score_calls


@pytest.fixture
def inputs(monkeypatch, tmp_path):
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews.json')
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')
	call = {
		'topic_code': 'TEST-OPEN',
		'scope_text': 'Scope: CGM signal processing.',
		'deadlines': [(datetime.now(UTC) + timedelta(days=30)).isoformat()],
	}
	claim = portfolio.add_assertion(
		'methods', 'CGM signal processing', 'Methods paper', 'confirmed'
	)
	monkeypatch.setattr(api, 'get_topic', lambda code: call)
	return call, [claim]


def test_unreviewed_claims_never_score_or_generate(inputs):
	call, claims = inputs
	for confidence in ('inferred', 'needs_review', 'missing'):
		unreviewed = [{**claims[0], 'confidence': confidence}]
		assert score_calls([call], unreviewed) == {call['topic_code']: None}
		with pytest.raises(ValueError, match='usable portfolio assertions'):
			concepts.build_generation_packet(job_id='test', call=call, portfolio=unreviewed)


def test_review_is_required_and_invalidated_by_input_changes(inputs):
	call, claims = inputs
	assert eligibility.assess(call, claims)['state'] == 'uncertain'
	eligibility.record_review(
		call, claims, 'eligible', 'All conditions checked', 'Official source section 4'
	)
	assert eligibility.assess(call, claims)['state'] == 'eligible'
	assert (
		eligibility.assess({**call, 'topic_conditions': 'Changed rules'}, claims)['state']
		== 'uncertain'
	)
	assert eligibility.assess(call, [{**claims[0], 'included': False}])['state'] == 'uncertain'


def test_closed_call_cannot_be_overridden(inputs):
	call, claims = inputs
	call['deadlines'] = ['2020-01-01T12:00:00+00:00']
	eligibility.record_review(
		call, claims, 'eligible', 'Reviewer incorrectly approved', 'Official source section 4'
	)
	assert eligibility.assess(call, claims)['state'] == 'ineligible'
	with pytest.raises(ValueError, match='passed'):
		concepts.start_job(call, claims)


def test_api_rejects_generation_before_contacting_ollama(inputs, monkeypatch):
	call, _ = inputs
	monkeypatch.setattr(
		concepts, 'connection_status', lambda: pytest.fail('Must gate before contacting model')
	)
	response = TestClient(api.app).post('/concepts/jobs', json={'topic_code': call['topic_code']})
	assert response.status_code == 409


def test_offline_ollama_returns_actionable_503_without_starting_job(inputs, monkeypatch):
	call, claims = inputs
	eligibility.record_review(
		call, claims, 'eligible', 'All conditions checked', 'Official source section 4'
	)
	monkeypatch.setattr(
		concepts,
		'connection_status',
		lambda: {'ready': False, 'detail': 'Connect the SSH tunnel.'},
	)
	monkeypatch.setattr(concepts, 'start_job', lambda *a, **k: pytest.fail('No job when offline'))
	response = TestClient(api.app).post('/concepts/jobs', json={'topic_code': call['topic_code']})
	assert response.status_code == 503
	assert 'tunnel' in response.json()['detail']


def test_review_requires_explicit_confirmation_and_rationale(inputs):
	call, _ = inputs
	response = TestClient(api.app).post(
		f'/calls/{call["topic_code"]}/eligibility',
		json={'state': 'eligible', 'reason': 'yes', 'source': 'x', 'conditions_checked': False},
	)
	assert response.status_code == 422


def test_null_claim_is_rejected_without_corrupting_portfolio(inputs):
	_, claims = inputs
	response = TestClient(api.app).patch(f'/portfolio/{claims[0]["id"]}', json={'claim': None})
	assert response.status_code == 422
	assert portfolio.load_portfolio() == claims


def test_dossier_pairs_confirmed_sources_and_marks_declared_gaps(inputs):
	call, claims = inputs
	claims += [
		{
			'claim': 'Clinical partner needed',
			'component': 'collaborators',
			'confidence': 'missing',
			'included': True,
		}
	]
	dossier = build_dossier(call, claims)
	assert dossier['evidence_pairs'][0]['claim'] == 'CGM signal processing'
	assert dossier['evidence_pairs'][0]['passage']['text'] == 'CGM signal processing.'
	assert dossier['gaps'] == ['Clinical partner needed']
	assert len(dossier['evidence_pairs']) == 1


def test_later_stage_deadline_is_not_treated_as_a_fresh_call(inputs):
	call, _ = inputs
	call['deadlines'].append('2020-01-01T12:00:00+00:00')
	assert eligibility.availability(call) == 'later_stage'


def test_phd_date_requires_full_iso_date_and_survives_correction(inputs):
	client = TestClient(api.app)
	base = {'component': 'career_eligibility', 'claim': 'PhD award date', 'confidence': 'confirmed'}
	assert client.post('/portfolio', json={**base, 'phd_award_date': '2001'}).status_code == 422
	created = client.post('/portfolio', json={**base, 'phd_award_date': '2001-01-01'})
	assert created.status_code == 201
	assert created.json()['phd_award_date'] == '2001-01-01'
	updated = client.patch(
		f'/portfolio/{created.json()["id"]}', json={'phd_award_date': '2001-01-02'}
	)
	assert updated.status_code == 200
	assert portfolio.load_portfolio()[-1]['phd_award_date'] == '2001-01-02'


def test_midnight_date_placeholder_requires_closing_time_review(inputs):
	from zoneinfo import ZoneInfo

	call, claims = inputs
	today = datetime.now(ZoneInfo('Europe/Brussels')).date().isoformat()
	call['deadlines'] = [f'{today}T00:00:00+00:00']
	assert eligibility.availability(call) == 'closing_today'
	assert 'exact closing time' in eligibility.assess(call, claims)['reason']


def test_empty_review_cannot_pass_length_validation_with_whitespace(inputs):
	call, _ = inputs
	response = TestClient(api.app).post(
		f'/calls/{call["topic_code"]}/eligibility',
		json={
			'state': 'eligible',
			'reason': ' ' * 20,
			'source': ' ' * 20,
			'conditions_checked': True,
		},
	)
	assert response.status_code == 422


def test_deadline_check_works_without_a_system_timezone_database():
	"""Windows and slim Linux images ship no IANA timezone data. The tzdata
	package must cover them, or every call with a deadline fails eligibility."""
	import zoneinfo

	zoneinfo.reset_tzpath(to=[])
	zoneinfo.ZoneInfo.clear_cache()
	try:
		assert (
			eligibility.availability({'deadlines': ['2099-01-01T17:00:00']}) == 'upcoming_deadline'
		)
	finally:
		zoneinfo.reset_tzpath()
