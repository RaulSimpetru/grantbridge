import json
import time

import pytest

from grant_recommender import concepts, eligibility

CALL = {
	'topic_code': 'FAKE-CODE-2024',
	'title': 'A fake call',
	'scope_text': 'Specific Challenge: do the work. Scope: build the thing.',
	'topic_conditions': 'See General Annexes.',
	'deadlines': ['2099-09-13T00:00:00+00:00'],
}

PORTFOLIO = [
	{
		'component': 'methods',
		'claim': 'Optical coherence tomography',
		'source': 'CV',
		'confidence': 'confirmed',
		'included': True,
	},
	{
		'component': 'career_eligibility',
		'claim': 'PhD date unknown',
		'source': None,
		'confidence': 'missing',
		'included': True,
	},
]


def _fake_concept_set(allowed_ids, call_ids, researcher_ids, titles=('One', 'Two', 'Three')):
	return {
		'concepts': [
			{
				'concept_id': str(i + 1),
				'title': titles[i],
				'problem': 'p',
				'central_hypothesis': f'hypothesis {titles[i]}',
				'call_fit': 'fits',
				'researcher_fit': 'fits',
				'objectives': ['a', 'b'],
				'required_assets_and_collaborators': ['needs a partner'],
				'novelty': 'novel',
				'principal_feasibility_risk': 'risk',
				'call_evidence': [{'source_id': call_ids[0], 'claim_supported': 'x'}],
				'researcher_evidence': [{'source_id': researcher_ids[0], 'claim_supported': 'y'}],
			}
			for i in range(3)
		]
	}


