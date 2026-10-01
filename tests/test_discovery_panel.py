"""Discovery screen: what it loads, how it ranks, and the thread boundary
around the scrape. Rendered through an offscreen terminal so these exercise the
real grid."""

import os
import time
from datetime import datetime
from unittest import mock

import pytest
from xnano import Terminal

from grant_recommender import catalog, discovery_panel

OPEN_CALL = {
	'topic_code': 'OPEN-1',
	'title': 'Long-term CGM for type 2 diabetes',
	'scope_text': 'long-term CGM glucose forecasting for type 2 diabetes',
	'deadlines': ['2099-01-01T17:00:00'],
	'source_url': 'https://example.org/open-1',
}
CLOSED_CALL = {
	'topic_code': 'CLOSED-1',
	'title': 'A call whose deadline has passed',
	'scope_text': 'long-term CGM glucose forecasting',
	'deadlines': ['2019-01-01T17:00:00'],
}
PORTFOLIO = [
	{
		'id': 'a1',
		'component': 'methods',
		'claim': 'long-term CGM glucose forecasting',
		'confidence': 'confirmed',
		'included': True,
	}
]


BLANK = {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()}


def a_concept(job_id, concept_id, title):
	return {
		'job_id': job_id,
		'created_at': f'2030-01-2{concept_id}',
		'trial': True,
		'model': 'test-model',
		'concept': {
			'concept_id': concept_id,
			'title': title,
			'problem': 'Glucose excursion patterns are hard to predict.',
			'objectives': ['Model the patterns', 'Validate on held-out subjects'],
			'call_evidence': [{'source_id': 'CALL_TEXT'}],
			'researcher_evidence': [],
		},
	}


def stage(grid, monkeypatch, **snapshot):
	"""Hand the panel a lifecycle snapshot instead of the real stores."""
	snap = {**BLANK, **snapshot}
	monkeypatch.setattr(discovery_panel.lifecycle, 'snapshot', lambda calls: snap)
	grid._refresh_stages()
	# The live panel repaints on its tick; a test has to ask.
	grid.repaint()
	return snap


@pytest.fixture
def panel(monkeypatch):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	# Real runs, rounds and decisions are on disk; the panel must not read them
	# in a test, and a test that wants them says so with stage().
	monkeypatch.setattr(discovery_panel.lifecycle, 'snapshot', lambda calls: dict(BLANK))
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [CLOSED_CALL, OPEN_CALL])
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda provider: {
			'name': provider.upper(),
			'items': [{'id': f'{provider}-1'}],
			'fetched_at': '2030-01-22T05:00:00+00:00',
			'error': None,
		},
	)
	grid = discovery_panel.Discovery()
	terminal = Terminal.offscreen(cols=180, rows=46)
	terminal.attach_grid(grid)
	return grid, terminal


def test_opportunities_are_ranked_by_fit(panel):
	grid, _ = panel
	# Both calls mention CGM, but the open one matches the portfolio more
	# closely, so it must not be buried under the expired one.
	assert grid.opportunities[0]['fit']['rank'] == 1


def test_eligibility_is_reported_separately_from_fit(panel):
	grid, _ = panel
	states = {row['call']['topic_code']: row['eligibility']['state'] for row in grid.opportunities}

	# A passed deadline is ineligible however well it scores: the gate is
	# deterministic and must not be softened by similarity.
	assert states['CLOSED-1'] == 'ineligible'
	assert states['OPEN-1'] in ('eligible', 'uncertain')


def test_a_dead_network_gives_an_empty_screen_not_a_traceback(monkeypatch):
	def boom(*args, **kwargs):
		raise catalog.SourcesUnavailable('sources down')

	monkeypatch.setattr(catalog, 'watched_calls', boom)
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)

	assert discovery_panel.load_opportunities() == []


def test_panel_paints_sources_the_watch_list_and_a_dossier(panel):
	grid, terminal = panel
	terminal.render(grid)
	out = terminal.get_output()

	assert 'sources' in out
	assert 'Long-term CGM' in out
	assert 'ELIGIBILITY' in out
	assert 'SOURCE' in out


def test_selection_moves_and_redraws_the_dossier(panel):
	grid, terminal = panel
	first = grid.opportunities[0]['call']['topic_code']

	grid.select_next()
	assert grid.selected == 1
	grid.select_next()  # clamped, never off the end
	assert grid.selected == 1

	terminal.render(grid)
	second = grid.opportunities[1]['call']['topic_code']
	assert first != second
	assert grid.opportunities[grid.selected]['call']['topic_code'] == second


def test_scrape_runs_off_thread_and_logs_each_step(panel, monkeypatch):
	grid, _ = panel
	seen = {}

	def fake_discover(max_imports, on_event=None):
		seen['max_imports'] = max_imports
		on_event('fetching dfg…')
		on_event('imported DFG-1')
		return {}, []

	monkeypatch.setattr(discovery_panel.watch, 'discover', fake_discover)
	# Run the worker inline so the test does not race a thread.
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, daemon=None: type('T', (), {'start': lambda s: target()})(),
	)

	grid.scrape()

	assert seen['max_imports'] == discovery_panel.MAX_IMPORTS
	assert any('fetching dfg' in line for line in grid._log)
	assert any('imported DFG-1' in line for line in grid._log)
	assert grid.scraping is False


def test_a_failing_scrape_is_logged_not_raised(panel, monkeypatch):
	grid, _ = panel

	def boom(max_imports, on_event=None):
		raise RuntimeError('source down')

	monkeypatch.setattr(discovery_panel.watch, 'discover', boom)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, daemon=None: type('T', (), {'start': lambda s: target()})(),
	)

	grid.scrape()

	assert any('scrape failed' in line for line in grid._log)
	assert grid.scraping is False


def test_a_second_scrape_cannot_start_while_one_runs(panel, monkeypatch):
	grid, _ = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, daemon=None: type('T', (), {'start': lambda s: started.append(1)})(),
	)
	grid.scraping = True

	grid.scrape()

	assert started == []


def test_times_on_screen_are_the_researchers_clock_not_utc(panel):
	"""Every stamp was UTC with no label, hours off the local wall clock. Tokyo
	is never on UTC, so a UTC stamp cannot pass by luck."""
	if not hasattr(time, 'tzset'):
		pytest.skip('time.tzset is Unix-only')
	grid, _ = panel
	try:
		# A POSIX rule, not 'Asia/Tokyo': slim Linux images have no zoneinfo files,
		# and glibc then falls back to UTC.
		with mock.patch.dict(os.environ, TZ='JST-9'):
			time.tzset()
			before = datetime.now().astimezone()
			grid._log_event('scrape finished')
			grid._paint_sources()
			now = datetime.now().astimezone()
	finally:
		time.tzset()

	assert grid._log[-1][:5] in {f'{before:%H:%M}', f'{now:%H:%M}'}
	# The fixture's sources were fetched at 05:00 UTC, which is 14:00 in Tokyo.
	assert grid.left.sources.rows[0][1].text.endswith('14:00')


def test_added_sources_appear_in_the_sources_pane(panel, monkeypatch):
	"""A source the researcher added has to show up beside the built-ins, or
	there is no way to tell it took."""
	names = {'dfg': 'DFG', 'custom-1': 'Example Stiftung'}
	monkeypatch.setattr(discovery_panel.german_funding, 'all_sources', lambda: names)
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda provider: {
			'name': names[provider],
			'items': [],
			'fetched_at': '2030-01-22T05:00:00+00:00',
			'error': None,
		},
	)
	grid, terminal = panel

	grid._paint_sources()
	terminal.render(grid)

	assert 'Example Stiftung' in terminal.get_output()


def test_pressing_a_opens_a_focused_input_and_escape_closes_it(panel):
	grid, terminal = panel

	terminal.actions.keyboard('a')
	assert grid.adding is True
	# input=True is what makes the field capture typing; without it the field
	# is inert and the URL goes nowhere.
	assert grid.left.add_source.input is True

	terminal.actions.keyboard('escape')
	assert grid.adding is False
	# Back to inert, or it keeps eating every keystroke in the panel.
	assert grid.left.add_source.input is False


def test_typing_a_url_and_pressing_enter_adds_the_source(panel, monkeypatch):
	grid, terminal = panel
	added = {}

	monkeypatch.setattr(
		discovery_panel.german_funding,
		'add_source',
		lambda name, url: (
			added.update(name=name, url=url) or {'name': name, 'items': [{'id': 'x'}]}
		),
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('a')
	for char in 'https://stiftung.example/feed.rss':
		terminal.actions.keyboard(char)
	terminal.actions.keyboard('enter')

	assert added['url'] == 'https://stiftung.example/feed.rss'
	# The name defaults to the feed's host rather than asking for a second field.
	assert added['name'] == 'stiftung.example'
	assert any('added source' in line for line in grid._log)


def test_a_feed_that_will_not_parse_is_reported_not_raised(panel, monkeypatch):
	grid, terminal = panel

	def boom(name, url):
		raise ValueError('no entries')

	monkeypatch.setattr(discovery_panel.german_funding, 'add_source', boom)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('a')
	for char in 'https://bad.example/x':
		terminal.actions.keyboard(char)
	terminal.actions.keyboard('enter')

	assert any('could not add' in line for line in grid._log)


def test_an_empty_url_just_closes_the_input(panel, monkeypatch):
	grid, terminal = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(1)}
		)(),
	)

	terminal.actions.keyboard('a')
	terminal.actions.keyboard('enter')

	assert grid.adding is False
	assert started == []


