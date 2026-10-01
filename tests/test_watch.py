"""The watch loop's decision rule. plan() is pure, so these need no model,
no network and no server."""

import threading

from grant_recommender import watch

CONFIRMED = [
	{
		'id': 'a1',
		'component': 'methods',
		'claim': 'long-term CGM glucose forecasting',
		'confidence': 'confirmed',
		'included': True,
	},
]
INFERRED = [{**CONFIRMED[0], 'confidence': 'inferred'}]

ELIGIBLE = {
	'topic_code': 'OPEN-1',
	'scope_text': 'long-term CGM glucose forecasting for type 2 diabetes',
	'deadlines': ['2099-01-01T17:00:00'],
}
OTHER = {
	'topic_code': 'OPEN-2',
	'scope_text': 'battery chemistry for grid storage',
	'deadlines': ['2099-01-01T17:00:00'],
}
PASSED = {
	'topic_code': 'CLOSED-1',
	'scope_text': 'long-term CGM glucose forecasting',
	'deadlines': ['2019-01-01T17:00:00'],
}


def test_idle_while_a_run_is_in_progress():
	"""A 12B model takes minutes; a tick must not queue a second run."""
	action = watch.plan([ELIGIBLE], CONFIRMED, [{'topic_code': 'OTHER', 'status': 'running'}])
	assert action['action'] == 'idle'
	assert 'in progress' in action['reason']


def test_idle_without_a_confirmed_capability():
	"""Inferred-only portfolio must not drive autonomous generation -- same
	rule concepts.start_job enforces for the human path."""
	action = watch.plan([ELIGIBLE], INFERRED, [])
	assert action['action'] == 'idle'
	assert 'confirmed' in action['reason']


def test_skips_calls_that_already_have_a_run():
	action = watch.plan([ELIGIBLE], CONFIRMED, [{'topic_code': 'OPEN-1', 'status': 'succeeded'}])
	assert action['action'] == 'idle'


def test_never_develops_a_passed_deadline():
	action = watch.plan([PASSED], CONFIRMED, [])
	assert action['action'] == 'idle'


def test_develops_the_best_open_call_as_a_trial_when_eligibility_is_uncertain():
	action = watch.plan([ELIGIBLE, OTHER], CONFIRMED, [])
	assert action['action'] == 'develop'
	assert action['topic_code'] == 'OPEN-1'  # better fit than the battery call
	# The deterministic gate cannot clear an unstructured call outright, so the
	# run must be marked trial rather than presented as a cleared decision.
	assert action['trial'] is (action['eligibility'] != 'eligible')


def test_fit_floor_keeps_irrelevant_notices_out_of_generation():
	"""A ministry feed is mostly press releases. Sharing no term with the
	portfolio must not be developed just for being the only candidate."""
	unrelated = {
		'topic_code': 'NOTICE-1',
		'scope_text': 'Ausschreibung zur Foerderung von Weinbau und Kellerwirtschaft',
		'deadlines': [],
	}
	assert watch.plan([unrelated], CONFIRMED, [])['action'] == 'idle'


def test_fit_floor_is_not_cleared_by_shared_stop_words():
	"""keyword_overlap counts every token, so a notice sharing only 'in', 'the'
	and 'and' with the claim cleared the floor and got minutes of model time."""
	portfolio = [
		{**CONFIRMED[0], 'claim': 'long-term CGM glucose forecasting in the clinic and at home'}
	]
	unrelated = {
		'topic_code': 'NOTICE-2',
		'scope_text': 'Support for viticulture and cellar management in the Rhine valley',
		'deadlines': [],
	}
	assert watch.plan([unrelated], portfolio, [])['action'] == 'idle'
	# The same claim still develops a call that shares real words with it.
	action = watch.plan([unrelated, ELIGIBLE], portfolio, [])
	assert (action['action'], action['topic_code']) == ('develop', 'OPEN-1')


