import json
from datetime import UTC, datetime

from grant_recommender import calls

# Trimmed to the fields _fetch_topic actually reads, shaped exactly like a real
# SEDIA topic record (DATASOURCE='SEDIA', type='1') captured live from a public
# Horizon 2020 topic.
REAL_SHAPED_METADATA = {
	'identifier': ['FAKE-CODE-2024'],
	'title': ['A fake but realistically-shaped topic'],
	'callIdentifier': ['H2020-FAKE-2023-2024'],
	'callTitle': ['FAKE CALL TITLE'],
	'deadlineDate': ['2024-09-13T00:00:00.000+0000'],
	'status': ['31094503'],
	'typesOfAction': ['Research and Innovation action'],
	'descriptionByte': ['<p>Scope: <b>do the work</b>.</p>'],
	'frameworkProgramme': ['31045243'],
	'programmePeriod': ['2021 - 2027'],
	'url': [
		'https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/FAKE-CODE-2024.json'
	],
}


class _FakeResponse:
	def __init__(self, results):
		self._results = results

	def raise_for_status(self):
		pass

	def json(self):
		return {'results': self._results}


def _reset_cache(monkeypatch, tmp_path):
	monkeypatch.setattr(calls, '_CACHE_PATH', tmp_path / 'sedia_cache.json')
	monkeypatch.setattr(calls, '_cache', None)


def test_get_topic_parses_a_real_shaped_metadata_record(monkeypatch, tmp_path):
	_reset_cache(monkeypatch, tmp_path)
	monkeypatch.setattr(
		calls.requests,
		'post',
		lambda *a, **k: _FakeResponse([{'metadata': REAL_SHAPED_METADATA}]),
	)

	topic = calls.get_topic('FAKE-CODE-2024')

	assert topic is not None
	assert topic['topic_code'] == 'FAKE-CODE-2024'
	assert topic['title'] == 'A fake but realistically-shaped topic'
	assert topic['call_identifier'] == 'H2020-FAKE-2023-2024'
	assert topic['call_title'] == 'FAKE CALL TITLE'
	assert topic['deadlines'] == ['2024-09-13T00:00:00.000+0000']
	assert topic['status'] == '31094503'
	assert topic['types_of_action'] == ['Research and Innovation action']
	assert topic['scope_text'] == 'Scope: do the work.'
	assert topic['framework_programme'] == '31045243'
	assert topic['programme_period'] == '2021 - 2027'
	assert topic['source_url'].endswith('FAKE-CODE-2024.json')
	assert 'T' in topic['fetched_at']  # ISO timestamp, not a guessed/hardcoded date

	cached = json.loads((tmp_path / 'sedia_cache.json').read_text())
	assert 'FAKE-CODE-2024' in cached


def test_get_topic_returns_none_without_an_identifier_match(monkeypatch, tmp_path):
	_reset_cache(monkeypatch, tmp_path)
	other = {**REAL_SHAPED_METADATA, 'identifier': ['SOME-OTHER-CODE']}
	monkeypatch.setattr(
		calls.requests, 'post', lambda *a, **k: _FakeResponse([{'metadata': other}])
	)

	assert calls.get_topic('FAKE-CODE-2024') is None


def test_topic_prefers_english_and_skips_award_records(monkeypatch, tmp_path):
	_reset_cache(monkeypatch, tmp_path)
	translation = {
		**REAL_SHAPED_METADATA,
		'type': ['1'],
		'language': ['nl'],
		'title': ['Translated title'],
	}
	award = {**REAL_SHAPED_METADATA, 'type': ['6'], 'language': ['en'], 'deadlineDate': []}
	english = {**REAL_SHAPED_METADATA, 'type': ['1'], 'language': ['en']}
	monkeypatch.setattr(
		calls.requests,
		'post',
		lambda *a, **k: _FakeResponse(
			[{'metadata': translation}, {'metadata': award}, {'metadata': english}]
		),
	)
	result = calls.get_topic('FAKE-CODE-2024')
	assert result['title'] == english['title'][0]
	assert result['deadlines'] == english['deadlineDate']


def test_a_live_capture_from_the_old_parser_is_refetched_with_its_paragraphs(monkeypatch, tmp_path):
	"""strip_html joined SEDIA's paragraphs and list items with nothing between
	them, so the call text read 'medicines.However' and 'templatesStandard'. A
	capture made that way is still fresh by date, and must be fetched again."""
	live = tmp_path / 'sedia_live.json'
	stale = {
		'topic_code': 'FAKE-CODE-2024',
		'source_record_type': '1',
		'display_document_version': 1,
		'scope_text': 'better medicines.However, despite',
		'fetched_at': datetime.now(UTC).isoformat(),
	}
	live.write_text(json.dumps({'FAKE-CODE-2024': stale}))
	monkeypatch.setattr(calls, '_LIVE_PATH', live)
	html = (
		'<p>better medicines.</p><p>However, despite</p>'
		'<ul><li>Application form templates</li><li>Standard application form</li></ul>'
	)
	record = {**REAL_SHAPED_METADATA, 'descriptionByte': [html]}
	monkeypatch.setattr(
		calls.requests, 'post', lambda *a, **k: _FakeResponse([{'metadata': record}])
	)

	topic = calls.get_current_topic('FAKE-CODE-2024')

	assert [line for line in topic['scope_text'].splitlines() if line] == [
		'better medicines.',
		'However, despite',
		'Application form templates',
		'Standard application form',
	]