EU_CALL = {
	'topic_code': 'HORIZON-1',
	'title': 'EU call about CGM',
	'scope_text': 'long-term CGM glucose forecasting',
	'deadlines': ['2099-01-01T17:00:00'],
}
DFG_CALL = {
	'topic_code': 'DFG-1',
	'title': 'DFG notice about CGM',
	'provider': 'dfg',
	'scope_text': 'long-term CGM glucose forecasting for type 2 diabetes',
	'deadlines': ['2099-01-01T17:00:00'],
}
DFG_CALL_2 = {
	'topic_code': 'DFG-2',
	'title': 'DFG notice about something else',
	'provider': 'dfg',
	'scope_text': 'viticulture and cellar management',
	'deadlines': ['2099-01-01T17:00:00'],
}


def test_source_key_treats_eu_calls_as_one_connector():
	assert discovery_panel.source_key(EU_CALL) == 'eu'
	assert discovery_panel.source_key(DFG_CALL) == 'dfg'


def test_watch_list_is_grouped_by_source_then_ranked_within_the_group(monkeypatch):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [DFG_CALL_2, EU_CALL, DFG_CALL])

	rows = discovery_panel.load_opportunities()
	sources = [discovery_panel.source_key(r['call']) for r in rows]

	# EU first, then the German sources in registration order.
	assert sources == ['eu', 'dfg', 'dfg']
	# Inside the DFG group the better-matching notice comes first, even though
	# the input order had it last.
	dfg = [r for r in rows if discovery_panel.source_key(r['call']) == 'dfg']
	assert dfg[0]['call']['topic_code'] == 'DFG-1'
	assert dfg[0]['fit']['rank'] < dfg[1]['fit']['rank']


def test_a_source_no_longer_registered_sorts_last_rather_than_raising(monkeypatch):
	orphan = {**DFG_CALL, 'topic_code': 'GONE-1', 'provider': 'custom-removed'}
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [orphan, EU_CALL])

	rows = discovery_panel.load_opportunities()

	assert discovery_panel.source_key(rows[-1]['call']) == 'custom-removed'


def test_each_group_gets_a_header_with_its_count(monkeypatch):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [EU_CALL, DFG_CALL, DFG_CALL_2])
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda provider: {'name': provider, 'items': [], 'fetched_at': None, 'error': None},
	)
	grid = discovery_panel.Discovery()
	terminal = Terminal.offscreen(cols=180, rows=46)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	assert 'EU Funding Portal (1)' in out
	assert 'DFG (2)' in out


def test_selection_still_indexes_calls_not_painted_header_rows(monkeypatch):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [EU_CALL, DFG_CALL])
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda provider: {'name': provider, 'items': [], 'fetched_at': None, 'error': None},
	)
	grid = discovery_panel.Discovery()

	grid.select_next()

	# Two calls, two groups, so two headers are painted. Moving down once must
	# land on the second call, not on a header. Asserted through the tree, which
	# is what the selection actually indexes: reading it out of `opportunities`
	# passed whether or not headers were counted.
	assert grid._selected_call()['topic_code'] == 'DFG-1'
	assert grid._entries[grid.selected]['kind'] == 'call'


def test_match_strength_reads_as_words_not_a_bare_number():
	assert discovery_panel.match_strength(0.0)[0] == 'no overlap'
	assert discovery_panel.match_strength(0.05)[0] == 'weak'
	assert discovery_panel.match_strength(0.2)[0] == 'partial'
	assert discovery_panel.match_strength(0.5)[0] == 'strong'


def test_displayed_terms_skip_words_that_match_everything():
	call = {'scope_text': 'studies and spectroscopy and tissue oxygenation'}
	portfolio = [
		{
			'claim': 'tissue oxygenation and spectroscopy studies',
			'confidence': 'confirmed',
			'included': True,
		}
	]

	terms = discovery_panel.display_terms(call, portfolio)

	# 'and' and 'studies' match almost any call, so listing them as evidence of
	# a match is noise.
	assert 'and' not in terms
	assert 'studies' not in terms
	assert 'spectroscopy' in terms


def test_excerpt_drops_the_breadcrumb_trail_and_repeats():
	real = 'A real paragraph of announcement text that is long enough to be content.'
	raw = (
		'Bekanntmachungen 2026\nForschung fördern\nBekanntmachungen\n'
		f'Bekanntmachungen 2026\n{real}\n{real}\n'
		'More detail follows here in the body of the call.'
	)

	out = discovery_panel.excerpt(raw)

	assert out.startswith('A real paragraph')
	# Repeats collapse, and the crumbs above the first real line are gone.
	assert out.count('A real paragraph') == 1
	assert 'Forschung fördern' not in out


def test_excerpt_keeps_short_lines_once_real_content_started():
	raw = 'A long opening paragraph that clearly counts as real content here.\n20.05.2026\nnext'
	out = discovery_panel.excerpt(raw)
	assert '20.05.2026' in out


def test_a_deadline_buried_in_the_text_is_surfaced_as_unverified():
	call = {'scope_text': 'vom 20.05.2026 - Abgabetermin: 30.06.2026 blah'}
	assert discovery_panel.deadline_in_text(call) == '30.06.2026'


def test_no_deadline_hint_when_the_text_has_none():
	assert discovery_panel.deadline_in_text({'scope_text': 'no dates here at all'}) is None


def test_dossier_leads_with_the_match_and_never_shows_bm25(panel):
	grid, terminal = panel
	grid._width = 170
	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'MATCH' in out
	assert 'ELIGIBILITY' in out
	# bm25 is a retrieval score with no meaning to a reader; the rank carries
	# the same ordering information.
	assert 'bm25' not in out.lower()
	# Fit is the question being asked, so it comes before eligibility.
	assert out.index('MATCH') < out.index('ELIGIBILITY')


def test_enter_opens_the_selected_calls_page(panel, monkeypatch):
	grid, terminal = panel
	opened = []
	monkeypatch.setattr(discovery_panel.webbrowser, 'open', lambda url: opened.append(url))
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	# Select by topic code: the expired call can outrank the open one, so the
	# first row is not reliably the one with a URL.
	grid.selected = next(
		i for i, r in enumerate(grid.opportunities) if r['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('enter')

	assert opened == [OPEN_CALL['source_url']]
	assert any('opening' in line for line in grid._log)


def test_enter_submits_the_feed_instead_while_the_add_form_is_open(panel, monkeypatch):
	grid, terminal = panel
	opened = []
	monkeypatch.setattr(discovery_panel.webbrowser, 'open', lambda url: opened.append(url))
	monkeypatch.setattr(
		discovery_panel.german_funding, 'add_source', lambda name, url: {'name': name, 'items': []}
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('a')
	for char in 'https://stiftung.example/feed.rss':
		terminal.actions.keyboard(char)
	terminal.actions.keyboard('enter')

	# Enter belongs to the form while it is open; it must not also fire a browser.
	assert opened == []
	assert grid.adding is False


def test_a_call_without_a_source_url_says_so_rather_than_opening_nothing(panel, monkeypatch):
	grid, _ = panel
	opened = []
	monkeypatch.setattr(discovery_panel.webbrowser, 'open', lambda url: opened.append(url))
	grid.selected = 0
	grid.opportunities[0]['call'] = {**OPEN_CALL, 'source_url': None}
	grid.open_selected()

	assert opened == []
	assert any('no source URL' in line for line in grid._log)


def test_a_non_web_link_is_refused(panel, monkeypatch):
	"""source_url comes from a scraped page, so a file: or javascript: link
	must never be handed to the browser."""
	grid, _ = panel
	opened = []
	monkeypatch.setattr(discovery_panel.webbrowser, 'open', lambda url: opened.append(url))
	grid.selected = 0
	grid.opportunities[0]['call'] = {**OPEN_CALL, 'source_url': 'file:///etc/passwd'}
	grid.open_selected()

	assert opened == []
	assert any('refused' in line for line in grid._log)


def test_enter_does_nothing_when_the_screen_is_not_the_one_on_show(panel, monkeypatch):
	grid, terminal = panel
	opened = []
	monkeypatch.setattr(discovery_panel.webbrowser, 'open', lambda url: opened.append(url))
	grid.active = False

	terminal.actions.keyboard('enter')

	assert opened == []


REFINED = {
	'topic_code': 'OPEN-1',
	'source_hash': 'abc',
	'model': 'm',
	'fields': {
		'deadline': {'value': '02.09.2026', 'quote': 'Abgabetermin: 02.09.2026 fuer alle'},
	},
	'dropped': ['budget'],
}


def test_e_reads_the_selected_call_off_thread(panel, monkeypatch):
	grid, terminal = panel
	asked = []
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: None)
	monkeypatch.setattr(
		discovery_panel.refinement,
		'refine',
		lambda call, on_text=None, use_cache=True: asked.append(call) or REFINED,
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('e')

	assert len(asked) == 1
	assert grid.refining is False
	assert any('deadline' in line for line in grid._log)


def test_pressing_the_key_on_an_already_read_call_reads_it_again(panel, monkeypatch):
	"""A cache hit that silently does nothing reads as a broken key. An explicit
	press is a request, so it re-reads and says so."""
	grid, terminal = panel
	asked = []
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: REFINED)
	monkeypatch.setattr(
		discovery_panel.refinement,
		'refine',
		lambda call, on_text=None, use_cache=True: asked.append(use_cache) or REFINED,
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('e')

	assert asked == [False]
	assert any('re-reading' in line for line in grid._log)


def test_refined_fields_are_shown_with_the_quote_they_came_from(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: REFINED)
	grid._width = 170

	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'DEADLINE' in out
	assert '02.09.2026' in out
	# The evidence is shown, not just the verdict.
	assert 'Abgabetermin' in out
	# And the reader is told something was discarded.
	assert 'dropped' in out


def test_a_read_that_kept_nothing_does_not_claim_quotes(panel, monkeypatch):
	"""The header said every value below was quoted from the call when no value
	was below it."""
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.refinement, 'cached', lambda call: {**REFINED, 'fields': {}, 'dropped': []}
	)
	grid._width = 170

	grid._paint_dossier()
	terminal.render(grid)

	assert 'quoted from the call' not in terminal.get_output()


def test_a_failing_refinement_is_logged_and_clears_the_flag(panel, monkeypatch):
	grid, terminal = panel

	def boom(call, on_text=None, use_cache=True):
		raise RuntimeError('model down')

	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: None)
	monkeypatch.setattr(discovery_panel.refinement, 'refine', boom)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)

	terminal.actions.keyboard('e')

	assert any('could not read' in line for line in grid._log)
	assert grid.refining is False


