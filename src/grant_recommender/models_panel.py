"""Model picker: choose which local model everything here talks to.

Opens over whatever screen you were on. The search box has focus the whole time
it is open, so typing filters and the arrows and enter still reach their
handlers; there is no mode inside the mode.

A name that matches nothing installed is offered as a pull, because searching
for a model you do not have yet is the reason to search at all.
"""

import threading

from xnano import BaseGrid, Field
from xnano.components.text import Text
from xnano.core.content import Run
from xnano.hooks import on_keyboard, on_tick

from grant_recommender import models
from grant_recommender.panel_widgets import ACCENT, IDLE_BORDER, MUTED, RowList, message_rows


class ModelPicker(BaseGrid, direction='vertical', gap=0):
	search: Text = Field(
		default_factory=lambda: Text(content='', input=False),
		height=3,
		border='rounded',
		border_color=ACCENT,
		title=' model · type to filter · enter to use · esc to close ',
		padding=(0, 1),
		group='model_search',
	)
	listing: RowList = Field(
		default_factory=RowList,
		height='1fr',
		border='rounded',
		border_color=IDLE_BORDER,
		title=' installed models ',
		padding=(0, 1),
		scroll=True,
		group='model_list',
	)
	status: Text = Field(
		default_factory=lambda: Text(content=''),
		height=3,
		border='rounded',
		border_color=IDLE_BORDER,
		title=' status ',
		padding=(0, 1),
	)

	active: bool = Field(default=False, state=True)
	selected: int = Field(default=0, state=True)
	busy: bool = Field(default=False, state=True)

	def __post_init__(self) -> None:
		self._lock = threading.Lock()
		self._models: list[dict] = []
		self._status = ''
		self._dirty = False
		self._current = ''
		self.repaint()

	# -- data ----------------------------------------------------------------

	def query(self) -> str:
		return str(self.search.content or '').strip()

	def visible(self) -> list[dict]:
		needle = self.query().lower()
		found = [m for m in self._models if needle in m['name'].lower()]
		# A name that matches nothing installed is still actionable: offer it.
		if needle and not found:
			found = [{'name': self.query(), 'size': 0, 'installed': False}]
		return found

	def open(self) -> None:
		"""Called when the picker is shown: reload and focus the search."""
		self.active = True
		self.selected = 0
		self.search = Text(content='', input=True, placeholder='filter or type a model to pull')
		self._set_status('reading the model list…')
		threading.Thread(target=self._load, daemon=True).start()

	def close(self) -> None:
		self.active = False
		# input=False again, or the box keeps swallowing every keystroke.
		self.search = Text(content='', input=False)

	def _load(self) -> None:
		try:
			found = models.installed()
			current = models.active()
		except Exception as exc:  # noqa: BLE001 -- shown in the status line
			self._set_status(f'could not reach Ollama: {exc}')
			return
		with self._lock:
			self._models = found
			self._current = current
			self._status = f'{len(found)} installed · in use: {current}'
			self._dirty = True

	def _set_status(self, text: str) -> None:
		with self._lock:
			self._status = text
			self._dirty = True

	# -- painting ------------------------------------------------------------

	def repaint(self) -> None:
		rows = []
		entries = self.visible()
		for index, model in enumerate(entries):
			chosen = index == self.selected
			marker = '▌ ' if chosen else '  '
			if not model['installed']:
				rows.append(
					(
						Run(
							text=f'{marker}{model["name"][:44]:<46}',
							foreground=ACCENT if chosen else None,
						),
						Run(text='not downloaded · enter to pull', foreground='yellow'),
					)
				)
				continue
			in_use = model['name'] == self._current
			# One column layout either way, so a model whose size Ollama does not
			# report does not shunt the rest of the row sideways.
			detail = f'{model.get("parameters") or "":>7}  {models.human_size(model["size"]):>8}'
			rows.append(
				(
					Run(
						text=f'{marker}{model["name"][:44]:<46}',
						foreground=ACCENT if chosen else None,
						modifiers=('bold',) if chosen else (),
					),
					Run(text=f'{detail:<18}', foreground=MUTED),
					Run(
						text='in use' if in_use else 'downloaded',
						foreground='green' if in_use else MUTED,
					),
				)
			)
		self.listing = RowList(rows=tuple(rows) if rows else message_rows('No models installed.'))
		self.status = Text(content=self._status, foreground=MUTED)

	# -- events --------------------------------------------------------------

	@on_tick(200)
	def refresh_view(self) -> None:
		if not self.active:
			return
		# Typing changes the filter without going through _dirty, so the picker
		# repaints on every tick while it is open. It is a short list.
		with self._lock:
			self._dirty = False
		self.repaint()

	@on_keyboard('down')
	def select_next(self) -> None:
		if self.active:
			self.selected = min(self.selected + 1, max(0, len(self.visible()) - 1))

	@on_keyboard('up')
	def select_previous(self) -> None:
		if self.active:
			self.selected = max(self.selected - 1, 0)

	@on_keyboard('enter')
	def choose(self) -> None:
		if not self.active or self.busy:
			return
		entries = self.visible()
		if not entries:
			return
		model = entries[min(self.selected, len(entries) - 1)]
		if model['installed']:
			try:
				models.select(model['name'])
			except Exception as exc:  # noqa: BLE001 -- shown in the status line
				self._set_status(f'could not switch: {exc}')
				return
			self._current = model['name']
			self._set_status(f'now using {model["name"]}')
			return
		self.busy = True
		self._set_status(f'pulling {model["name"]}… this downloads gigabytes')
		threading.Thread(target=self._pull, args=(model['name'],), daemon=True).start()

	def _pull(self, name: str) -> None:
		try:
			models.pull(name, on_progress=lambda message: self._set_status(f'{name}: {message}'))
		except Exception as exc:  # noqa: BLE001 -- shown in the status line
			self._set_status(f'pull failed: {exc}')
			return
		finally:
			self.busy = False
		# Reload first: _load writes its own status line, so telling the reader
		# what to do next has to come after it.
		self._load()
		self._set_status(f'{name} downloaded · enter again to use it')
