"""GrantBridge in the terminal: the shell that holds every panel.

Screens are child grids. Hidden grids still receive key events in xnano, so the
shell also flips each screen's `active` flag and every screen gates its own
bindings on it. Without that, pressing `s` while the model picker is open would
start a scrape you cannot see.

Run it with:  uv run python -m grant_recommender.app
"""

from xnano import BaseGrid, Field, Terminal
from xnano.components.text import Text
from xnano.core.content import Run
from xnano.hooks import on_keyboard, on_tick

from grant_recommender import concepts
from grant_recommender.discovery_panel import GLOBAL_KEYS as discovery_panel_keys
from grant_recommender.discovery_panel import Discovery
from grant_recommender.models_panel import ModelPicker
from grant_recommender.panel_widgets import ACCENT, MUTED, RowList, spinner

SCREENS = (('discovery', 'Opportunities'),)
# The keys that depend on the selection come from the panel, which is also
# where the always-available ones live so it can measure what room is left.
GLOBAL_KEYS = discovery_panel_keys


class App(BaseGrid, direction='vertical', gap=0):
	tabs: RowList = Field(default_factory=RowList, height=1)
	discovery: Discovery = Field(default_factory=Discovery, height='1fr')
	models: ModelPicker = Field(default_factory=ModelPicker, height='1fr', visible=False)
	footer: Text = Field(default_factory=lambda: Text(content=''), height=1)

	screen: str = Field(default='discovery', state=True)

	def __post_init__(self) -> None:
		self._previous = 'discovery'
		self._badge = ''
		self._hint = None
		self._show('discovery')

	@on_tick(120)
	def show_activity(self) -> None:
		"""A badge in the header whenever an agent is working, so the fact that
		something is running is visible from anywhere on the screen."""
		if self.screen == 'models':
			return
		label, elapsed = self.discovery.activity()
		badge = f'{spinner(elapsed)} {label} · {elapsed:.0f}s' if label else ''
		if badge != self._badge:
			self._badge = badge
			self._paint_tabs(self.screen)
		self._paint_footer()

	def _show(self, name: str) -> None:
		self.screen = name
		# The picker is an overlay, not a numbered screen: it hides the one
		# underneath and gives it back on escape.
		picking = name == 'models'
		self.grid_update_field('models', visible=picking)
		self.models.active = picking
		for key, _label in SCREENS:
			shown = (not picking) and key == name
			self.grid_update_field(key, visible=shown)
			getattr(self, key).active = shown
		if picking:
			self.models.open()
			self.tabs = RowList(
				rows=(
					(
						Run(text='  GrantBridge   ', foreground=ACCENT, modifiers=('bold',)),
						Run(
							text=' ▸ model ',
							foreground='black',
							background=ACCENT,
							modifiers=('bold',),
						),
					),
				)
			)
			self.footer = Text(
				content='  ↑↓ select   enter use or pull   esc back', foreground=MUTED
			)
			# So the contextual footer is repainted on the way back out.
			self._hint = None
			return
		self._paint_tabs(name)
		self._paint_footer()

	def _paint_footer(self) -> None:
		"""Only the keys that do something to whatever is selected, plus the ones
		that always work. Ten undifferentiated bindings taught nobody the order
		the work happens in."""
		hint = self.discovery.hint()
		if hint == self._hint:
			return
		self._hint = hint
		# An open text box takes the keys that otherwise always work.
		keys = hint if self.discovery.typing else f'{hint}   ·   {GLOBAL_KEYS}'
		self.footer = Text(content=f'  {keys}', foreground=MUTED)

	def _paint_tabs(self, name: str) -> None:
		# A filled tab plus a leading arrow: a trailing dot was too easy to miss.
		runs = [Run(text='  GrantBridge   ', foreground=ACCENT, modifiers=('bold',))]
		for key, label in SCREENS:
			runs.append(
				Run(
					text=f' ▸ {label} ' if key == name else f'   {label} ',
					foreground='black' if key == name else MUTED,
					background=ACCENT if key == name else None,
					modifiers=('bold',) if key == name else (),
				)
			)
			runs.append(Run(text=' '))
		# Always visible: which local model everything here is talking to, and
		# whether an agent is working right now.
		runs.append(Run(text=f'   {concepts.connection_settings()["model"]}', foreground=MUTED))
		if self._badge:
			runs.append(Run(text=f'    {self._badge}', foreground=ACCENT, modifiers=('bold',)))
		self.tabs = RowList(rows=(tuple(runs),))

	# Every shell binding is gated on the picker being closed. An input field
	# suppresses hooks on its own grid but not on its parent, so without this
	# typing 'q' into the model search quits the app.
	@on_keyboard('m')
	def show_models(self, ctx) -> None:
		if self.screen != 'models':
			self._previous = self.screen
			self._show('models')
			# Without focus the search box renders but never receives a keystroke.
			ctx.focus('model_search')

	@on_keyboard('escape')
	def leave_models(self, ctx) -> None:
		if self.screen == 'models':
			self.models.close()
			ctx.blur()
			self._show(self._previous)

	@on_keyboard('1')
	def show_discovery(self) -> None:
		if self.screen != 'models':
			self._show('discovery')

	@on_keyboard('q')
	def quit(self, ctx) -> None:
		if self.screen != 'models':
			ctx.terminal.request_exit()


def main() -> None:
	terminal = Terminal(title='GrantBridge')
	terminal.attach_grid(App())
	terminal.run()


if __name__ == '__main__':
	main()