def _fake_critique():
	return {
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


class _FakeResponse:
	"""Stands in for both Ollama response shapes: one JSON object when
	stream=False, and newline-delimited chunks over iter_lines when the product
	streams so the UI can show the model working."""

	status_code = 200

	def __init__(self, payload):
		self._payload = payload

	def raise_for_status(self):
		pass

	def json(self):
		return self._payload

	def iter_lines(self):
		import json as _json

		content = self._payload['message']['content']
		# Split into a few chunks so the streaming accumulator is genuinely
		# exercised rather than handed the whole document in one piece.
		size = max(1, len(content) // 4)
		parts = [content[i : i + size] for i in range(0, len(content), size)]
		for part in parts:
			yield _json.dumps({'message': {'content': part}, 'done': False}).encode()
		yield _json.dumps({'message': {'content': ''}, 'done': True}).encode()


def _happy_path_post(url, json, timeout, allow_redirects=False, stream=False):
	schema = json['format']
	props = schema.get('properties', {})
	if 'concept_critiques' in props:
		content = _fake_critique()
	else:
		allowed = _allowed_ids_from_schema(schema)
		content = _fake_concept_set(allowed, allowed['call'], allowed['researcher'])
	return _FakeResponse({'message': {'content': __import__('json').dumps(content)}})


def _allowed_ids_from_schema(schema):
	enum = schema['properties']['concepts']['items']['properties']['call_evidence']['items'][
		'properties'
	]['source_id'].get('enum', [])
	researcher_enum = schema['properties']['concepts']['items']['properties'][
		'researcher_evidence'
	]['items']['properties']['source_id'].get('enum', [])
	return {'call': enum, 'researcher': researcher_enum}


def _wait_for(job_id, timeout=5):
	deadline = time.time() + timeout
	while time.time() < deadline:
		job = concepts.get_job(job_id)
		if job['status'] != 'running':
			return job
		time.sleep(0.02)
	raise TimeoutError('job did not finish in time')


def test_invented_critique_cannot_erase_research_intent(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	monkeypatch.setattr(concepts, '_model_digest', lambda connection: None)
	seen = []

	def post(url, json, timeout, allow_redirects=False, stream=False):
		seen.append(json)
		if 'concept_critiques' in json['format'].get('properties', {}):
			critique = _fake_critique()
			critique['revision_priorities'] = [
				'INVENTED RULE: must remove dietary coaching and patient reminders'
			]
			return _FakeResponse({'message': {'content': __import__('json').dumps(critique)}})
		allowed = _allowed_ids_from_schema(json['format'])
		titles = (
			('Dietary coaching for diabetes', 'Reminder design', 'Glycaemic outcomes')
			if len(seen) == 1
			else ('Signal extraction', 'Sensor calibration', 'Method comparison')
		)
		return _FakeResponse(
			{
				'message': {
					'content': __import__('json').dumps(
						_fake_concept_set(allowed, allowed['call'], allowed['researcher'], titles)
					)
				}
			}
		)

	monkeypatch.setattr(concepts.requests, 'post', post)
	job = concepts.start_job(CALL, PORTFOLIO, trial=True)
	final = _wait_for(job['id'])
	assert final['status'] == 'succeeded'
	assert 'INVENTED RULE' not in seen[2]['messages'][1]['content']
	assert 'INVENTED RULE' in final['critique']['revision_priorities'][0]
	assert final['concepts'][0]['title'] == 'Dietary coaching for diabetes'
	assert final['revision_audit']['restored_fields']


@pytest.fixture(autouse=True)
def _clear_jobs(monkeypatch, tmp_path):
	concepts.JOBS.clear()
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews' / 'eligibility.json')
	eligibility.record_review(
		CALL, PORTFOLIO, 'eligible', 'Test requirements met', 'Test source section 4'
	)
	yield
	concepts.JOBS.clear()


def test_build_generation_packet_excludes_missing_confidence_assertions():
	packet = concepts.build_generation_packet(job_id='j1', call=CALL, portfolio=PORTFOLIO)

	assert 'CALL_CONDITIONS' in packet['allowed_evidence_ids']
	assert packet['allowed_evidence_ids'].count('PROFILE_001') == 1
	assert 'PhD date unknown' not in packet['user_prompt']
	assert 'Optical coherence tomography' in packet['user_prompt']


def test_build_generation_packet_rejects_an_all_missing_portfolio():
	only_missing = [{**PORTFOLIO[1]}]
	with pytest.raises(ValueError, match='usable portfolio assertions'):
		concepts.build_generation_packet(job_id='j1', call=CALL, portfolio=only_missing)


def test_trial_context_and_product_rules_survive_all_actual_stage_requests(monkeypatch, tmp_path):
	from grant_recommender.product_grounding import RULES, VERSION

	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	monkeypatch.setattr(concepts, '_model_digest', lambda connection: None)
	requests_seen = []

	def post(url, json, timeout, allow_redirects=False, stream=False):
		requests_seen.append(json)
		return _happy_path_post(url, json, timeout)

	monkeypatch.setattr(concepts.requests, 'post', post)
	call = {
		**CALL,
		'call_title': 'Example programme: Track A',
		'deadlines': ['2020-08-20'],
	}
	job = concepts.start_job(call, PORTFOLIO, trial=True)
	assert _wait_for(job['id'])['status'] == 'succeeded'
	assert job['grounding_policy_version'] == VERSION
	assert len(requests_seen) == 3
	for request in requests_seen:
		assert RULES in request['messages'][0]['content']
		prompt = request['messages'][1]['content']
		assert 'EXPLORATORY TRIAL: applicant eligibility is NOT established' in prompt
		assert call['call_title'] in prompt
		assert '2020-08-20' in prompt


def test_grounding_rules_leave_the_base_prompt_unchanged():
	from grant_recommender import ideation
	from grant_recommender.product_grounding import RULES, apply_grounding_rules

	original = {'system_prompt': ideation.SYSTEM_PROMPT}
	guarded = apply_grounding_rules(original)
	assert RULES in guarded['system_prompt']
	assert RULES not in original['system_prompt']
	assert original['system_prompt'] == ideation.SYSTEM_PROMPT


def test_start_job_reaches_succeeded_with_all_five_stages(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	monkeypatch.setattr(concepts.requests, 'post', _happy_path_post)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)

	job = concepts.start_job(CALL, PORTFOLIO)
	final = _wait_for(job['id'])

	assert final['status'] == 'succeeded'
	assert [s['status'] for s in final['stages']] == ['succeeded'] * 5
	assert len(final['concepts']) == 3
	assert final['diversity'].keys() == {'1', '2', '3'}
	log_lines = (tmp_path / f'{job["id"]}.jsonl').read_text().splitlines()
	assert len(log_lines) == 3  # draft, critique, final


def test_start_job_fails_at_grounding_check_on_duplicate_titles(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)

	def bad_post(url, json, timeout, allow_redirects=False, stream=False):
		schema = json['format']
		if 'concept_critiques' in schema.get('properties', {}):
			return _FakeResponse(
				{'message': {'content': __import__('json').dumps(_fake_critique())}}
			)
		allowed = _allowed_ids_from_schema(schema)
		content = _fake_concept_set(
			allowed, allowed['call'], allowed['researcher'], titles=('Same', 'Same', 'Same')
		)
		return _FakeResponse({'message': {'content': __import__('json').dumps(content)}})

	monkeypatch.setattr(concepts.requests, 'post', bad_post)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)

	job = concepts.start_job(CALL, PORTFOLIO)
	final = _wait_for(job['id'])

	assert final['status'] == 'failed'
	stage = next(s for s in final['stages'] if s['name'] == 'check_eligibility_and_grounding')
	assert stage['status'] == 'failed'
	assert 'distinct' in stage['detail']


def test_start_job_fails_at_generate_when_ollama_is_unreachable(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)

	def unreachable(*a, **k):
		raise concepts.requests.exceptions.ConnectionError('connection refused')

	monkeypatch.setattr(concepts.requests, 'post', unreachable)
	monkeypatch.setattr(concepts.requests, 'get', unreachable)

	job = concepts.start_job(CALL, PORTFOLIO)
	final = _wait_for(job['id'])

	assert final['status'] == 'failed'
	assert final['stages'][0]['name'] == 'generate'
	assert final['stages'][0]['status'] == 'failed'
	assert 'connection refused' in final['error']
	assert all(s['status'] == 'pending' for s in final['stages'][1:])


def test_start_job_fails_instead_of_hanging_on_a_null_content_response(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	monkeypatch.setattr(
		concepts.requests,
		'post',
		lambda url, json, timeout, allow_redirects=False: _FakeResponse(
			{'message': {'content': None}}
		),
	)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)

	job = concepts.start_job(CALL, PORTFOLIO)
	final = _wait_for(job['id'])

	assert final['status'] == 'failed'
	assert final['stages'][0]['status'] == 'failed'
	assert 'TypeError' in final['error']


def test_start_job_persists_a_readable_json_snapshot_on_success(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	monkeypatch.setattr(concepts.requests, 'post', _happy_path_post)
	monkeypatch.setattr(
		concepts.requests,
		'get',
		lambda *a, **k: (_ for _ in ()).throw(concepts.requests.RequestException('no ollama')),
	)

	job = concepts.start_job(CALL, PORTFOLIO)
	_wait_for(job['id'])

	saved = json.loads((tmp_path / f'{job["id"]}.json').read_text())
	assert saved['status'] == 'succeeded'
	assert saved['id'] == job['id']
	assert 'created_at' in saved


def test_load_persisted_jobs_marks_a_stale_running_job_interrupted(monkeypatch, tmp_path):
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	stale = {
		'id': 'stalejob123',
		'topic_code': CALL['topic_code'],
		'status': 'running',
		'created_at': '2020-01-01T00:00:00+00:00',
		'retry_of': None,
		'seed_index': 0,
		'model': 'x',
		'model_digest': None,
		'stages': [
			{'name': n, 'status': 'pending', 'wall_time_seconds': None, 'detail': None}
			for n in concepts.STAGE_NAMES
		],
		'draft': None,
		'critique': None,
		'concepts': None,
		'diversity': None,
		'error': None,
		'evidence': {},
	}
	(tmp_path / 'stalejob123.json').write_text(json.dumps(stale))

	concepts._load_persisted_jobs()

	reloaded = concepts.get_job('stalejob123')
	assert reloaded['status'] == 'interrupted'
	on_disk = json.loads((tmp_path / 'stalejob123.json').read_text())
	assert on_disk['status'] == 'interrupted'


def test_diversity_check_is_pairwise_and_symmetric_keys():
	result = concepts.diversity_check(
		[
			{
				'concept_id': '1',
				'title': 'CGM analysis',
				'central_hypothesis': 'glucose signals help',
			},
			{'concept_id': '2', 'title': 'Battery recycling', 'central_hypothesis': 'unrelated'},
		]
	)
	assert set(result) == {'1', '2'}
	assert set(result['1']) == {'2'}


def test_product_rejects_duplicate_concept_ids_even_when_titles_differ():
	output = _fake_concept_set([], ['CALL_TEXT'], ['PROFILE_001'])
	output['concepts'][1]['concept_id'] = output['concepts'][0]['concept_id']
	with pytest.raises(ValueError, match='identifiers must be distinct'):
		concepts._validate_product_concepts(
			output,
			allowed_evidence_ids=['CALL_TEXT', 'PROFILE_001'],
			require_researcher_evidence=True,
		)


def test_streaming_accumulates_chunks_and_reports_progress(monkeypatch):
	"""The live console is only honest if the callback sees the text growing
	and the parsed result still matches the non-streaming path."""
	document = __import__('json').dumps({'ok': True, 'note': 'a' * 40})
	seen = []

	class _Streamed:
		status_code = 200

		def raise_for_status(self):
			pass

		def iter_lines(self):
			import json as _json

			for part in [document[:10], document[10:25], document[25:]]:
				yield _json.dumps({'message': {'content': part}, 'done': False}).encode()
			yield _json.dumps({'message': {'content': ''}, 'done': True}).encode()

	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _Streamed())
	parsed, meta = concepts._ollama_chat(
		{
			'system_prompt': 's',
			'user_prompt': 'u',
			'_connection': {'base_url': 'http://x', 'model': 'm'},
		},
		schema={},
		num_predict=10,
		seed=1,
		on_text=seen.append,
	)

	assert parsed == {'ok': True, 'note': 'a' * 40}
	assert len(seen) == 3
	assert seen[0] == document[:10]
	assert seen[-1] == document  # each callback carries the full text so far
	assert meta['request']['stream'] is True
	assert meta['response']['message']['content'] == document


def test_live_tail_is_dropped_once_a_stage_ends(monkeypatch, tmp_path):
	"""Progress, not a result: a finished stage must not keep a half-written
	copy of output the parsed record already holds."""
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path)
	job = concepts._new_job_state('j', 'CODE', 0, None, [])
	concepts._set_live(job, 'generate', 'partial output')
	assert job['stages'][0]['live'] == 'partial output'

	concepts._set_stage(job, 'generate', 'succeeded')
	assert job['stages'][0]['live'] is None


def test_a_control_character_inside_a_string_does_not_kill_the_run(monkeypatch):
	"""Models put literal newlines inside JSON strings. The strict parser
	rejected them, and one real concept run died at the critique stage with
	'Invalid control character at line 29' and lost the four stages after it."""

	class _Response:
		status_code = 200

		def raise_for_status(self):
			pass

		def json(self):
			return {
				'message': {'content': '{"summary": "line one\nline two", "n": 1}'},
				'done': True,
			}

	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _Response())

	parsed, _meta = concepts._ollama_chat(
		{
			'system_prompt': 'you are a reviewer',
			'user_prompt': 'critique this',
			'_connection': {'base_url': 'http://x', 'model': 'm'},
		},
		schema={'type': 'object'},
		num_predict=64,
		seed=1,
	)

	assert parsed['summary'] == 'line one\nline two'


def test_list_jobs_is_ordered_by_creation_not_by_job_id(monkeypatch):
	# JOBS is filled by globbing the store, so its insertion order is by job id --
	# random hex, unrelated to time. Callers take the last entry as the newest run.
	by_id = {
		'0000ffffffff': {
			'id': '0000ffffffff',
			'topic_code': 'T',
			'created_at': '2030-01-23T00:00:00+00:00',
		},
		'aaaa11112222': {
			'id': 'aaaa11112222',
			'topic_code': 'U',
			'created_at': '2030-01-30T00:00:00+00:00',
		},
		'ffff00000001': {
			'id': 'ffff00000001',
			'topic_code': 'T',
			'created_at': '2030-01-01T00:00:00+00:00',
		},
	}
	monkeypatch.setattr(concepts, 'JOBS', by_id)
	assert [j['id'] for j in concepts.list_jobs('T')] == ['ffff00000001', '0000ffffffff']
	assert concepts.list_jobs()[-1]['id'] == 'aaaa11112222'
