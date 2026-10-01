"""Where each call and concept has got to.

The collision guard is the part worth protecting: a concept id is only unique
inside its own run, so a decision has to name both halves or deciding on one
run's first concept silently marks every other run's first concept too.
"""

import pytest

from grant_recommender import lifecycle

CALL = {'topic_code': 'OPEN-1'}


def a_round(job_id, concept_id, outcome, finished_at):
	return {
		'id': f'round-{job_id}-{concept_id}',
		'title': 'x',
		'topic_code': 'OPEN-1',
		'origin': f'concept {concept_id} of run {job_id}',
		'finished_at': finished_at,
		'outcome': outcome,
	}


def an_event(kind, target_id, target_type='concept'):
	return {'kind': kind, 'target_type': target_type, 'target_id': target_id}


def test_a_concept_key_names_the_run_as_well_as_the_concept():
	assert lifecycle.concept_key('abc123', '1') == 'abc123/1'


def test_deciding_on_one_runs_concept_does_not_mark_anothers(monkeypatch):
	"""Both runs number their first concept '1'."""
	monkeypatch.setattr(lifecycle.decisions, 'load', lambda: [an_event('develop', 'runA/1')])

	decided = lifecycle.decided()

	assert decided == {'runA/1': 'pursued'}
	assert 'runB/1' not in decided


def test_the_latest_decision_wins_and_an_undo_clears_it(monkeypatch):
	monkeypatch.setattr(
		lifecycle.decisions,
		'load',
		lambda: [
			an_event('save', 'j/1'),
			an_event('dismiss', 'j/1'),
			an_event('save', 'j/2'),
			an_event('unsave', 'j/2'),
		],
	)

	# The log is append-only, so an undo is a later event, never a deletion.
	assert lifecycle.decided() == {'j/1': 'dropped'}


def test_decisions_about_calls_are_not_read_as_decisions_about_concepts(monkeypatch):
	monkeypatch.setattr(
		lifecycle.decisions, 'load', lambda: [an_event('develop', 'OPEN-1', target_type='call')]
	)

	assert lifecycle.decided() == {}


def test_a_review_is_matched_to_its_concept_through_the_origin_line(monkeypatch):
	monkeypatch.setattr(
		lifecycle.reviews, 'list_rounds', lambda: [a_round('j1', '2', 'revise', '2030-01-22')]
	)

	assert lifecycle.reviewed()['j1/2']['outcome'] == 'revise'


def test_the_newest_round_is_the_one_reported(monkeypatch):
	monkeypatch.setattr(
		lifecycle.reviews,
		'list_rounds',
		# list_rounds sorts newest first.
		lambda: [
			a_round('j1', '1', 'fund', '2030-01-22'),
			a_round('j1', '1', 'reject', '2026-01-01'),
		],
	)

	assert lifecycle.reviewed()['j1/1']['outcome'] == 'fund'


def test_a_round_with_no_parsable_origin_is_skipped_not_raised(monkeypatch):
	stray = a_round('j1', '1', 'fund', '2030-01-22') | {'origin': 'typed in by hand'}
	monkeypatch.setattr(lifecycle.reviews, 'list_rounds', lambda: [stray])

	assert lifecycle.reviewed() == {}


@pytest.mark.parametrize(
	('snapshot', 'expected'),
	[
		({}, (False, False, False, False)),
		({'read': {'OPEN-1'}}, (True, False, False, False)),
		(
			{'concepts': {'OPEN-1': [{'job_id': 'j', 'concept': {'concept_id': '1'}}]}},
			(False, True, False, False),
		),
	],
)
def test_call_progress_reports_each_stage_separately(snapshot, expected):
	snap = {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()} | snapshot

	assert lifecycle.call_progress(CALL, snap) == expected


def test_a_reviewed_or_decided_concept_lights_its_calls_stage():
	item = {'job_id': 'j', 'concept': {'concept_id': '1'}}
	snap = {
		'concepts': {'OPEN-1': [item]},
		'reviewed': {'j/1': {'outcome': 'fund'}},
		'decided': {'j/1': 'pursued'},
		'read': set(),
	}

	assert lifecycle.call_progress(CALL, snap) == (False, True, True, True)


def test_a_decision_outranks_a_review_because_it_happens_after_it():
	item = {'job_id': 'j', 'concept': {'concept_id': '1'}}
	snap = {
		'concepts': {},
		'reviewed': {'j/1': {'outcome': 'reject'}},
		'decided': {},
		'read': set(),
	}

	assert lifecycle.concept_state(item, snap) == 'panel: reject'

	snap['decided']['j/1'] = 'pursued'
	assert lifecycle.concept_state(item, snap) == 'pursued'


def test_an_untouched_concept_says_so():
	item = {'job_id': 'j', 'concept': {'concept_id': '9'}}
	snap = {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()}

	assert lifecycle.concept_state(item, snap) == 'not reviewed'