class _Ctx:
	def __init__(self, terminal=None, render_size=None):
		self.terminal = terminal
		self.render_size = render_size


def test_resize_reads_the_viewport_width_not_the_breakpoint_name(panel):
	"""Regression: ctx.render_size is a tier name like 'medium'. Indexing it
	gave 'm' and int('m') crashed the app on the first resize."""
	grid, terminal = panel

	grid.track_width(_Ctx(terminal=terminal, render_size='medium'))

	assert grid._width == terminal.size[0]


def test_a_resize_that_reports_nothing_usable_is_ignored(panel):
	grid, _ = panel
	before = grid._width

	grid.track_width(_Ctx(terminal=None, render_size='medium'))
	grid.track_width(_Ctx(terminal=object(), render_size=None))

	# A resize must never be able to take the panel down.
	assert grid._width == before


def test_width_change_rewraps_the_dossier(panel):
	grid, terminal = panel
	grid._width = 200

	grid.track_width(_Ctx(terminal=terminal))

	assert grid._width == terminal.size[0]
	assert grid._width != 200


def test_the_panel_shows_a_bar_and_the_models_output_while_reading(panel):
	"""Twenty-nine seconds of nothing is not feedback: while a call is being
	read the dossier shows elapsed time and what the model has written."""
	grid, terminal = panel
	grid._width = 170
	grid.refining = True
	grid._activity_topic = grid._selected_call()['topic_code']
	grid._refine_started = discovery_panel.time.monotonic() - 12
	grid._refine_live = '{\n "deadline": {\n  "value": "02.09.2026"'

	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'READING' in out
	assert '12s' in out
	# The stream itself, not just a spinner.
	assert '02.09.2026' in out


def test_the_tick_keeps_the_bar_moving_without_rereading_sources(panel, monkeypatch):
	grid, _ = panel
	reads = []
	monkeypatch.setattr(grid, '_paint_sources', lambda: reads.append(1))
	grid.refining = True
	grid._dirty = False

	grid.refresh_view()

	# The clock advances every tick; the sources do not, and re-reading them
	# from disk five times a second would be waste.
	assert reads == []


def test_the_live_buffer_is_cleared_when_the_read_finishes(panel, monkeypatch):
	grid, _ = panel
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: None)
	monkeypatch.setattr(
		discovery_panel.refinement,
		'refine',
		lambda call, on_text=None, use_cache=True: (on_text('partial output'), REFINED)[1],
	)

	grid._refine_worker(OPEN_CALL)

	assert grid._refine_live == ''
	assert grid.refining is False


def _select_a_german_notice(grid):
	"""OPEN-1, as a DFG notice: d only fetches from German pages, because EU
	topic files sit on the portal."""
	grid.selected = next(
		i for i, r in enumerate(grid.opportunities) if r['call']['topic_code'] == 'OPEN-1'
	)
	row = grid.opportunities[grid.selected]
	row['call'] = {**row['call'], 'provider': 'dfg'}


def test_d_pulls_the_files_a_call_page_links_to(panel, monkeypatch):
	grid, terminal = panel
	saved = []
	monkeypatch.setattr(
		discovery_panel.german_funding, 'fetch', lambda url, provider: '<html>page</html>'
	)
	monkeypatch.setattr(
		discovery_panel.documents,
		'discover',
		lambda call, html: [
			{'url': 'https://x/a.pdf', 'filename': 'a.pdf', 'kind': 'template'},
			{'url': 'https://x/b.pdf', 'filename': 'b.pdf', 'kind': 'guidance'},
		],
	)
	monkeypatch.setattr(
		discovery_panel.documents, 'download', lambda call, doc: saved.append(doc['filename'])
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	_select_a_german_notice(grid)

	terminal.actions.keyboard('d')

	assert saved == ['a.pdf', 'b.pdf']
	assert grid.pulling is False
	assert any('2 of 2 file' in line for line in grid._log)


def test_one_unreachable_file_does_not_stop_the_others(panel, monkeypatch):
	grid, terminal = panel
	saved = []

	def download(call, doc):
		if doc['filename'] == 'a.pdf':
			raise ValueError('over the cap')
		saved.append(doc['filename'])

	monkeypatch.setattr(discovery_panel.german_funding, 'fetch', lambda url, provider: '<html/>')
	monkeypatch.setattr(
		discovery_panel.documents,
		'discover',
		lambda call, html: [
			{'url': 'https://x/a.pdf', 'filename': 'a.pdf', 'kind': 'template'},
			{'url': 'https://x/b.pdf', 'filename': 'b.pdf', 'kind': 'guidance'},
		],
	)
	monkeypatch.setattr(discovery_panel.documents, 'download', download)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	_select_a_german_notice(grid)

	terminal.actions.keyboard('d')

	assert saved == ['b.pdf']
	assert any('skipped a.pdf' in line for line in grid._log)


def test_a_page_with_no_documents_says_so(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(discovery_panel.german_funding, 'fetch', lambda url, provider: '<html/>')
	monkeypatch.setattr(discovery_panel.documents, 'discover', lambda call, html: [])
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	_select_a_german_notice(grid)

	terminal.actions.keyboard('d')

	assert any('no documents linked' in line for line in grid._log)
	assert grid.pulling is False


def test_d_is_not_offered_on_an_eu_topic_and_says_where_its_files_are(panel, monkeypatch):
	"""The fetcher may not reach the EU portal, so d on an EU topic only ever
	failed, with an error about the provider."""
	grid, terminal = panel
	fetched = []
	monkeypatch.setattr(
		discovery_panel.german_funding, 'fetch', lambda url, provider: fetched.append(url)
	)
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	grid.selected = next(
		i for i, r in enumerate(grid.opportunities) if r['call']['topic_code'] == 'OPEN-1'
	)

	assert 'd files' not in grid.hint()
	terminal.actions.keyboard('d')

	assert fetched == []
	assert grid.pulling is False
	assert any('portal page' in line for line in grid._log)


def test_the_synopsis_is_shown_and_marked_as_written_not_quoted(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.refinement,
		'cached',
		lambda call: {**REFINED, 'synopsis': 'Funds pilot projects on secure data environments.'},
	)
	grid._width = 170

	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'SYNOPSIS' in out
	assert 'secure data environments' in out
	# A synopsis is generated prose; the panel must not let it read as a quote,
	# and it names the model rather than saying "the model".
	assert 'written by m from the call text' in out


def test_a_synopsis_with_invented_figures_is_reported_as_dropped(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.refinement,
		'cached',
		lambda call: {**REFINED, 'synopsis': None, 'invented_figures': ['01.01.2030']},
	)
	grid._width = 170

	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'synopsis dropped' in out
	assert '01.01.2030' in out


def test_the_synopsis_names_the_model_that_wrote_it(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.refinement,
		'cached',
		lambda call: {**REFINED, 'model': 'demo:14b', 'synopsis': 'Funds pilot projects.'},
	)
	grid._width = 170

	grid._paint_dossier()
	terminal.render(grid)

	# The model that wrote it, which may differ from the one selected now.
	assert 'written by demo:14b' in terminal.get_output()


def test_right_opens_a_call_and_nests_its_concepts_under_it(panel, monkeypatch):
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	assert len(grid._entries) == 2  # both calls, neither open
	terminal.actions.keyboard('right')

	assert [e['kind'] for e in grid._entries] == ['call', 'call', 'concept']
	terminal.render(grid)
	assert 'Glucose forecasting' in terminal.get_output()


def test_concepts_nest_under_the_call_that_produced_them(panel, monkeypatch):
	grid, _ = panel
	stage(
		grid,
		monkeypatch,
		concepts={
			'OPEN-1': [a_concept('j1', '1', 'Belongs to open')],
			'CLOSED-1': [a_concept('j2', '1', 'Belongs to closed')],
		},
	)
	grid._expanded.update({'OPEN-1', 'CLOSED-1'})
	grid._rebuild_tree()

	nested = {
		e['row']['call']['topic_code']: e['item']['concept']['title']
		for e in grid._entries
		if e['kind'] == 'concept'
	}

	assert nested == {'OPEN-1': 'Belongs to open', 'CLOSED-1': 'Belongs to closed'}


def test_the_right_column_follows_what_is_selected(panel, monkeypatch):
	"""No mode to remember: a call shows its dossier, a concept shows itself."""
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('right')  # opens
	terminal.actions.keyboard('right')  # steps onto the concept
	terminal.render(grid)
	assert 'Glucose excursion patterns are hard to predict' in terminal.get_output()

	terminal.actions.keyboard('left')  # back out to the call
	terminal.render(grid)
	assert grid._selected_concept() is None
	assert 'ELIGIBILITY' in terminal.get_output()


def test_only_one_model_job_runs_at_a_time(panel, monkeypatch):
	"""One 12B serves the scrape, the extractor, generation and the panel, and
	the watch loop is a fifth caller. Two at once on one machine thrashes."""
	grid, terminal = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(1)}
		)(),
	)
	grid.refining = True

	terminal.actions.keyboard('g')
	terminal.actions.keyboard('s')
	terminal.actions.keyboard('d')

	assert started == []
	assert any('busy with a call being read' in line for line in grid._log)


