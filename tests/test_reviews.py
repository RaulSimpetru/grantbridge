"""Review panel: document adaptation, grounding of the prompts, concurrent
execution and the deterministic consolidation."""

import json

import pytest

from grant_recommender import concepts, reviews

CONCEPT = {
	'concept_id': '1',
	'title': 'Glucose forecasting for diabetes progression',
	'problem': 'Progression is assessed too coarsely.',
	'central_hypothesis': 'Glycaemic variability predicts progression.',
	'call_fit': 'Matches the call on predictive biomarkers.',
	'researcher_fit': 'Builds on confirmed glucose forecasting work.',
	'objectives': ['Record a cohort', 'Fit a model'],
	'required_assets_and_collaborators': ['Needs a clinical partner'],
	'novelty': 'First use of variability indices for this endpoint.',
	'principal_feasibility_risk': 'Patient access is unproven.',
	'call_evidence': [{'source_id': 'CALL_SCOPE', 'claim_supported': 'x'}],
	'researcher_evidence': [{'source_id': 'PROFILE_001', 'claim_supported': 'y'}],
}

JOB = {
	'id': 'job123',
	'status': 'succeeded',
	'topic_code': 'FAKE-2027',
	'trial': True,
	'concepts': [CONCEPT],
	'draft': {'concepts': [CONCEPT]},
	'evidence': {
		'CALL_SCOPE': {'section': 'scope', 'text': 'Develop predictive biomarkers.'},
		'PROFILE_001': {'section': 'methods', 'text': 'Long-term CGM glucose forecasting.'},
	},
}


def _verdict(verdict='revise'):
	return {
		'strengths': ['Clear hypothesis'],
		'weaknesses': ['Sample size unstated'],
		'unsupported_claims': [{'claim': 'First use', 'why_unsupported': 'No passage supports it'}],
		'questions_for_applicant': ['Which cohort?'],
		'verdict': verdict,
		'verdict_reason': 'Needs a sampling plan.',
	}


class _Streamed:
	status_code = 200

	def __init__(self, payload):
		self._payload = payload

	def raise_for_status(self):
		pass

	def json(self):
		# Used when a round runs without an on_text callback, which takes the
		# non-streaming path.
		return {'message': {'content': json.dumps(self._payload)}}

	def iter_lines(self):
		text = json.dumps(self._payload)
		yield json.dumps({'message': {'content': text[:20]}, 'done': False}).encode()
		yield json.dumps({'message': {'content': text[20:]}, 'done': False}).encode()
		yield json.dumps({'message': {'content': ''}, 'done': True}).encode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
	monkeypatch.setattr(reviews, 'ROOT', tmp_path / 'reviews')
	monkeypatch.setattr(
		concepts, 'connection_settings', lambda: {'base_url': 'http://x', 'model': 'm'}
	)


def test_from_concept_carries_every_field_and_its_evidence():
	document = reviews.from_concept(JOB, '1')
	assert document['title'] == CONCEPT['title']
	assert document['trial'] is True
	# Objectives are a list in the concept; a reviewer needs them as readable text.
	assert '- Record a cohort' in document['sections']['Objectives']
	assert document['evidence']['CALL_SCOPE']['text'] == 'Develop predictive biomarkers.'


def test_from_concept_refuses_an_unfinished_run():
	with pytest.raises(ValueError, match='completed'):
		reviews.from_concept({**JOB, 'status': 'running'}, '1')


def test_each_reviewer_gets_its_own_remit_and_the_same_evidence():
	document = reviews.from_concept(JOB, '1')
	packets = {key: reviews._packet(document, key) for key in reviews.ROLES}

	for key, packet in packets.items():
		assert reviews.ROLES[key]['remit'] in packet['system_prompt']
		# Grounding is not optional: every reviewer sees the passages and is
		# told not to claim standing it does not have.
		assert 'CALL_SCOPE' in packet['user_prompt']
		assert 'never claim to be a panel member' in packet['system_prompt']
		assert 'exploratory trial' in packet['user_prompt'].lower()
	assert len({p['system_prompt'] for p in packets.values()}) == len(reviews.ROLES)