def test_discovery_imports_only_notices_not_already_watched(monkeypatch, real_discover):
	"""The step the loop needs to *find* calls: a refreshed notice that is not
	on the watch list yet gets imported, an already-known one does not."""
	imported = []
	monkeypatch.setattr(watch, 'PROVIDERS', ('dfg',))
	monkeypatch.setattr(
		watch.german_funding,
		'refresh',
		lambda provider: {'items': [{'id': 'DFG-new'}, {'id': 'DFG-known'}], 'error': None},
	)
	monkeypatch.setattr(
		watch.german_funding, 'watched_calls', lambda: [{'topic_code': 'DFG-known'}]
	)

	def fake_import(provider, notice_id):
		imported.append(notice_id)
		return {'topic_code': notice_id, 'scope_text': 'x', 'deadlines': []}

	monkeypatch.setattr(watch.german_funding, 'import_notice', fake_import)
	sources, calls = watch.discover(max_imports=5)
	assert imported == ['DFG-new']
	assert [c['topic_code'] for c in calls] == ['DFG-new']
	assert sources['dfg'] == '2 notices'


def test_discovery_caps_imports_per_tick(monkeypatch, real_discover):
	"""Each import is a page fetch; a fresh feed must not burst 25 of them."""
	monkeypatch.setattr(watch, 'PROVIDERS', ('dfg',))
	monkeypatch.setattr(
		watch.german_funding,
		'refresh',
		lambda provider: {'items': [{'id': f'DFG-{i}'} for i in range(10)], 'error': None},
	)
	monkeypatch.setattr(watch.german_funding, 'watched_calls', list)
	monkeypatch.setattr(
		watch.german_funding,
		'import_notice',
		lambda provider, nid: {'topic_code': nid, 'scope_text': 'x', 'deadlines': []},
	)
	_, calls = watch.discover(max_imports=3)
	assert len(calls) == 3


def test_tick_does_no_network_work_while_a_run_is_in_progress(monkeypatch):
	monkeypatch.setattr(
		watch.concepts, 'list_jobs', lambda: [{'topic_code': 'X', 'status': 'running'}]
	)

	def explode(*args, **kwargs):
		raise AssertionError('must not touch the network while a run is in flight')

	monkeypatch.setattr(watch, 'discover', explode)
	action = watch.tick([], CONFIRMED)
	assert action['action'] == 'idle'


def test_run_stops_after_the_requested_iterations():
	seen = []
	watch.run(
		list,
		lambda: CONFIRMED,
		interval_s=0,
		iterations=3,
		on_tick=seen.append,
	)
	assert len(seen) == 3


def test_run_survives_a_failing_source_and_keeps_going():
	def boom():
		raise RuntimeError('source down')

	seen = []
	watch.run(boom, lambda: CONFIRMED, interval_s=0, iterations=2, on_tick=seen.append)
	assert [r['action'] for r in seen] == ['error', 'error']
	assert 'source down' in seen[0]['reason']


def test_stop_event_ends_the_loop():
	stop = threading.Event()
	stop.set()
	seen = []
	watch.run(list, lambda: CONFIRMED, interval_s=0, stop=stop, on_tick=seen.append)
	assert seen == []


def test_tick_develops_a_discovered_call_and_records_who_did_it(monkeypatch, tmp_path):
	"""The develop branch had no coverage: the autouse discover stub made every
	run of `run()` idle, and the assertions only counted ticks."""
	started, recorded = [], []
	monkeypatch.setattr(watch.concepts, 'list_jobs', list)
	monkeypatch.setattr(
		watch,
		'discover',
		lambda max_imports, on_event=None: (
			{'dfg': '1 notice'},
			[
				{
					'topic_code': 'DFG-new',
					'scope_text': 'long-term CGM glucose forecasting',
					'deadlines': ['2099-01-01T17:00:00'],
				}
			],
		),
	)
	monkeypatch.setattr(watch.concepts, 'connection_status', lambda: {'ready': True})
	monkeypatch.setattr(
		watch.concepts, 'start_job', lambda *a, **k: started.append(a) or {'id': 'job-1'}
	)
	monkeypatch.setattr(
		watch.decisions, 'record', lambda *a, **k: recorded.append((a, k)) or {'id': 'd1'}
	)

	action = watch.tick([], CONFIRMED)

	assert action['action'] == 'develop'
	assert action['topic_code'] == 'DFG-new'
	assert action['job_id'] == 'job-1'
	assert started, 'no concept run was started'
	# Its own decisions stay distinguishable from the researcher's.
	(args, kwargs) = recorded[0]
	assert args[:3] == ('develop', 'call', 'DFG-new')
	assert kwargs['actor'] == 'watcher'