def test_generate_refuses_a_call_whose_deadline_has_passed(panel, monkeypatch):
	grid, terminal = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(1)}
		)(),
	)
	grid.selected = next(
		i for i, r in enumerate(grid.opportunities) if r['call']['topic_code'] == 'CLOSED-1'
	)

	terminal.actions.keyboard('g')

	# The same deterministic gate the watch loop applies.
	assert started == []
	assert any('is ineligible' in line for line in grid._log)


def test_generate_marks_an_unconfirmed_call_as_an_exploratory_trial(panel, monkeypatch):
	grid, terminal = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(args)}
		)(),
	)
	grid.selected = next(
		i for i, r in enumerate(grid.opportunities) if r['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('g')

	assert started, 'generation should have started'
	_call, _portfolio, trial = started[0]
	assert trial is True
	assert any('exploratory trial' in line for line in grid._log)


def test_review_needs_a_concept_to_review(panel, monkeypatch):
	grid, terminal = panel
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(1)}
		)(),
	)

	terminal.actions.keyboard('r')  # a call row is selected, not a concept

	assert started == []
	assert any('select a concept first' in line for line in grid._log)


def test_the_arrows_move_through_concepts_and_calls_alike(panel, monkeypatch):
	"""One selection, one pair of arrow keys: a concept is another row."""
	grid, _ = panel
	stage(
		grid,
		monkeypatch,
		concepts={'CLOSED-1': [a_concept('j1', '1', 'One'), a_concept('j1', '2', 'Two')]},
	)
	grid._expanded.add('CLOSED-1')
	grid._rebuild_tree()
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'CLOSED-1'
	)

	grid.select_next()
	assert grid._selected_concept()['concept']['title'] == 'One'
	grid.select_next()
	assert grid._selected_concept()['concept']['title'] == 'Two'
	grid.select_next()
	assert grid._selected_concept() is None  # out the other side, onto a call


def test_the_call_an_agent_is_working_on_is_marked_in_the_list(panel):
	grid, terminal = panel
	grid.generating = True
	grid._activity_topic = 'OPEN-1'
	grid._activity_started = discovery_panel.time.monotonic()

	grid._paint_opportunities()
	terminal.render(grid)
	rows = [line for line in terminal.get_output().splitlines() if 'Long-term CGM' in line]

	# A spinner beside the row, so it is obvious which call is busy.
	assert rows and any(char in rows[0] for char in discovery_panel.spinner(0) + '⠙⠹⠸⠼⠴⠦⠧⠇⠏')


def test_generation_shows_a_bar_and_the_models_output(panel):
	grid, terminal = panel
	grid.generating = True
	grid._activity_topic = grid._selected_call()['topic_code']
	grid._activity_started = discovery_panel.time.monotonic() - 30
	grid._gen_stage = 'generate'
	grid._gen_live = '{\n "concepts": [\n  {"title": "Glucose forecasting"'

	# Beside the call it is working from, not on a screen you switch to.
	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert '30s' in out
	assert 'generate' in out
	# The stream itself, not just a spinner.
	assert 'Glucose forecasting' in out


def test_a_bar_past_its_budget_waits_short_of_full(panel):
	"""It eases to 95% and waits rather than claim a finish, but round(9.5) is
	10, so a slow read or run drew a full bar for work still going."""
	grid, _ = panel
	grid._refine_started = grid._activity_started = discovery_panel.time.monotonic() - 1000

	for bar in (grid._reading_rows()[0], grid._generating_rows()[0]):
		assert bar[1].text == '█' * 9
		assert bar[2].text == '░'


def test_activity_reports_what_is_running_and_for_how_long(panel):
	grid, _ = panel
	assert grid.activity() == ('', 0.0)

	grid._begin('OPEN-1')
	grid.reviewing = True
	label, elapsed = grid.activity()

	assert label == 'a review round'
	assert elapsed >= 0
	assert grid._activity_topic == 'OPEN-1'

	grid.reviewing = False
	grid._end()
	assert grid.activity()[0] == ''
	assert grid._activity_topic == ''


def test_the_watch_list_shows_how_far_each_call_has_got(panel, monkeypatch):
	"""The list is a work queue, so a row has to say what has been done to it."""
	grid, terminal = panel
	stage(
		grid,
		monkeypatch,
		read={'OPEN-1'},
		concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]},
		decided={'j1/1': 'pursued'},
	)

	terminal.render(grid)
	rows = {
		'open': next(l for l in terminal.get_output().splitlines() if 'Long-term CGM' in l),
		'closed': next(l for l in terminal.get_output().splitlines() if 'deadline has passed' in l),
	}

	# read, concepts, (not reviewed), decided
	assert '●●○●' in rows['open']
	assert '○○○○' in rows['closed']


def test_pursuing_a_concept_logs_the_decision_and_opens_a_draft(panel, monkeypatch):
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	recorded, drafted = [], []
	monkeypatch.setattr(
		discovery_panel.decisions,
		'record',
		lambda *args: recorded.append(args),
	)
	monkeypatch.setattr(
		discovery_panel.proposals,
		'create',
		lambda job_id, concept_id, variant: (
			drafted.append((job_id, concept_id, variant)) or {'id': 'draft-1'}
		),
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	terminal.actions.keyboard('p')

	# The revised concept, which is the one the tree shows.
	assert drafted == [('j1', '1', 'revised')]
	assert recorded == [('develop', 'concept', 'j1/1', 'draft-1')]
	assert any('draft draft-1' in line for line in grid._log)


@pytest.mark.parametrize(('key', 'kind'), [('k', 'save'), ('x', 'dismiss')])
def test_parking_and_dropping_are_logged_without_opening_a_draft(panel, monkeypatch, key, kind):
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	recorded = []
	monkeypatch.setattr(discovery_panel.decisions, 'record', lambda *args: recorded.append(args))
	monkeypatch.setattr(
		discovery_panel.proposals,
		'create',
		lambda *a: pytest.fail('only pursuing opens a draft'),
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	terminal.actions.keyboard(key)

	assert recorded == [(kind, 'concept', 'j1/1', None)]


def test_a_decision_key_on_a_call_row_asks_for_a_concept(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.decisions, 'record', lambda *a: pytest.fail('nothing is selected')
	)

	terminal.actions.keyboard('p')

	assert any('select a concept first' in line for line in grid._log)


def test_a_draft_that_cannot_be_opened_records_no_decision(panel, monkeypatch):
	"""The decision and the draft go together: half of a pursue is worse than
	none, because the log would claim work that does not exist."""
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	monkeypatch.setattr(
		discovery_panel.decisions, 'record', lambda *a: pytest.fail('no draft, no decision')
	)

	def boom(*_args):
		raise ValueError('Choose a completed concept run.')

	monkeypatch.setattr(discovery_panel.proposals, 'create', boom)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	terminal.actions.keyboard('p')

	assert any('could not open a draft' in line for line in grid._log)


def test_the_footer_hint_names_only_what_the_selection_can_do(panel, monkeypatch):
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	on_closed = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'CLOSED-1'
	)
	grid.selected = on_closed

	# A call row offers the call's own keys, never a concept's.
	assert 'e read the call' in grid.hint()
	assert 'p pursue' not in grid.hint()

	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)
	assert '→ 1 concepts' in grid.hint()

	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	assert 'p pursue' in grid.hint()
	assert 'g develop' not in grid.hint()