def test_round_runs_every_reviewer_and_streams_each_one(monkeypatch):
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _Streamed(_verdict()))
	streamed: dict[str, list[str]] = {}
	document = reviews.from_concept(JOB, '1')

	result = reviews.run_round(
		document, on_text=lambda role, text: streamed.setdefault(role, []).append(text)
	)

	assert set(result['reviews']) == set(reviews.ROLES)
	assert all(r['status'] == 'succeeded' for r in result['reviews'].values())
	# Every reviewer streamed, and each callback carried a growing prefix.
	assert set(streamed) == set(reviews.ROLES)
	assert all(len(chunks) == 2 and len(chunks[1]) > len(chunks[0]) for chunks in streamed.values())


def test_one_failing_reviewer_leaves_a_partial_panel_not_a_dead_round(monkeypatch):
	def post(url, json, timeout, allow_redirects=False, stream=False):
		if 'Fit to this call' in json['messages'][0]['content']:
			raise concepts.requests.RequestException('model dropped')
		return _Streamed(_verdict())

	monkeypatch.setattr(concepts.requests, 'post', post)
	result = reviews.run_round(reviews.from_concept(JOB, '1'))

	assert result['reviews']['fit']['status'] == 'failed'
	assert result['consolidated']['failed_roles'] == ['fit']
	assert len(result['consolidated']['verdicts']) == len(reviews.ROLES) - 1


def test_consolidation_lets_the_worst_verdict_carry():
	"""A panel does not average a reject away into a revise."""
	built = {
		'a': {'status': 'succeeded', 'result': _verdict('fund')},
		'b': {'status': 'succeeded', 'result': _verdict('fund')},
		'c': {'status': 'succeeded', 'result': _verdict('reject')},
	}
	summary = reviews.consolidate(built)
	assert summary['outcome'] == 'reject'
	assert summary['tally'] == {'fund': 2, 'revise': 0, 'reject': 1}
	assert {c['raised_by'] for c in summary['unsupported_claims']} == {'a', 'b', 'c'}


def test_round_is_persisted_and_listable(monkeypatch):
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _Streamed(_verdict()))
	result = reviews.run_round(reviews.from_concept(JOB, '1'))

	assert reviews.load(result['id'])['id'] == result['id']
	listed = reviews.list_rounds()
	assert [r['id'] for r in listed] == [result['id']]
	assert listed[0]['title'] == CONCEPT['title']


def test_round_refuses_an_empty_document():
	with pytest.raises(ValueError, match='no sections'):
		reviews.run_round({'title': 't', 'topic_code': 'c', 'sections': {}, 'evidence': {}})


def test_unknown_role_is_rejected_before_any_model_call():
	with pytest.raises(ValueError, match='Unknown reviewer roles'):
		reviews.run_round(reviews.from_concept(JOB, '1'), roles=['methods', 'astrology'])


def test_a_verdict_reason_has_room_to_finish_its_argument():
	"""Ollama closes a string at its maxLength, and verdict_reason is written
	last: at 400 characters gemma4:12b-mlx was cut off mid-sentence in 8 of 9
	reviews. Measured reasons that finish run to about 530 characters."""
	assert reviews.REVIEW_SCHEMA['properties']['verdict_reason']['maxLength'] >= 1200


def test_a_verdict_reason_cut_off_at_its_cap_ends_on_its_last_full_sentence(monkeypatch):
	"""qwen3:4b-instruct-2507 wrote to the cap on 3 of 3 reviews and was stopped
	mid-word, so a longer cap alone does not finish its argument."""
	cut = ('Needs a sampling plan. ' * 80)[: reviews.REASON_CHARS]
	monkeypatch.setattr(
		concepts.requests, 'post', lambda *a, **k: _Streamed({**_verdict(), 'verdict_reason': cut})
	)

	result = reviews.run_round(reviews.from_concept(JOB, '1'), roles=['methods'])

	reason = result['reviews']['methods']['result']['verdict_reason']
	assert cut.endswith('Need') and reason.endswith('Needs a sampling plan.')
