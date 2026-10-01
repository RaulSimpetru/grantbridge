"""User-added feed sources. The interesting parts are the validation, the
rollback when a feed does not parse, and that adding a source cannot widen what
this module is allowed to fetch."""

import json

import pytest
from fastapi.testclient import TestClient

from grant_recommender import api, catalog
from grant_recommender import german_funding as gf

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Open call for metabolic health</title>
    <link>https://stiftung.example/calls/metabolic</link>
    <pubDate>Tue, 22 Jan 2030 09:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Off-host entry that must be dropped</title>
    <link>https://elsewhere.example/calls/other</link>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom style call</title>
    <link href="https://stiftung.example/calls/atom"/>
    <updated>2030-01-22T09:00:00Z</updated>
  </entry>
</feed>"""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
	monkeypatch.setattr(gf, 'CUSTOM_SOURCES', tmp_path / 'custom_sources.json')


def test_a_feed_is_registered_only_after_it_parses(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)

	added = gf.add_source('Example Stiftung', 'https://stiftung.example/feed.rss')

	assert added['host'] == 'stiftung.example'
	assert [item['title'] for item in added['items']] == ['Open call for metabolic health']
	assert added['id'] in gf.custom_sources()
	assert added['id'] in gf.all_sources()


def test_entries_pointing_off_the_feed_host_are_dropped(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)

	added = gf.add_source('Example Stiftung', 'https://stiftung.example/feed.rss')

	# The second item is on elsewhere.example: adding a source must not become a
	# way to make this module fetch arbitrary hosts.
	assert all('stiftung.example' in item['url'] for item in added['items'])


def test_atom_feeds_are_read_too(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: ATOM)

	added = gf.add_source('Atom Source', 'https://stiftung.example/atom.xml')

	assert [item['title'] for item in added['items']] == ['Atom style call']


def test_a_page_that_is_neither_a_feed_nor_a_listing_is_rolled_back(monkeypatch):
	# Not valid XML, and with no links worth following either, so both paths
	# come up empty and nothing should be left registered.
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: '<html><p>oops')

	with pytest.raises(ValueError, match='looked like a funding call'):
		gf.add_source('Broken', 'https://stiftung.example/broken')

	# A source that polls forever and returns nothing is worse than no source.
	assert gf.custom_sources() == {}


def test_a_feed_with_no_usable_entries_is_rejected(monkeypatch):
	empty = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: empty)

	# An empty feed falls through to the link reader, which also finds nothing.
	with pytest.raises(ValueError, match='looked like a funding call'):
		gf.add_source('Empty', 'https://stiftung.example/empty.rss')
	assert gf.custom_sources() == {}


@pytest.mark.parametrize(
	'url',
	[
		'http://stiftung.example/feed.rss',  # not https
		'https://user:pw@stiftung.example/feed.rss',  # credentials
		'https://stiftung.example:8443/feed.rss',  # odd port
		'not a url',
	],
)
def test_unsafe_urls_are_refused(url, monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda u, p: FEED)
	with pytest.raises(ValueError):
		gf.add_source('Bad', url)


def test_a_source_needs_a_name(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	with pytest.raises(ValueError, match='name'):
		gf.add_source('  ', 'https://stiftung.example/feed.rss')


def test_the_same_feed_cannot_be_added_twice(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	gf.add_source('Once', 'https://stiftung.example/feed.rss')

	with pytest.raises(ValueError, match='already'):
		gf.add_source('Twice', 'https://stiftung.example/feed.rss')


def test_removing_a_source_takes_it_out_of_the_union(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	added = gf.add_source('Example', 'https://stiftung.example/feed.rss')

	gf.remove_source(added['id'])

	assert gf.custom_sources() == {}
	assert added['id'] not in gf.all_sources()


def test_a_custom_host_is_authorised_only_for_its_own_provider(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	added = gf.add_source('Example', 'https://stiftung.example/feed.rss')

	assert gf.allowed_url('https://stiftung.example/x', added['id']) is True
	# Not for the built-ins, and not for a provider that does not exist.
	assert gf.allowed_url('https://stiftung.example/x', 'dfg') is False
	assert gf.allowed_url('https://stiftung.example/x', 'custom-unknown') is False


def test_the_watch_loop_polls_added_sources(monkeypatch):
	from grant_recommender import watch

	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	added = gf.add_source('Example', 'https://stiftung.example/feed.rss')

	assert added['id'] in watch.providers()


NOTICE = (
	'<main><h1>Open call for metabolic health</h1><p>'
	+ 'Projects on implantable and wearable glucose sensors are eligible. ' * 4
	+ '</p></main>'
)


def _import_from_added_feed(monkeypatch) -> dict:
	"""What the watch loop does with an added feed: refresh it, import a notice."""
	pages = {
		'https://stiftung.example/feed.rss': FEED,
		'https://stiftung.example/calls/metabolic': NOTICE,
	}
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: pages[url])
	added = gf.add_source('Example Stiftung', 'https://stiftung.example/feed.rss')
	item = gf.refresh(added['id'])['items'][0]
	return gf.import_notice(added['id'], item['id'])


def test_a_notice_from_an_added_feed_is_imported_under_its_name(monkeypatch):
	# This raised KeyError: the name was looked up among the built-ins only, so
	# the watch loop polled an added feed every tick and never imported a call.
	call = _import_from_added_feed(monkeypatch)

	assert call['call_identifier'] == 'Example Stiftung'
	assert gf.get_call(call['topic_code']) == call


def test_an_imported_custom_notice_resolves_from_the_watch_list(monkeypatch):
	call = _import_from_added_feed(monkeypatch)
	# A CUSTOM- code means nothing to the EU portal; asking it was a 404.
	monkeypatch.setattr(catalog, 'get_eu_topic', lambda code: pytest.fail('asked the EU portal'))

	assert catalog.get_topic(call['topic_code']) == call


def test_persisted_sources_survive_a_reload(monkeypatch, tmp_path):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	added = gf.add_source('Example', 'https://stiftung.example/feed.rss')

	on_disk = json.loads((tmp_path / 'custom_sources.json').read_text())

	assert on_disk[added['id']]['url'] == 'https://stiftung.example/feed.rss'


PAGE = """<html><body>
  <a href="/impressum">Impressum und Datenschutz</a>
  <a href="/calls/metabolic-health">Ausschreibung: Stoffwechsel und Ernaehrung</a>
  <a href="/calls/open-science">Förderprogramm Open Science 2026</a>
  <a href="https://elsewhere.example/off">A link on another host entirely</a>
  <a href="/x">short</a>