def test_developing_a_call_nobody_has_read_reads_it_first(panel, monkeypatch):
	"""Otherwise the dossier stays blank behind the concepts it produced."""
	grid, _ = panel
	order = []
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: None)
	monkeypatch.setattr(
		discovery_panel.refinement,
		'refine',
		lambda call, on_text=None: (
			order.append('read')
			or {'fields': {'deadline': {}}, 'dropped': ['consortium'], 'synopsis': ''}
		),
	)
	monkeypatch.setattr(
		discovery_panel.concepts,
		'start_job',
		lambda *a, **k: order.append('develop') or {'id': 'job-1'},
	)
	monkeypatch.setattr(discovery_panel.concepts, 'get_job', lambda job_id: {'status': 'succeeded'})

	grid._generate_worker(OPEN_CALL, PORTFOLIO, trial=False)

	assert order == ['read', 'develop']
	assert grid.refining is False
	# The read reports what it kept, the same way the explicit key does.
	assert any('deadline' in line and 'dropped 1' in line for line in grid._log)


def test_a_call_already_read_is_not_read_again_before_developing(panel, monkeypatch):
	grid, _ = panel
	order = []
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: {'fields': {}})
	monkeypatch.setattr(
		discovery_panel.refinement, 'refine', lambda *a, **k: pytest.fail('already read')
	)
	monkeypatch.setattr(
		discovery_panel.concepts,
		'start_job',
		lambda *a, **k: order.append('develop') or {'id': 'job-1'},
	)
	monkeypatch.setattr(discovery_panel.concepts, 'get_job', lambda job_id: {'status': 'succeeded'})

	grid._generate_worker(OPEN_CALL, PORTFOLIO, trial=False)

	assert order == ['develop']


def test_the_generation_clock_restarts_after_a_read(panel, monkeypatch):
	"""Reading first takes about half a minute. Left running, the clock the bar
	reads would open it a third full before generation had begun."""
	grid, _ = panel
	stamps = []
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: None)
	monkeypatch.setattr(
		discovery_panel.refinement,
		'refine',
		lambda call, on_text=None: {'fields': {}, 'dropped': [], 'synopsis': ''},
	)
	monkeypatch.setattr(
		discovery_panel.concepts,
		'start_job',
		lambda *a, **k: stamps.append(grid._activity_started) or {'id': 'job-1'},
	)
	monkeypatch.setattr(discovery_panel.concepts, 'get_job', lambda job_id: {'status': 'succeeded'})
	grid._begin('OPEN-1')
	grid._activity_started -= 30  # as though the key had been pressed 30s ago
	pressed_at = grid._activity_started

	grid._generate_worker(OPEN_CALL, PORTFOLIO, trial=False)

	assert stamps[0] > pressed_at + 20


def test_the_footer_does_not_offer_a_key_the_gate_will_refuse(panel):
	grid, _ = panel
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'CLOSED-1'
	)

	# CLOSED-1's deadline passed, so g refuses; saying otherwise wastes a press.
	assert 'g develop' not in grid.hint()
	assert 'not eligible' in grid.hint()
	# Once: b refuses too, and the gate said twice read as a stutter.
	assert grid.hint().count('not eligible') == 1


def test_progress_is_only_drawn_on_the_call_being_worked_on(panel):
	"""A bar under the wrong call's dossier reads as that call being read."""
	grid, terminal = panel
	grid._width = 170
	grid.refining = True
	grid._refine_started = discovery_panel.time.monotonic() - 12
	grid._refine_live = 'extracted from the other call'
	# An agent is reading CLOSED-1 while the researcher looks at OPEN-1.
	grid._activity_topic = 'CLOSED-1'
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	grid._paint_dossier()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'READING' not in out
	assert 'extracted from the other call' not in out


def test_a_worker_rebuilding_the_tree_keeps_the_cursor_on_its_row(panel, monkeypatch):
	"""The cursor used to be restored by index. A generate run finishing under
	an open call shifted every row below it, so the next p/k/x decided about a
	concept the researcher had never opened."""
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'CLOSED-1': [a_concept('j1', '1', 'First')]})
	grid._expanded.add('CLOSED-1')
	grid._rebuild_tree()
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	# A worker lands with two more concepts for the call above.
	stage(
		grid,
		monkeypatch,
		concepts={
			'CLOSED-1': [
				a_concept('j1', '1', 'First'),
				a_concept('j1', '2', 'Second'),
				a_concept('j1', '3', 'Third'),
			]
		},
	)

	assert grid._entry()['row']['call']['topic_code'] == 'OPEN-1'
	assert grid._selected_concept() is None


