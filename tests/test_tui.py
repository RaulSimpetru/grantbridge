"""Review-panel TUI: document discovery and the streaming delta logic.

Textual's run_test drives the real app headlessly. It is async, but the suite
has no asyncio plugin, so each case wraps its own asyncio.run rather than
adding a dependency for four tests.
"""

import asyncio
import json

from grant_recommender import reviews, tui

JOB = {
	'id': 'job1',
	'status': 'succeeded',
	'topic_code': 'FAKE-2027',
	'created_at': '2030-01-21T10:00:00+00:00',
	'model': 'gemma4:12b-mlx',
	'concepts': [
		{'concept_id': '1', 'title': 'First concept', 'problem': 'p'},
		{'concept_id': '2', 'title': 'Second concept', 'problem': 'p'},
	],
	'evidence': {},
}


def test_only_succeeded_runs_with_concepts_are_reviewable(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	(tmp_path / 'running.json').write_text(json.dumps({**JOB, 'id': 'job2', 'status': 'running'}))
	(tmp_path / 'empty.json').write_text(json.dumps({**JOB, 'id': 'job3', 'concepts': None}))
	(tmp_path / 'broken.json').write_text('not json at all')
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)

	found = tui.reviewable_documents()

	# One entry per concept of the one usable run; a corrupt file is skipped
	# rather than crashing the app on launch.
	assert [d['concept_id'] for d in found] == ['1', '2']
	assert all(d['topic_code'] == 'FAKE-2027' for d in found)


def test_documents_are_newest_first(monkeypatch, tmp_path):
	(tmp_path / 'old.json').write_text(
		json.dumps({**JOB, 'id': 'old', 'created_at': '2026-01-01T00:00:00+00:00'})
	)
	(tmp_path / 'new.json').write_text(
		json.dumps({**JOB, 'id': 'new', 'created_at': '2030-01-01T00:00:00+00:00'})
	)
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)

	assert next(d['job']['id'] for d in tui.reviewable_documents()) == 'new'


def test_streaming_renders_the_whole_text_and_ignores_non_growth(monkeypatch, tmp_path):
	"""The pane renders the accumulated text; a repeated poll with no new
	tokens must not trigger a redraw."""
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	written: list[str] = []
	monkeypatch.setattr(tui.ReviewPane, 'set_text', lambda self, text: written.append(text))

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			app._on_text('methods', 'Hello')
			app._on_text('methods', 'Hello world')
			app._on_text('methods', 'Hello world')  # no growth -> nothing written

	asyncio.run(scenario())
	assert written == ['Hello', 'Hello world']