def test_an_undo_clears_only_its_own_family(monkeypatch):
	"""Parking and pursuing are not exclusive. Keyed on the target alone, an
	unsave also erased the pursue, and its draft went on existing with nothing
	in the panel pointing at it."""
	monkeypatch.setattr(
		lifecycle.decisions,
		'load',
		lambda: [
			an_event('develop', 'j/1'),
			an_event('save', 'j/1'),
			an_event('unsave', 'j/1'),
		],
	)

	assert lifecycle.decided() == {'j/1': 'pursued'}


def test_the_latest_standing_decision_is_the_one_reported(monkeypatch):
	monkeypatch.setattr(
		lifecycle.decisions,
		'load',
		lambda: [an_event('develop', 'j/1'), an_event('dismiss', 'j/1')],
	)
	assert lifecycle.decided() == {'j/1': 'dropped'}

	monkeypatch.setattr(
		lifecycle.decisions,
		'load',
		lambda: [an_event('dismiss', 'j/1'), an_event('develop', 'j/1')],
	)
	assert lifecycle.decided() == {'j/1': 'pursued'}


def test_a_round_where_every_reviewer_failed_is_not_a_review(monkeypatch):
	"""consolidate() returns outcome None when nothing succeeded; counting it
	rendered the concept's state as the literal string 'panel: None'."""
	dead = a_round('j1', '1', None, '2030-01-22')
	monkeypatch.setattr(lifecycle.reviews, 'list_rounds', lambda: [dead])

	assert lifecycle.reviewed() == {}
	assert (
		lifecycle.concept_state(
			{'job_id': 'j1', 'concept': {'concept_id': '1'}},
			{'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()},
		)
		== 'not reviewed'
	)


def test_a_concept_id_with_spaces_still_matches_its_round(monkeypatch):
	"""concept_id is a model-produced string, not a number."""
	monkeypatch.setattr(
		lifecycle.reviews,
		'list_rounds',
		lambda: [a_round('run9', '1 (main)', 'fund', '2030-01-22')],
	)

	assert lifecycle.reviewed()['run9/1 (main)']['outcome'] == 'fund'


def test_concept_runs_takes_only_succeeded_jobs_newest_first(monkeypatch, tmp_path):
	import json

	monkeypatch.setattr(lifecycle, 'JOBS_DIR', tmp_path)

	def write(name, payload):
		(tmp_path / name).write_text(json.dumps(payload))

	write(
		'a.json',
		{
			'id': 'a',
			'topic_code': 'OPEN-1',
			'status': 'succeeded',
			'created_at': '2026-01-01',
			'concepts': [{'concept_id': '1', 'title': 'older'}],
		},
	)
	write(
		'b.json',
		{
			'id': 'b',
			'topic_code': 'OPEN-1',
			'status': 'succeeded',
			'created_at': '2026-05-01',
			'concepts': [{'concept_id': '1', 'title': 'newer'}],
			'archive_context': {'query': 'x'},
		},
	)
	write(
		'c.json',
		{
			'id': 'c',
			'topic_code': 'OPEN-1',
			'status': 'running',
			'created_at': '2030-01-01',
			'concepts': [{'concept_id': '1', 'title': 'unfinished'}],
		},
	)
	write('d.json', {'id': 'd', 'status': 'succeeded', 'created_at': '2030-01-02', 'concepts': []})
	(tmp_path / 'broken.json').write_text('{not json')

	runs = lifecycle.concept_runs()

	assert [c['concept']['title'] for c in runs['OPEN-1']] == ['newer', 'older']
	assert [c['archive'] for c in runs['OPEN-1']] == [True, False]
	assert list(runs) == ['OPEN-1']


def test_a_half_written_decision_row_does_not_stop_the_app(tmp_path, monkeypatch):
	"""Two processes append to this file; one killed mid-write left a partial
	line that made every later launch raise."""
	path = tmp_path / 'decisions.jsonl'
	path.write_text('{"kind": "save", "target_type": "concept", "target_id": "j/1"}\n{"kind": "dis')
	monkeypatch.setattr(lifecycle.decisions, 'DECISIONS_PATH', path)

	assert lifecycle.decided() == {'j/1': 'parked'}


def test_a_round_missing_its_consolidated_block_is_skipped(tmp_path, monkeypatch):
	import json

	monkeypatch.setattr(lifecycle.reviews, 'ROOT', tmp_path)
	good = tmp_path / 'good'
	good.mkdir()
	(good / 'round.json').write_text(
		json.dumps(
			{
				'id': 'good',
				'finished_at': '2030-01-22',
				'document': {'title': 't', 'topic_code': 'OPEN-1', 'origin': 'concept 1 of run j1'},
				'consolidated': {'outcome': 'fund'},
			}
		)
	)
	killed = tmp_path / 'killed'
	killed.mkdir()
	(killed / 'round.json').write_text(json.dumps({'id': 'killed', 'document': {}}))

	assert [r['id'] for r in lifecycle.reviews.list_rounds()] == ['good']
	assert 'j1/1' in lifecycle.reviewed()