def test_a_review_is_shown_only_under_the_concept_it_was_asked_about(panel, monkeypatch):
	grid, terminal = panel
	stage(
		grid,
		monkeypatch,
		concepts={'OPEN-1': [a_concept('j1', '1', 'Asked about'), a_concept('j1', '2', 'Other')]},
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	first, second = (i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	grid.selected = first
	grid.reviewing = True
	grid._review_key = 'j1/1'
	grid._review_live = {'methods': 'a verdict about the first concept'}

	grid._sync_panes()
	grid._paint_concept()
	terminal.render(grid)
	assert 'a verdict about the first concept' in terminal.get_output()

	grid.selected = second
	grid._paint_concept()
	# A fresh buffer: an offscreen terminal is not cleared between renders, so
	# reusing it would show the previous frame's cells and pass either way.
	after = Terminal.offscreen(cols=180, rows=46)
	after.attach_grid(grid)
	after.render(grid)
	assert 'a verdict about the first concept' not in after.get_output()


@pytest.mark.parametrize(('cols', 'rows'), [(186, 46), (150, 44), (120, 40), (100, 30), (90, 26)])
def test_the_screen_still_works_on_a_small_terminal(panel, monkeypatch, cols, rows):
	"""Heights that suit a 46-row window pushed the footer off a 30-row one, and
	three reviewer panes side by side are unreadable under about 150 columns."""
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid._width, grid._rows = cols, rows
	grid.apply_layout()
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	grid._after_move()

	terminal = Terminal.offscreen(cols=cols, rows=rows)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	# The concept you selected is always on screen, whatever the window. The
	# prose itself rewraps, so the labels are what can be asserted across widths.
	assert 'PROBLEM' in out
	assert 'STATE' in out
	# The reviewer panes give way to the consolidated verdict, never to nothing.
	assert ('Methodology & evaluation' in out) == (cols >= discovery_panel.WIDE_ENOUGH_FOR_PANES)
	assert 'No review yet' in out


@pytest.mark.parametrize('cols', [250, 200, 150, 126, 125, 124, 120, 100, 90, 80])
def test_the_whole_footer_fits_the_window_at_every_usable_width(panel, monkeypatch, cols):
	"""It was switching to the short wording below 120 columns while the long one
	was 124 characters, so the keys fell off the end between 120 and 124. Eighty
	columns is the floor: the watch list alone is 52 wide."""
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid._width = cols

	for index in range(len(grid._entries)):
		grid.selected = index
		footer = f'  {grid.hint()}   \u00b7   {discovery_panel.GLOBAL_KEYS}'
		assert len(footer) <= cols, f'{len(footer)} chars in {cols} columns: {footer}'


def test_the_footer_keeps_the_most_important_keys_when_it_has_to_shorten(panel, monkeypatch):
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	grid._width = 200
	wide = grid.hint()
	grid._width = 90
	narrow = grid.hint()

	assert len(narrow) < len(wide)
	# Reviewing and pursuing are the two the screen exists for.
	for keep in ('r review', 'p pursue', 'w write-up'):
		assert keep in narrow


def test_a_concept_already_reviewed_shows_its_stored_round(panel, monkeypatch):
	"""Selecting it used to give an empty pane, as though the panel had never run."""
	grid, terminal = panel
	stage(
		grid,
		monkeypatch,
		concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]},
		reviewed={'j1/1': {'id': 'round-1', 'outcome': 'revise'}},
	)
	monkeypatch.setattr(
		discovery_panel.reviews,
		'load',
		lambda round_id: {
			'id': round_id,
			'consolidated': {
				'outcome': 'revise',
				'tally': {'fund': 0, 'revise': 1, 'reject': 0},
				'unsupported_claims': [],
				'questions': [],
			},
			'reviews': {
				'methods': {
					'status': 'succeeded',
					'result': {
						'verdict': 'revise',
						'verdict_reason': 'needs a sample size',
						'strengths': [],
						'weaknesses': ['no power analysis'],
						'questions_for_applicant': [],
					},
				}
			},
		},
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	grid._after_move()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'PANEL OUTCOME  REVISE' in out
	assert 'no power analysis' in out
	assert grid._review_key == 'j1/1'


def test_a_round_that_will_not_load_leaves_the_pane_empty_rather_than_raising(panel, monkeypatch):
	grid, _ = panel
	stage(
		grid,
		monkeypatch,
		concepts={'OPEN-1': [a_concept('j1', '1', 'X')]},
		reviewed={'j1/1': {'id': 'gone', 'outcome': 'fund'}},
	)

	def missing(round_id):
		raise OSError('no such round')

	monkeypatch.setattr(discovery_panel.reviews, 'load', missing)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	grid._after_move()

	assert grid._round is None


def test_left_folds_a_call_shut_again(panel, monkeypatch):
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('right')
	assert grid._expanded == {'OPEN-1'}

	terminal.actions.keyboard('left')

	assert grid._expanded == set()
	assert [e['kind'] for e in grid._entries] == ['call', 'call']


def test_right_on_a_call_with_no_concepts_says_so(panel):
	grid, terminal = panel

	terminal.actions.keyboard('right')

	assert grid._expanded == set()
	assert any('press g to develop' in line for line in grid._log)


def test_a_scrape_puts_newly_imported_calls_into_the_tree(panel, monkeypatch):
	"""The tree is what the selection indexes, so a rebuilt watch list that
	never reaches it leaves the new call invisible."""
	grid, _ = panel
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	third = {
		'topic_code': 'NEW-1',
		'title': 'A notice imported by the scrape',
		'scope_text': 'long-term CGM glucose',
		'deadlines': ['2099-01-01T17:00:00'],
	}

	def fake_discover(max_imports, on_event=None):
		monkeypatch.setattr(
			catalog, 'watched_calls', lambda *a, **k: [CLOSED_CALL, OPEN_CALL, third]
		)

	monkeypatch.setattr(discovery_panel.watch, 'discover', fake_discover)

	grid.scrape()

	codes = [e['row']['call']['topic_code'] for e in grid._entries]
	assert 'NEW-1' in codes
	assert len(grid._entries) == 3


def test_moving_off_a_concept_takes_its_pane_away(panel, monkeypatch):
	grid, terminal = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	grid._after_move()
	terminal.render(grid)
	assert 'Glucose excursion patterns' in terminal.get_output()

	grid.select_previous()

	after = Terminal.offscreen(cols=180, rows=46)
	after.attach_grid(grid)
	after.render(grid)
	assert 'Glucose excursion patterns' not in after.get_output()
	assert 'ELIGIBILITY' in after.get_output()


def test_the_up_key_walks_back_out_of_a_calls_concepts(panel, monkeypatch):
	grid, _ = panel
	stage(
		grid,
		monkeypatch,
		concepts={'CLOSED-1': [a_concept('j1', '1', 'One'), a_concept('j1', '2', 'Two')]},
	)
	grid._expanded.add('CLOSED-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept') + 1

	assert grid._selected_concept()['concept']['title'] == 'Two'
	grid.select_previous()
	assert grid._selected_concept()['concept']['title'] == 'One'
	grid.select_previous()
	assert grid._selected_concept() is None
	grid.select_previous()
	assert grid.selected == 0  # clamps rather than wrapping


def test_the_tick_paints_what_a_worker_left_behind(panel, monkeypatch):
	"""The only path from a finished worker to the screen."""
	grid, _ = panel
	painted = []
	monkeypatch.setattr(grid, 'repaint', lambda: painted.append(1))
	grid._dirty = True

	grid.refresh_view()

	assert painted == [1]
	assert grid._dirty is False


def test_r_runs_a_round_and_clears_the_flag_even_when_it_fails(panel, monkeypatch):
	grid, _ = panel
	stage(grid, monkeypatch, concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]})
	discovery_panel.JOBS_DIR.mkdir(parents=True, exist_ok=True)
	(discovery_panel.JOBS_DIR / 'j1.json').write_text('{"id": "j1", "status": "succeeded"}')
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	monkeypatch.setattr(discovery_panel.reviews, 'from_concept', lambda job, cid: {'title': 'x'})
	monkeypatch.setattr(
		discovery_panel.reviews,
		'run_round',
		lambda doc, on_text=None, on_done=None: {'consolidated': {'outcome': 'fund'}},
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')

	grid.review_concept()

	assert grid.reviewing is False
	assert grid._round == {'consolidated': {'outcome': 'fund'}}
	assert any('panel says fund' in line for line in grid._log)

	def boom(doc, on_text=None, on_done=None):
		raise RuntimeError('ollama is down')

	monkeypatch.setattr(discovery_panel.reviews, 'run_round', boom)
	grid.review_concept()

	# Left True, every later keypress would report "busy with a review round".
	assert grid.reviewing is False
	assert any('review failed' in line for line in grid._log)


def test_the_dossier_surfaces_an_unrecorded_deadline_and_the_source_warning(monkeypatch):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(
		discovery_panel.lifecycle,
		'snapshot',
		lambda calls: {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()},
	)
	monkeypatch.setattr(
		catalog,
		'watched_calls',
		lambda *a, **k: [
			{
				'topic_code': 'DFG-9',
				'title': 'A notice with its date in the prose',
				'scope_text': 'long-term CGM. Abgabetermin: 30.06.2026 fuer Antraege.',
				'deadlines': [],
				'provider': 'dfg',
				'source_warning': 'Imported automatically and not verified against the source.',
			}
		],
	)
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda provider: {'name': provider, 'items': [], 'fetched_at': None, 'error': None},
	)
	grid = discovery_panel.Discovery()
	terminal = Terminal.offscreen(cols=180, rows=46)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	assert 'none recorded' in out
	assert '30.06.2026' in out
	# Never presented as the deadline: it was read out of prose, not a field.
	assert 'unverified' in out
	assert 'Imported automatically' in out


def _on_a_concept(grid, monkeypatch, **snapshot):
	stage(
		grid,
		monkeypatch,
		concepts={'OPEN-1': [a_concept('j1', '1', 'Glucose forecasting')]},
		**snapshot,
	)
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	grid._after_move()


def test_w_writes_the_briefing_and_the_pane_then_points_at_it(panel, monkeypatch, tmp_path):
	grid, terminal = panel
	monkeypatch.setattr(discovery_panel.briefing, 'ROOT', tmp_path / 'briefings')
	monkeypatch.setattr(discovery_panel.proposals, 'list_drafts', lambda job_id: [])
	_on_a_concept(grid, monkeypatch)

	terminal.actions.keyboard('w')

	written = list((tmp_path / 'briefings').glob('*.md'))
	assert [p.name for p in written] == ['OPEN-1-j1-1.md']
	assert 'Glucose forecasting' in written[0].read_text()
	assert any('wrote' in line and 'OPEN-1-j1-1.md' in line for line in grid._log)

	grid._paint_concept()
	terminal.render(grid)
	assert 'WRITE-UP' in terminal.get_output()


def test_w_on_a_call_row_asks_for_a_concept(panel, monkeypatch, tmp_path):
	grid, terminal = panel
	monkeypatch.setattr(discovery_panel.briefing, 'ROOT', tmp_path / 'briefings')

	terminal.actions.keyboard('w')

	assert not (tmp_path / 'briefings').exists()
	assert any('select a concept first' in line for line in grid._log)


def test_u_takes_back_the_standing_decision_without_editing_the_log(panel, monkeypatch):
	grid, terminal = panel
	recorded = []
	monkeypatch.setattr(discovery_panel.decisions, 'record', lambda *args: recorded.append(args))
	_on_a_concept(grid, monkeypatch, decided={'j1/1': 'parked'})

	terminal.actions.keyboard('u')

	# A new event, never a deletion: the log stays a faithful record.
	assert recorded == [('unsave', 'concept', 'j1/1')]
	assert any('undid parked' in line for line in grid._log)


