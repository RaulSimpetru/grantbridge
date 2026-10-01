from grant_recommender import decisions


def test_record_appends_and_load_returns_events_in_order(monkeypatch, tmp_path):
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')

	first = decisions.record('save', 'call', 'CALL-1')
	second = decisions.record('dismiss', 'call', 'CALL-2', reason='not eligible')

	events = decisions.load()
	assert [e['id'] for e in events] == [first['id'], second['id']]
	assert events[1]['reason'] == 'not eligible'
	assert 'T' in events[0]['created_at']


def test_load_returns_empty_list_when_no_file_exists(monkeypatch, tmp_path):
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'missing.jsonl')

	assert decisions.load() == []


def test_undo_is_a_new_event_not_a_rewrite(monkeypatch, tmp_path):
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', tmp_path / 'decisions.jsonl')

	decisions.record('save', 'call', 'CALL-1')
	decisions.record('unsave', 'call', 'CALL-1')

	events = decisions.load()
	assert [e['kind'] for e in events] == ['save', 'unsave']
