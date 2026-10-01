import json
from datetime import UTC, datetime

from grant_recommender import calls
from grant_recommender.source_document import source_document


def walk(nodes):
	for node in nodes:
		yield node
		yield from walk(node.get('children', []))


def text(nodes):
	return ''.join(n.get('text', '') for n in walk(nodes))


def test_official_conditions_keep_headings_paragraphs_and_annex_links():
	html = '<div><h4>General conditions</h4><div><h4>1. Admissibility Conditions: Proposal page limit and layout</h4><p><strong><em>\ufeff</em></strong>described in <a href="https://example.org/annex.pdf">Annex A</a>.</p><p>Proposal page limits and layout: described in Part B.</p></div><div><h4>2. Eligible Countries</h4><p>described in Annex B.</p></div></div>'
	nodes = list(walk(source_document(html)))
	assert [text(n['children']) for n in nodes if n['type'] == 'h4'] == [
		'General conditions',
		'1. Admissibility Conditions: Proposal page limit and layout',
		'2. Eligible Countries',
	]
	assert len([n for n in nodes if n['type'] == 'p']) == 3
	assert next(n for n in nodes if n['type'] == 'a')['href'] == 'https://example.org/annex.pdf'
	assert '\ufeff' not in text(nodes)


def test_no_active_html_styles_or_unsafe_urls_survive():
	doc = source_document(
		'<script>evil()</script><style>body{display:none}</style><iframe>iframe</iframe><p onclick="evil()" style="display:none">Safe <a href="javascript:evil()">label</a><img src="x" onerror="evil()"><a href="https://user:secret@example.org">private URL</a></p>'
	)
	serialized = json.dumps(doc)
	assert text(doc) == 'Safe labelprivate URL'
	assert all(
		word not in serialized
		for word in ['onclick', 'onerror', 'javascript', 'evil()', 'secret', 'iframe', 'style']
	)
	assert all('href' not in node for node in walk(doc) if node['type'] == 'a')


def test_lists_numbers_tables_inline_emphasis_and_entities_are_preserved():
	doc = source_document(
		'<ol start="3"><li>First <b>item</b></li><li value="5">Next &amp; final</li></ol><table><tr><td colspan="2">Cell<br>Second line</td></tr></table>'
	)
	nodes = list(walk(doc))
	assert doc[0]['start'] == 3
	assert doc[0]['children'][1]['value'] == 5
	assert next(n for n in nodes if n['type'] == 'td')['colspan'] == 2
	assert any(n['type'] == 'strong' for n in nodes)
	assert any(n['type'] == 'br' for n in nodes)
	assert 'Next & final' in text(doc)


def test_display_migration_refetches_a_stale_capture_once(monkeypatch, tmp_path):
	path = tmp_path / 'live.json'
	previous = {
		'topic_code': 'TEST',
		'source_record_type': '1',
		'fetched_at': datetime.now(UTC).isoformat(),
	}
	path.write_text(json.dumps({'TEST': previous}))
	monkeypatch.setattr(calls, '_LIVE_PATH', path)
	counter = []

	def fetch(code):
		counter.append(code)
		return {
			**previous,
			'display_document_version': 1,
			'topic_conditions_document': source_document('<h4>General conditions</h4><p>Text.</p>'),
		}

	monkeypatch.setattr(calls, '_fetch_topic', fetch)
	assert calls.get_current_topic('TEST')['topic_conditions_document']
	assert calls.get_current_topic('TEST')['topic_conditions_document']
	assert counter == ['TEST']


def test_missing_and_malformed_attributes_do_not_break_source_rendering():
	doc = source_document(
		'<a href>Label</a><ol start><li value="²">Item</li></ol><table><td colspan="99999999999999999999">Cell</td></table>'
	)
	assert text(doc) == 'LabelItemCell'
	assert all(
		not any(key in node for key in ('href', 'start', 'value', 'colspan')) for node in walk(doc)
	)