def test_undoing_a_pursue_says_the_draft_is_still_there(panel, monkeypatch):
	grid, terminal = panel
	recorded = []
	monkeypatch.setattr(discovery_panel.decisions, 'record', lambda *args: recorded.append(args))
	_on_a_concept(grid, monkeypatch, decided={'j1/1': 'pursued'})

	terminal.actions.keyboard('u')

	assert recorded == [('undevelop', 'concept', 'j1/1')]
	assert any('its draft is still on disk' in line for line in grid._log)


def test_u_on_an_undecided_concept_does_nothing(panel, monkeypatch):
	grid, terminal = panel
	monkeypatch.setattr(
		discovery_panel.decisions, 'record', lambda *a: pytest.fail('nothing to undo')
	)
	_on_a_concept(grid, monkeypatch)

	terminal.actions.keyboard('u')

	assert any('nothing to undo' in line for line in grid._log)


def test_a_concept_from_the_private_archive_is_marked_in_the_tree_and_the_pane(panel, monkeypatch):
	grid, terminal = panel
	archived = a_concept('j1', '1', 'Built from the archive')
	archived['archive'] = True
	stage(grid, monkeypatch, concepts={'OPEN-1': [archived]})
	grid._expanded.add('OPEN-1')
	grid._rebuild_tree()
	grid.selected = next(i for i, e in enumerate(grid._entries) if e['kind'] == 'concept')
	grid._after_move()
	terminal.render(grid)
	out = terminal.get_output()

	assert '‡' in out
	assert 'private proposal archive' in out


def test_a_resize_rewraps_the_concept_pane_as_well_as_the_dossier(panel, monkeypatch):
	"""Both are wrapped by hand to the same width. Only the dossier was
	repainted, so a concept kept its old line breaks until something else
	happened to repaint it."""
	grid, _ = panel
	_on_a_concept(grid, monkeypatch)

	def widest(rows):
		return max((sum(len(run.text) for run in row) for row in rows.rows), default=0)

	before = widest(grid.right.concept_view.detail)

	class _Ctx:
		class terminal:
			size = (90, 30)

	grid.track_width(_Ctx())

	assert grid._width == 90
	assert widest(grid.right.concept_view.detail) < before


def test_the_watch_list_scrolls_to_keep_the_cursor_on_screen(monkeypatch):
	"""It simply ran past the bottom of its pane: with a call open the cursor
	could sit well below the last visible row, and p, k or x then decided about
	a concept nobody could see."""
	many = [
		{
			'topic_code': f'CALL-{i:02d}',
			'title': f'Call number {i}',
			'scope_text': 'long-term CGM glucose forecasting',
			'deadlines': ['2099-01-01T17:00:00'],
		}
		for i in range(40)
	]
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: many)
	monkeypatch.setattr(discovery_panel.lifecycle, 'snapshot', lambda calls: dict(BLANK))
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda p: {'name': p, 'items': [], 'fetched_at': None, 'error': None},
	)
	grid = discovery_panel.Discovery()
	grid._rows = 30
	capacity = grid._list_capacity()
	assert capacity < len(many), 'the list has to overflow for this to mean anything'

	def painted_cursor_is_visible():
		grid._paint_opportunities()
		shown = grid.left.opportunities.rows
		assert len(shown) <= capacity
		return any(row and row[0].text.startswith('▌') for row in shown)

	assert painted_cursor_is_visible()

	for _ in range(len(many) - 1):
		grid.select_next()
		assert painted_cursor_is_visible(), f'cursor lost at entry {grid.selected}'

	for _ in range(len(many) - 1):
		grid.select_previous()
		assert painted_cursor_is_visible(), f'cursor lost coming back at {grid.selected}'

	assert grid._top_row == 0


def test_a_list_that_fits_is_not_windowed(panel):
	grid, _ = panel

	grid._paint_opportunities()

	assert grid._top_row == 0
	assert len(grid.left.opportunities.rows) == len(grid._entries) + 1  # one source header


ROUND_WITH_A_DEAD_REVIEWER = {
	'consolidated': {
		'outcome': 'revise',
		'tally': {'fund': 0, 'revise': 2, 'reject': 0},
		'failed_roles': ['fit'],
		'weaknesses': [{'role': 'methods', 'weakness': 'no power analysis'}],
		'unsupported_claims': [],
		'questions': [],
	},
	'reviews': {},
}


def test_the_collapsed_view_keeps_the_weaknesses_and_says_a_reviewer_failed(panel, monkeypatch):
	"""Below the width where the three panes fit, this is the only thing on
	screen. Without them a partial panel read as a complete one."""
	grid, _ = panel
	_on_a_concept(grid, monkeypatch)
	grid._width, grid._rows = 120, 40
	grid.apply_layout()
	grid._review_key = 'j1/1'
	grid._round = ROUND_WITH_A_DEAD_REVIEWER

	grid._paint_concept()
	# A fresh terminal at the size under test: an offscreen buffer is not cleared
	# between renders and the fixture's is 180 wide.
	terminal = Terminal.offscreen(cols=120, rows=40)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	assert 'Methodology & evaluation' not in out  # the panes are gone at this width
	assert 'no power analysis' in out
	assert 'reviewer(s) failed' in out
	assert 'fit' in out


def test_a_round_in_progress_does_not_read_as_one_nobody_asked_for(panel, monkeypatch):
	grid, _ = panel
	_on_a_concept(grid, monkeypatch)
	grid._width, grid._rows = 120, 40
	grid.apply_layout()
	grid.reviewing = True
	grid._review_key = 'j1/1'
	grid._review_live = {'methods': 'the first reviewer has written this much'}

	grid._paint_concept()
	terminal = Terminal.offscreen(cols=120, rows=40)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	assert 'No review yet' not in out
	assert 'The panel is reading it now' in out
	assert 'the first reviewer has written this much' in out


def test_a_reviewer_that_has_landed_is_not_reported_as_still_writing(panel, monkeypatch):
	grid, _ = panel
	_on_a_concept(grid, monkeypatch)
	grid._width, grid._rows = 120, 40
	grid.apply_layout()
	grid.reviewing = True
	grid._review_key = 'j1/1'
	grid._review_live = {'methods': '{"strengths": ["Clear aim"]}', 'fit': '{"strengths"'}
	grid._review_records = {
		'methods': {
			'status': 'succeeded',
			'result': {'verdict': 'revise', 'verdict_reason': 'Needs a power analysis.'},
		},
		'fit': {'status': 'failed', 'error': 'model dropped'},
	}

	grid._paint_concept()
	terminal = Terminal.offscreen(cols=120, rows=40)
	terminal.attach_grid(grid)
	terminal.render(grid)
	out = terminal.get_output()

	assert 'methods: done: revise' in out
	assert 'fit: failed' in out
	assert 'feasibility: waiting for the model' in out


def test_the_reviewer_panes_also_give_way_on_a_short_window(panel, monkeypatch):
	"""At 170 by 30 three bordered boxes 14 rows deep left the concept itself
	three rows, which is not a view of anything."""
	grid, _ = panel
	_on_a_concept(grid, monkeypatch)

	grid._width, grid._rows = 170, 46
	grid.apply_layout()
	assert grid.right.concept_view._grid_field_info('panes').visible is True

	grid._rows = 30
	grid.apply_layout()
	assert grid.right.concept_view._grid_field_info('panes').visible is False


@pytest.mark.parametrize('cols', [200, 170, 150, 120, 100, 90, 80])
def test_the_hand_wrapped_rows_never_claim_more_width_than_the_pane_has(panel, cols):
	"""The old floor of 40 was wider than the pane below about 100 columns, so
	every row was re-wrapped by the renderer and the label columns came apart."""
	grid, _ = panel
	grid._width = cols

	assert grid._dossier_width() <= cols - discovery_panel.LEFT_COLUMN - 5


def test_the_scrape_pane_shows_the_newest_lines_not_the_oldest(panel):
	"""It does not scroll itself, and on a short window it is three rows deep,
	so showing the top of the last two hundred lines showed nothing useful."""
	grid, _ = panel
	grid._rows = 30
	grid.apply_layout()
	for i in range(40):
		grid._log_event(f'step {i:02d}')

	grid._paint_log()
	shown = grid.right.log.content

	assert 'step 39' in shown
	assert 'step 00' not in shown
	assert len(shown.splitlines()) <= grid._log_height


def test_a_line_wider_than_the_pane_does_not_hide_the_one_after_it(panel):
	"""The pane counted lines, not rows. g's start line wraps at 160 columns,
	so the line saying the run had finished never appeared."""
	grid, _ = panel
	grid._width, grid._rows = 160, 48
	grid.apply_layout()
	for i in range(6):
		grid._log_event(f'step {i}')
	grid._log_event(
		'generating concepts for HORIZON-HLTH-2027-02-TOOL-01-two-stage '
		'(exploratory trial, eligibility unconfirmed)'
	)
	grid._log_event('a1b2c3d4e5f6: 3 concepts')

	grid._paint_log()
	terminal = Terminal.offscreen(cols=160, rows=48)
	terminal.attach_grid(grid)
	terminal.render(grid)

	assert 'a1b2c3d4e5f6: 3 concepts' in terminal.get_output()


