import requests
from fastapi.testclient import TestClient

from grant_recommender import api, eligibility
from grant_recommender import german_funding as funding


def test_dfg_feed_filters_offsite_links_and_keeps_publication_not_deadline():
	rows = funding.parse_dfg("""<rss><channel>
	<item><title>Research opportunity</title><link>https://www.dfg.de/de/info-wissenschaft/2026/example</link><pubDate>Fri, 4 Jan 2030</pubDate></item>
	<item><title>Other</title><link>https://attacker.example/</link></item>
	</channel></rss>""")
	assert len(rows) == 2
	assert rows[1]['published'] == 'Fri, 4 Jan 2030'
	assert 'deadlines' not in rows[1]
	assert rows[0]['url'] == funding.DFG_PROGRAMME


def test_bmftr_links_are_deduplicated_and_offsite_notices_excluded():
	rows = funding.parse_bmftr(
		"""<a href="/de/richtlinie-test.php">Richtlinie zur Förderung von Forschungsprojekten</a>
	<a href="/de/richtlinie-test.php">Richtlinie zur Förderung von Forschungsprojekten</a>
	<a href="https://attacker.example/de/richtlinie-test.php">Richtlinie zur Förderung von Forschungsprojekten</a>""",
		funding.SOURCES['bmftr']['url'],
	)
	assert len(rows) == 1
	assert rows[0]['provider'] == 'bmftr'


def test_page_extracts_main_body_not_navigation_or_scripts():
	page = funding.Page(
		'<nav>Secret menu</nav><main><h1>Funding</h1><p>Source text</p><script>bad()</script><aside>Side menu</aside></main><footer>Footer</footer>'
	)
	assert page.text() == 'Funding\nSource text'


def test_bmftr_numeric_homepage_notice_alias_is_discovered():
	rows = funding.parse_bmftr(
		'<a href="/de/19026.php">11.08.2026 Richtlinie zur Förderung von Pilotprojekten</a>',
		funding.SOURCES['bmftr']['url'],
	)
	assert len(rows) == 1
	assert rows[0]['url'].endswith('/de/19026.php')


def test_failed_refresh_retains_previous_notices_with_warning(monkeypatch):
	funding._write(funding.CACHE / 'dfg.json', {'items': [{'id': 'kept'}], 'fetched_at': 'old'})
	monkeypatch.setattr(
		funding, 'fetch', lambda *a: (_ for _ in ()).throw(requests.ConnectionError('offline'))
	)
	result = funding.refresh('dfg')
	assert result['items'] == [{'id': 'kept'}]
	assert result['fetched_at'] == 'old'
	assert 'offline' in result['error']


def test_import_is_official_snapshot_without_invented_eligibility_or_deadline(
	monkeypatch, tmp_path
):
	item = funding.notice('dfg', funding.DFG_PROGRAMME, 'Sachbeihilfe')
	funding._write(funding.CACHE / 'dfg.json', {'items': [item]})
	monkeypatch.setattr(
		funding,
		'fetch',
		lambda *a: (
			'<main><h1>Research Grants</h1><p>'
			+ 'Original research with scientific quality. ' * 8
			+ '</p></main>'
		),
	)
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews.json')
	call = funding.import_notice('dfg', item['id'])
	assert call['country'] == 'DE'
	assert call['deadlines'] == []
	assert eligibility.assess(call, [])['state'] == 'uncertain'
	assert funding.get_call(item['id']) == call
	assert api.get_topic(item['id']) == call


def test_import_rejects_unknown_source_and_unlisted_notice():
	client = TestClient(api.app)
	assert client.post('/funding-sources/other/refresh').status_code == 422
	assert client.post('/funding-sources/dfg/notices/not-listed/import').status_code == 404


def test_fetch_rejects_nonofficial_target_before_network(monkeypatch):
	def unexpected(*args, **kwargs):
		raise AssertionError('Network must not be called')

	monkeypatch.setattr(requests, 'get', unexpected)
	try:
		funding.fetch('https://attacker.example/', 'dfg')
	except ValueError:
		pass
	else:
		raise AssertionError('Expected URL rejection')