def test_panel_shows_one_pane_per_role_and_lists_documents(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	seen = {}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			seen['roles'] = {p.role_key for p in app.query(tui.ReviewPane)}
			seen['documents'] = len(app.documents)

	asyncio.run(scenario())
	assert seen['roles'] == set(reviews.ROLES)
	assert seen['documents'] == 2


def test_a_failed_reviewer_is_shown_not_swallowed(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	seen = {}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			app._on_done('methods', {'status': 'failed', 'error': 'model dropped'})
			await pilot.pause()
			seen['status'] = str(app.query_one('#status-methods').content)

	asyncio.run(scenario())
	assert 'failed' in seen['status']


def test_verdict_colours_the_pane_status(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	seen = {}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			app._on_done(
				'fit',
				{
					'status': 'succeeded',
					'result': {'verdict': 'reject', 'verdict_reason': 'Out of scope.'},
				},
			)
			await pilot.pause()
			label = app.query_one('#status-fit')
			seen['text'] = str(label.content)
			seen['classes'] = set(label.classes)

	asyncio.run(scenario())
	assert 'REJECT' in seen['text']
	assert 'reject' in seen['classes']


def test_late_callbacks_after_teardown_do_not_raise(monkeypatch, tmp_path):
	"""Reviewer threads outlive a quit. A callback landing after the screen is
	gone must be a no-op, not an exception inside a worker thread."""
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	app = tui.PanelApp()

	async def scenario():
		async with app.run_test() as pilot:
			await pilot.pause()

	asyncio.run(scenario())

	# The app is torn down; these are exactly the calls an in-flight reviewer
	# would still make.
	app._on_text('methods', 'text arriving late')
	app._on_done(
		'methods', {'status': 'succeeded', 'result': {'verdict': 'fund', 'verdict_reason': 'ok'}}
	)
	app._finish(
		{
			'id': 'r1',
			'consolidated': {
				'outcome': 'fund',
				'tally': {},
				'unsupported_claims': [],
				'questions': [],
				'weaknesses': [],
				'failed_roles': [],
			},
		}
	)
	app._fail('late failure')


def test_finished_pane_shows_prose_not_the_raw_json(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	shown = {}

	result = {
		'verdict': 'revise',
		'verdict_reason': 'Needs a sampling plan.',
		'strengths': ['Clear hypothesis'],
		'weaknesses': ['Sample size unstated'],
		'questions_for_applicant': ['Which cohort?'],
		'unsupported_claims': [{'claim': 'First use', 'why_unsupported': 'No passage supports it'}],
	}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			monkeypatch.setattr(
				tui.ReviewPane, 'set_text', lambda self, text, **_: shown.__setitem__('text', text)
			)
			app._on_done('methods', {'status': 'succeeded', 'result': result})
			await pilot.pause()

	asyncio.run(scenario())
	body = shown['text']
	assert 'Clear hypothesis' in body
	assert 'No passage supports it' in body
	# The JSON the model streamed must not be what the reader is left with.
	assert '"verdict"' not in body


def test_each_concept_names_the_call_it_belongs_to(monkeypatch, tmp_path):
	"""Label parses markup, so "[FAKE-2027]" was read as a style and dropped."""
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	seen = {}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			seen['labels'] = [label.visual.plain for label in app.query('#doclist Label')]

	asyncio.run(scenario())
	assert seen['labels'] == ['First concept\n[FAKE-2027]', 'Second concept\n[FAKE-2027]']


def test_the_panel_tally_reads_as_counts_not_a_python_dict(monkeypatch, tmp_path):
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	seen = {}
	claim = {
		'raised_by': 'fit',
		'claim': 'The budget cited as [CALL_TEXT]',
		'why_unsupported': 'No passage gives a budget',
	}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			app._finish(
				{
					'id': 'r1',
					'consolidated': {
						'outcome': 'revise',
						'tally': {'fund': 1, 'revise': 2, 'reject': 0},
						'unsupported_claims': [claim],
						'questions': [],
						'weaknesses': [],
						'failed_roles': [],
					},
				}
			)
			await pilot.pause()
			seen['summary'] = app.query_one('#summary-body').visual.plain

	asyncio.run(scenario())
	assert 'PANEL OUTCOME: REVISE   fund 1   revise 2   reject 0' in seen['summary']
	assert "'fund'" not in seen['summary']
	# Model text cites passages in brackets, which markup would have eaten.
	assert '[CALL_TEXT]' in seen['summary']


def test_the_verdict_reason_is_shown_in_full_from_the_top_of_its_pane(monkeypatch, tmp_path):
	"""It sat on the one-line status label, which does not wrap, so all but its
	first few words ran off the pane's edge."""
	(tmp_path / 'ok.json').write_text(json.dumps(JOB))
	monkeypatch.setattr(tui, 'JOBS_DIR', tmp_path)
	reason = 'The sampling plan is missing, so the effect size cannot be judged. ' * 8
	seen = {}

	async def scenario():
		app = tui.PanelApp()
		async with app.run_test() as pilot:
			await pilot.pause()
			app._on_text('methods', '{"strengths": ["x"]}\n' * 80)
			await pilot.pause()
			seen['streamed_at'] = app.query_one('#scroll-methods').scroll_y
			app._on_done(
				'methods',
				{'status': 'succeeded', 'result': {'verdict': 'revise', 'verdict_reason': reason}},
			)
			await pilot.pause()
			seen['status'] = str(app.query_one('#status-methods').content)
			seen['pane'] = str(app.query_one('#log-methods').content)
			seen['finished_at'] = app.query_one('#scroll-methods').scroll_y

	asyncio.run(scenario())
	assert seen['status'] == 'REVISE'
	assert seen['pane'].startswith(reason)
	# The stream followed its end; the finished review opens at its start.
	assert seen['streamed_at'] > 0
	assert seen['finished_at'] == 0