def test_shrinking_the_window_brings_the_cursor_back_into_view(monkeypatch):
	"""The visible window is derived from the row count, so a resize that makes
	the pane shorter left the cursor outside it until the next keypress."""
	many = [
		{
			'topic_code': f'CALL-{i:02d}',
			'title': f'Call number {i}',
			'scope_text': 'long-term CGM glucose forecasting',
			'deadlines': ['2099-01-01T17:00:00'],
		}
		for i in range(40)
	]
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: many)
	monkeypatch.setattr(discovery_panel.lifecycle, 'snapshot', lambda calls: dict(BLANK))
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda p: {'name': p, 'items': [], 'fetched_at': None, 'error': None},
	)
	grid = discovery_panel.Discovery()
	grid._rows = 46
	grid.selected = 30
	grid._paint_opportunities()
	assert any(row and row[0].text.startswith('▌') for row in grid.left.opportunities.rows)

	class _Ctx:
		class terminal:
			size = (120, 24)

	grid.track_width(_Ctx())

	assert any(row and row[0].text.startswith('▌') for row in grid.left.opportunities.rows)


def _chain_spies(grid, monkeypatch, concepts_after=None):
	"""Record the order the chain runs its stages in, without running any."""
	order = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	monkeypatch.setattr(
		grid, '_generate_worker', lambda call, portfolio, trial: order.append(('develop', trial))
	)
	monkeypatch.setattr(grid, '_review_worker', lambda item: order.append('review'))
	monkeypatch.setattr(grid, '_write_briefing_for', lambda row, item: order.append('write'))
	monkeypatch.setattr(
		grid, '_concepts_of', lambda call: concepts_after if concepts_after is not None else []
	)
	monkeypatch.setattr(
		discovery_panel.decisions, 'record', lambda *a: pytest.fail('the chain must not decide')
	)
	return order


def test_b_reads_develops_reviews_and_writes_in_that_order(panel, monkeypatch):
	grid, terminal = panel
	order = _chain_spies(
		grid, monkeypatch, concepts_after=[a_concept('j1', '1', 'Made by the chain')]
	)
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('b')

	assert order == [('develop', True), 'review', 'write']
	assert 'OPEN-1' in grid._expanded
	assert any('read, develop, review, write-up' in line for line in grid._log)


def test_b_stops_before_the_decision(panel, monkeypatch):
	"""The decision log records what the researcher chose. A machine working
	overnight cannot choose on their behalf."""
	grid, terminal = panel
	_chain_spies(grid, monkeypatch, concepts_after=[a_concept('j1', '1', 'X')])
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('b')  # decisions.record would fail the test

	assert (
		discovery_panel.lifecycle.concept_state(a_concept('j1', '1', 'X'), grid._stages)
		== 'not reviewed'
	)


def test_b_refuses_a_call_whose_deadline_has_passed(panel, monkeypatch):
	grid, terminal = panel
	order = _chain_spies(grid, monkeypatch)
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'CLOSED-1'
	)

	terminal.actions.keyboard('b')

	assert order == []
	assert any('not working it through' in line for line in grid._log)


def test_b_stops_at_the_review_when_nothing_was_developed(panel, monkeypatch):
	grid, terminal = panel
	order = _chain_spies(grid, monkeypatch, concepts_after=[])
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('b')

	assert order == [('develop', True)]
	assert any('no concepts came back' in line for line in grid._log)


def test_b_waits_its_turn_like_every_other_model_job(panel, monkeypatch):
	grid, terminal = panel
	order = _chain_spies(grid, monkeypatch)
	grid.refining = True

	terminal.actions.keyboard('b')

	assert order == []
	assert any('busy with a call being read' in line for line in grid._log)


def test_the_chain_starts_exactly_one_concept_run(panel, monkeypatch):
	"""Through the real _generate_worker, not a stub: a chain that started a
	second run would put two jobs on one call and two loads on one model."""
	grid, terminal = panel
	starts = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	monkeypatch.setattr(discovery_panel.refinement, 'cached', lambda call: {'fields': {}})
	monkeypatch.setattr(
		discovery_panel.concepts,
		'start_job',
		lambda *a, **k: starts.append(a[0]['topic_code']) or {'id': 'job-1'},
	)
	monkeypatch.setattr(
		discovery_panel.concepts, 'get_job', lambda job_id: {'status': 'succeeded', 'stages': []}
	)
	monkeypatch.setattr(grid, '_review_worker', lambda item: None)
	monkeypatch.setattr(grid, '_write_briefing_for', lambda row, item: None)
	monkeypatch.setattr(grid, '_concepts_of', lambda call: [a_concept('job-1', '1', 'X')])
	grid.selected = next(
		i for i, e in enumerate(grid._entries) if e['row']['call']['topic_code'] == 'OPEN-1'
	)

	terminal.actions.keyboard('b')

	assert starts == ['OPEN-1']


def _many_calls(monkeypatch, titles):
	monkeypatch.setattr(discovery_panel, 'load_portfolio', lambda: PORTFOLIO)
	monkeypatch.setattr(
		catalog,
		'watched_calls',
		lambda *a, **k: [
			{
				'topic_code': f'CALL-{i:02d}',
				'title': title,
				'scope_text': 'long-term CGM glucose forecasting',
				'deadlines': ['2099-01-01T17:00:00'],
			}
			for i, title in enumerate(titles)
		],
	)
	monkeypatch.setattr(discovery_panel.lifecycle, 'snapshot', lambda calls: dict(BLANK))
	monkeypatch.setattr(
		discovery_panel.german_funding,
		'source_status',
		lambda p: {'name': p, 'items': [], 'fetched_at': None, 'error': None},
	)
	return discovery_panel.Discovery()


class _FocusCtx:
	def focus(self, group):
		self.focused = group

	def blur(self):
		self.focused = None


def test_typing_a_filter_narrows_the_watch_list(monkeypatch):
	"""On a long watch list, reaching a call in the middle took dozens of presses
	of the down arrow, which is most of the work of using this screen."""
	grid = _many_calls(monkeypatch, ['Gut microbiome', 'Quantum optics', 'Meal timing'])
	ctx = _FocusCtx()

	grid.begin_filter(ctx)
	assert ctx.focused == 'filter_box'
	assert grid.left.filter_box.input is True

	grid.left.filter_box.content = 'meal'
	grid.refresh_view()

	assert [e['row']['call']['title'] for e in grid._entries] == ['Meal timing']


def test_a_filter_matches_the_topic_code_too(monkeypatch):
	grid = _many_calls(monkeypatch, ['One', 'Two', 'Three'])
	grid._filter = 'call-01'
	grid._rebuild_tree()

	assert [e['row']['call']['topic_code'] for e in grid._entries] == ['CALL-01']


def test_enter_keeps_the_filter_and_escape_clears_it(monkeypatch):
	grid = _many_calls(monkeypatch, ['Gut microbiome', 'Quantum optics'])
	ctx = _FocusCtx()
	grid.begin_filter(ctx)
	grid.left.filter_box.content = 'quantum'

	grid.on_enter(ctx)

	assert grid.filtering is False
	assert grid._filter == 'quantum'
	assert grid.left.filter_box.input is False  # or it eats every later keystroke
	assert len(grid._entries) == 1

	grid.begin_filter(ctx)
	grid.cancel_typing(ctx)

	assert grid._filter == ''
	assert len(grid._entries) == 2


def test_a_filter_that_matches_nothing_says_so_rather_than_going_blank(monkeypatch):
	grid = _many_calls(monkeypatch, ['Gut microbiome'])
	grid._filter = 'astrophysics'
	grid._rebuild_tree()

	grid._paint_opportunities()

	assert 'No call matches' in grid.left.opportunities.rows[0][0].text


def test_no_binding_fires_while_the_filter_box_is_capturing(monkeypatch):
	"""An input field suppresses hooks on its own grid only."""
	grid = _many_calls(monkeypatch, ['One', 'Two'])
	started = []
	monkeypatch.setattr(
		discovery_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type(
			'T', (), {'start': lambda s: started.append(1)}
		)(),
	)
	grid.begin_filter(_FocusCtx())

	grid.scrape()
	grid.generate_concepts()
	grid.brief_call()
	grid.select_next()

	assert started == []
	assert grid.selected == 0


def test_paging_and_the_jump_keys_move_by_more_than_one(monkeypatch):
	grid = _many_calls(monkeypatch, [f'Call {i}' for i in range(40)])
	grid._rows = 30

	grid.page_down()
	assert grid.selected == grid._list_capacity()

	grid.go_to_bottom()
	assert grid.selected == len(grid._entries) - 1

	grid.page_up()
	assert grid.selected == len(grid._entries) - 1 - grid._list_capacity()

	grid.go_to_top()
	assert grid.selected == 0