</body></html>"""


def test_link_candidates_keep_only_on_host_links_with_real_labels(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: PAGE)
	gf._write(
		gf.CUSTOM_SOURCES,
		{
			'custom-x': {
				'name': 'S',
				'url': 'https://stiftung.example/f',
				'host': 'stiftung.example',
			}
		},
	)

	found = gf.link_candidates(PAGE, 'https://stiftung.example/list', 'custom-x')
	urls = [c['url'] for c in found]

	assert 'https://stiftung.example/calls/metabolic-health' in urls
	# Off-host and too-short labels are not candidates at all.
	assert not any('elsewhere.example' in u for u in urls)
	assert not any(u.endswith('/x') for u in urls)


def test_link_candidates_skip_the_site_menu_when_the_page_marks_it(monkeypatch):
	"""A funder's mega-menu comes first in document order. On the DFG news page
	its 619 links used up the whole budget before the 15 announcements."""
	gf._write(
		gf.CUSTOM_SOURCES,
		{
			'custom-x': {
				'name': 'S',
				'url': 'https://stiftung.example/f',
				'host': 'stiftung.example',
			}
		},
	)
	menu = ''.join(
		f'<a href="/menu/{i}">Menu entry number {i}</a>' for i in range(gf.LINK_CANDIDATES)
	)
	page = (
		f'<header><nav>{menu}</nav></header>'
		'<main><a href="/calls/metabolic-health">Ausschreibung: Stoffwechselforschung</a></main>'
		'<footer><a href="/impressum">Impressum und Datenschutz</a></footer>'
	)

	found = gf.link_candidates(page, 'https://stiftung.example/list', 'custom-x')

	assert [c['url'] for c in found] == ['https://stiftung.example/calls/metabolic-health']


def test_the_model_can_only_choose_from_links_that_exist(monkeypatch):
	candidates = [
		{'url': 'https://stiftung.example/a', 'title': 'Ausschreibung A'},
		{'url': 'https://stiftung.example/b', 'title': 'Impressum'},
	]
	# Positions out of range are the shape an invented link would take.
	monkeypatch.setattr(
		gf, 'notice', lambda provider, url, title: {'id': title, 'url': url, 'title': title}
	)
	from grant_recommender import concepts

	monkeypatch.setattr(
		concepts, '_ollama_chat', lambda *a, **k: ({'calls': [0, 99, -1]}, {'request': {}})
	)
	monkeypatch.setattr(
		concepts, 'connection_settings', lambda: {'base_url': 'http://x', 'model': 'm'}
	)

	picked = gf.select_call_links(candidates, 'custom-x')

	assert [p['url'] for p in picked] == ['https://stiftung.example/a']


def test_a_page_that_is_not_a_feed_is_added_by_reading_its_links(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: PAGE)
	monkeypatch.setattr(
		gf,
		'select_call_links',
		lambda candidates, provider: [
			gf.notice(provider, c['url'], c['title']) for c in candidates[:2]
		],
	)

	added = gf.add_source('Stiftung', 'https://stiftung.example/foerderung')

	# Recorded as an html source so refresh reads it the same way next time.
	assert added['kind'] == 'html'
	assert gf.custom_sources()[added['id']]['kind'] == 'html'
	assert len(added['items']) == 2


def test_a_real_feed_still_takes_the_deterministic_path(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: FEED)
	called = []
	monkeypatch.setattr(gf, 'select_call_links', lambda *a, **k: called.append(1) or [])

	added = gf.add_source('Feed', 'https://stiftung.example/feed.rss')

	# Parsing a feed exactly costs no model time, so the model is never asked.
	assert added['kind'] == 'rss'
	assert called == []


def test_a_page_with_nothing_fundable_is_refused(monkeypatch):
	monkeypatch.setattr(gf, 'fetch', lambda url, provider: PAGE)
	monkeypatch.setattr(gf, 'select_call_links', lambda candidates, provider: [])

	with pytest.raises(ValueError, match='looked like a funding call'):
		gf.add_source('Empty', 'https://stiftung.example/news')
	assert gf.custom_sources() == {}


# The watch list these sources feed: a clone may have no curated EU list at all.


@pytest.mark.parametrize('content', [None, '', '\n', '{}'])
def test_a_missing_or_blank_curated_list_watches_no_eu_topics(content, monkeypatch, tmp_path):
	path = tmp_path / 'watched_topics.json'
	if content is not None:
		path.write_text(content)
	monkeypatch.setattr(catalog, 'WATCHED_TOPICS_PATH', path)

	assert catalog.watched_topic_codes() == []


def test_with_nothing_curated_or_imported_the_watch_list_is_empty_not_down(monkeypatch, tmp_path):
	monkeypatch.setattr(catalog, 'WATCHED_TOPICS_PATH', tmp_path / 'watched_topics.json')

	# GET /calls was a 500 here. Reporting the sources as down instead would be
	# just as wrong, and would stop the watch loop before the discover() that
	# fills the list.
	assert catalog.watched_calls() == []
	response = TestClient(api.app).get('/calls')
	assert (response.status_code, response.json()) == (200, [])


def test_an_article_header_inside_main_is_content_not_site_chrome():
	"""WordPress and Drupal teasers put each announcement's link in the
	article's own <header>; treating that as the site menu hid every call."""
	page = gf.Page(
		'<header><nav><a href="/menu">Site menu</a></nav></header>'
		'<main><article><header><h2><a href="/calls/one">Call one</a></h2></header>'
		'<a href="/calls/one">Weiterlesen</a></article></main>'
	)

	assert ('/calls/one', 'Call one') in page.body_links
	assert ('/menu', 'Site menu') not in page.body_links