def test_tick_waits_instead_of_burning_a_call_when_the_model_is_unreachable(monkeypatch):
	"""A failed run retires its call, so starting one with no model behind it
	would quietly cost the researcher their best match. It must wait."""
	started, recorded = [], []
	monkeypatch.setattr(watch.concepts, 'list_jobs', list)
	monkeypatch.setattr(
		watch,
		'discover',
		lambda max_imports, on_event=None: (
			{'dfg': '1 notice'},
			[
				{
					'topic_code': 'DFG-new',
					'scope_text': 'long-term CGM glucose forecasting',
					'deadlines': ['2099-01-01T17:00:00'],
				}
			],
		),
	)
	monkeypatch.setattr(
		watch.concepts,
		'connection_status',
		lambda: {'ready': False, 'detail': 'Cannot reach this server.'},
	)
	monkeypatch.setattr(watch.concepts, 'start_job', lambda *a, **k: started.append(a))
	monkeypatch.setattr(watch.decisions, 'record', lambda *a, **k: recorded.append(a))

	action = watch.tick([], CONFIRMED)

	assert action['action'] == 'idle'
	assert 'Cannot reach this server.' in action['reason']
	# Discovery still ran, and nothing was spent.
	assert action['imported'] == ['DFG-new']
	assert not started
	assert not recorded
	# The log line must say why, not name the call it is waiting on.
	assert 'topic_code' not in action


def _discovers(code):
	return lambda max_imports, on_event=None: (
		{'dfg': '1 notice'},
		[
			{
				'topic_code': code,
				'scope_text': 'long-term CGM glucose forecasting',
				'deadlines': ['2099-01-01T17:00:00'],
			}
		],
	)


def test_tick_skips_a_call_the_researcher_dismissed(monkeypatch):
	started = []
	monkeypatch.setattr(watch.concepts, 'list_jobs', list)
	monkeypatch.setattr(watch, 'discover', _discovers('DFG-new'))
	monkeypatch.setattr(watch.concepts, 'connection_status', lambda: {'ready': True})
	monkeypatch.setattr(
		watch.concepts, 'start_job', lambda *a, **k: started.append(a) or {'id': 'job-1'}
	)

	watch.decisions.record('dismiss', 'call', 'DFG-new')
	assert watch.tick([], CONFIRMED)['action'] == 'idle'
	assert not started

	# Restoring it makes it a candidate again.
	watch.decisions.record('undo_dismiss', 'call', 'DFG-new')
	assert watch.tick([], CONFIRMED)['topic_code'] == 'DFG-new'
	assert len(started) == 1


def test_a_run_started_during_discovery_keeps_the_tick_idle(monkeypatch):
	"""Discovery takes seconds. A run the researcher starts meanwhile must stop
	this tick from starting a second one."""
	started, jobs = [], []

	def discover(max_imports, on_event=None):
		jobs.append({'id': 'ui-run', 'topic_code': 'DFG-other', 'status': 'running'})
		return _discovers('DFG-new')(max_imports)

	monkeypatch.setattr(watch.concepts, 'list_jobs', lambda: list(jobs))
	monkeypatch.setattr(watch, 'discover', discover)
	monkeypatch.setattr(watch.concepts, 'connection_status', lambda: {'ready': True})
	monkeypatch.setattr(watch.concepts, 'start_job', lambda *a, **k: started.append(a))

	assert watch.tick([], CONFIRMED)['action'] == 'idle'
	assert not started


def test_fit_floor_needs_two_shared_content_words():
	"""Every DFG notice ends with the same data-protection paragraph, and its
	"confidential treatment" alone made any notice look relevant to a claim
	about treatment."""
	portfolio = [
		{**CONFIRMED[0], 'claim': 'Wearable sensors tracked treatment response over six months'}
	]
	press_release = {
		'topic_code': 'DFG-press',
		'scope_text': 'German-Canadian training groups. We take the protection of your '
		'personal data and its confidential treatment extremely seriously.',
		'deadlines': [],
	}
	call = {
		'topic_code': 'DFG-call',
		'scope_text': 'Wearable sensors that measure treatment response in type 2 diabetes.',
		'deadlines': [],
	}

	assert watch.plan([press_release], portfolio, [])['action'] == 'idle'
	action = watch.plan([press_release, call], portfolio, [])
	assert (action['action'], action['topic_code']) == ('develop', 'DFG-call')
