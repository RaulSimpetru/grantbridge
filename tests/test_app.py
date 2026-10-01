"""The shell that holds the panels. The thing worth testing here is the gate:
xnano delivers key events to hidden grids too, so switching screens has to
deactivate the one that is off, or a keypress fires on both."""

import pytest
from xnano import Terminal

from grant_recommender import app, catalog, discovery_panel


class _Ctx:
	"""Focus calls are what make the picker's search box live; the tests only
	need them not to explode."""

	def focus(self, group):
		self.focused = group

	def blur(self):
		self.focused = None


@pytest.fixture(autouse=True)
def _quiet_discovery(monkeypatch):
	"""The shell's tests are about the shell. Without this every App() scores the
	real watch list and reads every store behind it."""
	monkeypatch.setattr(discovery_panel, 'load_portfolio', list)
	monkeypatch.setattr(catalog, 'watched_calls', lambda *a, **k: [])
	monkeypatch.setattr(
		discovery_panel.lifecycle,
		'snapshot',
		lambda calls: {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()},
	)


def _app():
	grid = app.App()
	terminal = Terminal.offscreen(cols=180, rows=36)
	terminal.attach_grid(grid)
	return grid, terminal


def test_opens_on_discovery_with_only_that_screen_active():
	grid, terminal = _app()
	terminal.render(grid)

	assert grid.screen == 'discovery'
	assert grid.discovery.active is True
	assert 'sources' in terminal.get_output()


def test_m_opens_the_model_picker_over_the_current_screen(monkeypatch):
	monkeypatch.setattr(app.ModelPicker, 'open', lambda self: setattr(self, 'active', True))
	grid, terminal = _app()

	grid.show_models(_Ctx())
	terminal.render(grid)

	assert grid.screen == 'models'
	assert grid.models.active is True
	# The screen underneath is hidden and deactivated, so its keys do not fire.
	assert grid.discovery.active is False


def test_typing_into_the_model_search_cannot_quit_the_app(monkeypatch):
	"""An input field suppresses hooks on its own grid but not on the parent, so
	'q' in the search box used to quit."""
	monkeypatch.setattr(app.ModelPicker, 'open', lambda self: setattr(self, 'active', True))
	grid, _ = _app()
	exited = []

	class Ctx:
		class terminal:
			@staticmethod
			def request_exit():
				exited.append(1)

	grid.show_models(_Ctx())
	grid.quit(Ctx())

	assert exited == []

	grid.leave_models(_Ctx())
	grid.quit(Ctx())
	assert exited == [1]


def test_no_screen_keeps_its_own_quit_binding(monkeypatch):
	"""Quit belongs to the shell. A screen that also binds 'q' fires it even
	while hidden, which is how typing into the model search quit the app."""
	monkeypatch.setattr(app.ModelPicker, 'open', lambda self: setattr(self, 'active', True))
	grid, terminal = _app()
	exited = []
	monkeypatch.setattr(terminal, 'request_exit', lambda: exited.append(1))

	grid.show_models(_Ctx())
	terminal.actions.keyboard('q')

	assert exited == []
	assert grid.screen == 'models'


def test_the_picker_hands_the_screen_back_on_escape(monkeypatch):
	monkeypatch.setattr(app.ModelPicker, 'open', lambda self: setattr(self, 'active', True))
	grid, _ = _app()

	grid.show_models(_Ctx())
	assert grid.discovery.active is False

	grid.leave_models(_Ctx())
	assert grid.screen == 'discovery'
	assert grid.discovery.active is True
	assert grid.models.active is False


def test_the_header_shows_a_badge_while_an_agent_works(monkeypatch):
	import time as _time

	grid, terminal = _app()
	grid.discovery.generating = True
	grid.discovery._activity_started = _time.monotonic() - 12

	grid.show_activity()
	terminal.render(grid)
	header = terminal.get_output().splitlines()[0]

	assert 'concept generation' in header
	assert '12s' in header


def test_the_badge_disappears_when_nothing_is_running():
	grid, terminal = _app()
	grid.discovery.generating = True
	grid.show_activity()

	grid.discovery.generating = False
	grid.show_activity()
	terminal.render(grid)

	assert 'concept generation' not in terminal.get_output().splitlines()[0]


def test_the_footer_is_painted_from_the_panel_and_follows_the_selection(monkeypatch):
	"""A flat row of every binding taught nobody the order the work happens in,
	so the footer names only what the selected row accepts."""
	hints = iter(['e read the call   d files', 'r review   p pursue   k park'])
	grid, terminal = _app()
	monkeypatch.setattr(grid.discovery, 'hint', lambda: next(hints))

	grid._hint = None
	grid._paint_footer()
	terminal.render(grid)
	first = terminal.get_output()
	assert 'e read the call' in first
	# The keys that always work are there too, whatever is selected.
	assert 'm model' in first
	assert 'q quit' in first

	grid.show_activity()
	after = Terminal.offscreen(cols=180, rows=36)
	after.attach_grid(grid)
	after.render(grid)
	assert 'p pursue' in after.get_output()


def test_leaving_the_model_picker_repaints_the_footer(monkeypatch):
	monkeypatch.setattr(app.ModelPicker, 'open', lambda self: setattr(self, 'active', True))
	monkeypatch.setattr(app.ModelPicker, 'close', lambda self: setattr(self, 'active', False))
	grid, _ = _app()
	ctx = _Ctx()

	grid.show_models(ctx)
	assert grid._hint is None  # so the way back out cannot keep the picker's footer

	grid.leave_models(ctx)
	assert grid._hint is not None


def test_an_open_text_box_footer_names_only_its_own_keys():
	"""The box takes every key, so q, m and s only type letters into it, and
	the add box's esc was nowhere on screen: its placeholder never renders."""
	grid, terminal = _app()
	grid.discovery.adding = True

	grid._paint_footer()
	terminal.render(grid)
	out = terminal.get_output()

	assert 'esc cancel' in out
	assert 'q quit' not in out
