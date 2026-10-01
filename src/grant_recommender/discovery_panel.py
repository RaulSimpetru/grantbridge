"""Discovery screen: scrape the official funding sources, then rank what came
back against the confirmed portfolio.

This is the terminal port of the web Opportunities panel plus the action that
fills it. Press `s` and the panel refreshes the DFG, BMFTR and added feeds,
imports up to five announcements it has not seen (the rest on the next press),
and reports each step as it happens. The
watch list is then rescored and the dossier shows why a call ranks where it
does.

Same rules as everywhere else in the product: eligibility is the deterministic
gate, kept visibly separate from the similarity score, and an imported notice
carries its source URL and its own warning that it is unverified.
"""

import json
import re
import textwrap
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from xnano import BaseGrid, Field
from xnano.components.text import Text
from xnano.core.content import Run
from xnano.hooks import on_keyboard, on_resize, on_tick

from grant_recommender import (
	briefing,
	catalog,
	concepts,
	decisions,
	documents,
	eligibility,
	german_funding,
	lifecycle,
	proposals,
	refinement,
	reviews,
	watch,
)
from grant_recommender.panel_widgets import (
	ACCENT,
	ELIGIBILITY_COLOR,
	IDLE_BORDER,
	MUTED,
	VERDICT_COLOR,
	RowList,
	message_rows,
	spinner,
)
from grant_recommender.portfolio import load_portfolio
from grant_recommender.review_view import ReviewPanes, summary_content
from grant_recommender.scoring import content_terms, score_calls

MAX_IMPORTS = 5
MAX_DOWNLOADS = 8
JOBS_DIR = Path(__file__).resolve().parents[2] / 'data' / 'private' / 'concept_jobs'
LOG_LINES = 200
# Roughly one call's read on a 12B model. Only paces the bar; the
# bar eases to 95% and waits rather than claiming a finish the model has not
# reported.
REFINE_BUDGET_S = 30.0
# Roughly the five-stage run on a 12B model.
GENERATE_BUDGET_S = 100.0
REFINE_TAIL_LINES = 6
LABEL_WIDTH = 13
LEFT_WIDTH = 46
# The left column's declared width, which _dossier_width has to agree with.
LEFT_COLUMN = 52
BAR_CELLS = 10
DEFAULT_WIDTH = 150
DEFAULT_ROWS = 46
# Three bordered reviewer panes side by side stop being readable below this,
# so under it the consolidated verdict takes the space instead.
WIDE_ENOUGH_FOR_PANES = 150
# The keys that work whatever is selected. Lives here, not in the shell, so
# hint() can measure what room is left for the keys that do not.
GLOBAL_KEYS = '/ filter   s scrape   a add   m model   q quit'
# The left column's fixed slots, named so the scroll maths and the Field
# declarations cannot drift apart.
SOURCES_HEIGHT = 8
ADD_SOURCE_HEIGHT = 3
# The shell's own rows above and below the screen: the tab strip and the footer.
SHELL_CHROME = 2
# What the shell puts around the hint: two leading spaces and a separator.
FOOTER_CHROME = len(f'     ·   {GLOBAL_KEYS}')


def first_that_fits(options: tuple[str, ...], budget: int) -> str:
	"""The longest wording that fits. The widths at which each variant stops
	fitting are not guessable: they were measured wrong twice."""
	return next((text for text in options if len(text) <= budget), options[-1])


def key_list(*keys: str, sep: str = '   ') -> str:
	"""Footer keys, leaving out the ones this row has no use for."""
	return sep.join(key for key in keys if key)


EXCERPT_CHARS = 1600
DECISION_COLOR = {
	'pursued': 'green',
	'chosen': 'green',
	'parked': 'yellow',
	'dropped': MUTED,
	'not reviewed': MUTED,
}
# A draft is the revised concept, not the first pass: the revised set is what
# `job['concepts']` holds and therefore what the tree shows.
DRAFT_VARIANT = 'revised'


def _wrapped(label: str, value: str, width: int, color: str, indent: int) -> list[tuple[Run, ...]]:
	"""A labelled value wrapped to `width`, continuation lines indented to the
	value column so the block still reads as a table."""
	lines = textwrap.wrap(value, max(10, width)) or ['']
	out = [
		(
			Run(text=f'{label:<{indent}}' if indent else '', foreground=MUTED),
			Run(text=lines[0], foreground=color),
		)
	]
	out += [(Run(text=' ' * indent), Run(text=line, foreground=color)) for line in lines[1:]]
	return out


def match_strength(overlap: float) -> tuple[str, str]:
	"""Plain words for the overlap score. The number alone means nothing to a
	reader, and bm25 means less than nothing."""
	if overlap <= 0:
		return 'no overlap', MUTED
	if overlap < 0.15:
		return 'weak', MUTED
	if overlap < 0.30:
		return 'partial', 'yellow'
	return 'strong', 'green'


def display_terms(call: dict, portfolio: list[dict], limit: int = 6) -> list[str]:
	"""A sample of the portfolio words found in this call, longest first and
	without the words that match everything. Not the full matched set, so no
	count is claimed alongside it."""
	return content_terms(call, portfolio)[:limit]


def short_date(value: str) -> str:
	return str(value)[:10]


# German notices print their deadline in the body rather than in a field, so a
# dossier that only reads `deadlines` says "none recorded" while the text below
# it gives a date. This surfaces that date, explicitly marked as unextracted,
# and never writes it into the call.
DEADLINE_HINT = re.compile(
	r'(?:Abgabetermin|Einreichungsfrist|Frist|Stichtag|deadline)\D{0,20}'
	r'(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})',
	re.IGNORECASE,
)


def deadline_in_text(call: dict) -> str | None:
	match = DEADLINE_HINT.search(call.get('scope_text') or '')
	return match.group(1) if match else None


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
	"""Trim the breadcrumb trail an imported page arrives with.

	Page text starts with navigation crumbs and a repeated title before the
	actual announcement, which pushed the real content off the screen.
	"""
	lines, seen = [], set()
	for line in text.splitlines():
		stripped = line.strip()
		if not stripped or stripped in seen:
			continue
		# Leading short lines are the crumb trail; once real prose starts, keep
		# everything so nothing substantive is hidden.
		if not lines and len(stripped) < 30:
			continue
		seen.add(stripped)
		lines.append(stripped)
	return '\n'.join(lines)[:limit]


EU_SOURCE = 'eu'
EU_LABEL = 'EU Funding Portal'


def source_key(call: dict) -> str:
	"""Which source a call came from. German notices carry a provider; the
	curated EU topic codes do not, and they are all one connector."""
	return call.get('provider') or EU_SOURCE


def source_label(provider: str) -> str:
	if provider == EU_SOURCE:
		return EU_LABEL
	source = german_funding.all_sources().get(provider)
	return source['name'] if source else provider


def source_order() -> list[str]:
	"""Stable group order: the EU connector, then sources as registered, so a
	newly added feed appends instead of reshuffling the list."""
	return [EU_SOURCE, *german_funding.all_sources()]


def _identity(entry: dict | None) -> tuple[str, str] | None:
	"""What a tree row is, independent of where it currently sits."""
	if not entry:
		return None
	item = entry['item']
	return (
		entry['row']['call']['topic_code'],
		lifecycle.concept_key(item['job_id'], item['concept']['concept_id']) if item else '',
	)


def state_color(state: str) -> str:
	"""Colour for a concept's one-word state. A panel verdict keeps the verdict
	colours so a reject reads the same here as it does in its own pane."""
	if state.startswith('panel: '):
		return VERDICT_COLOR.get(state[len('panel: ') :], MUTED)
	return DECISION_COLOR.get(state, MUTED)


def load_opportunities() -> list[dict]:
	"""The watch list, scored and gated. Never raises: an empty screen with a
	reason beats a traceback over a dead network."""
	try:
		calls = catalog.watched_calls()
	except Exception:  # noqa: BLE001 -- surfaced as an empty list in the panel
		return []
	portfolio = load_portfolio()
	fit = score_calls(calls, portfolio)
	rows = []
	for call in calls:
		assessment = eligibility.assess(call, portfolio)
		rows.append(
			{
				'call': call,
				'fit': fit.get(call['topic_code']) or {},
				'eligibility': assessment,
			}
		)
	# Grouped by source, best fit first inside each group. A source that is not
	# in the declared order (a feed removed since import) sorts to the end
	# rather than raising.
	order = source_order()

	def position(row):
		key = source_key(row['call'])
		rank = order.index(key) if key in order else len(order)
		return (rank, row['fit'].get('rank', 10**6))

	return sorted(rows, key=position)


class DiscoveryLeft(BaseGrid, direction='vertical', gap=0):
	sources: RowList = Field(
		default_factory=RowList,
		height=SOURCES_HEIGHT,
		border='rounded',
		border_color=IDLE_BORDER,
		title=' sources · a to add a feed ',
		padding=(0, 1),
		scroll=True,
		group='sources',
	)
	# A Text with input toggled, not an Input: an Input field swallows every
	# character key for the whole grid, visible or not, so `s` and `a` would
	# stop working the moment one existed.
	add_source: Text = Field(
		default_factory=lambda: Text(content='', input=False),
		height=ADD_SOURCE_HEIGHT,
		visible=False,
		border='rounded',
		border_color=ACCENT,
		title=' add an RSS or Atom feed ',
		padding=(0, 1),
		group='add_source',
	)
	filter_box: Text = Field(
		default_factory=lambda: Text(content='', input=False),
		height=ADD_SOURCE_HEIGHT,
		visible=False,
		border='rounded',
		border_color=ACCENT,
		title=' filter the watch list ',
		padding=(0, 1),
		group='filter_box',
	)
	opportunities: RowList = Field(
		default_factory=RowList,
		height='1fr',
		border='rounded',
		border_color=IDLE_BORDER,
		title=' watch list · fit rank · eligibility ',
		padding=(0, 1),
		scroll=True,
		group='opportunities',
	)


class ConceptView(BaseGrid, direction='vertical', gap=0):
	"""One concept: what it proposes, what the panel said, what was decided.

	It shows a single concept rather than a list because the list now lives in
	the tree on the left, nested under the call it came from.
	"""

	detail: RowList = Field(
		default_factory=RowList,
		height='1fr',
		border='rounded',
		border_color=IDLE_BORDER,
		title=' concept · r review · p pursue · k park · x drop · u undo · w write-up ',
		padding=(0, 1),
		scroll=True,
		group='concept_detail',
	)
	panes: ReviewPanes = Field(default_factory=ReviewPanes, height=14)
	summary: Text = Field(
		default_factory=lambda: Text(content=''),
		height=10,
		border='rounded',
		border_color=IDLE_BORDER,
		title=' consolidated ',
		padding=(0, 1),
		scroll=True,
		group='review_summary',
	)


class DiscoveryRight(BaseGrid, direction='vertical', gap=0):
	log: Text = Field(
		default_factory=lambda: Text(content=''),
		height=9,
		border='rounded',
		border_color=IDLE_BORDER,
		title=' scrape ',
		padding=(0, 1),
		scroll=True,
		group='log',
	)
	dossier: RowList = Field(
		default_factory=RowList,
		height='1fr',
		border='rounded',
		border_color=IDLE_BORDER,
		title=' dossier ',
		padding=(0, 1),
		scroll=True,
		group='dossier',
	)
	concept_view: ConceptView = Field(default_factory=ConceptView, height='1fr', visible=False)
	calltext: Text = Field(
		default_factory=lambda: Text(content=''),
		height=12,
		border='rounded',
		border_color=IDLE_BORDER,
		title=' call text ',
		padding=(0, 1),
		scroll=True,
		group='calltext',
	)


class Discovery(BaseGrid, direction='horizontal', gap=1):
	"""Sources and the ranked watch list on the left, scrape log and dossier on
	the right."""

	left: DiscoveryLeft = Field(default_factory=DiscoveryLeft, width=LEFT_COLUMN)
	right: DiscoveryRight = Field(default_factory=DiscoveryRight, width='1fr')

	# Hidden grids still receive key events, so every screen gates its own
	# bindings on whether it is the one on show.
	active: bool = Field(default=True, state=True)
	opportunities: list = Field(default_factory=list, state=True)
	selected: int = Field(default=0, state=True)
	scraping: bool = Field(default=False, state=True)
	adding: bool = Field(default=False, state=True)
	filtering: bool = Field(default=False, state=True)
	refining: bool = Field(default=False, state=True)
	pulling: bool = Field(default=False, state=True)
	reviewing: bool = Field(default=False, state=True)
	generating: bool = Field(default=False, state=True)

	def __post_init__(self) -> None:
		# Worker-thread writes land here; the tick hook is what paints them.
		self._lock = threading.Lock()
		self._log: list[str] = []
		self._dirty = True
		# Kept so the dossier can name which portfolio words matched, using the
		# same claims the score was computed from.
		self._width = DEFAULT_WIDTH
		self._rows = DEFAULT_ROWS
		self._portfolio = load_portfolio()
		# topic_code -> stored refinement, filled lazily and cached on disk.
		self._refined: dict[str, dict] = {}
		self._refine_live = ''
		self._refine_started = 0.0
		self._review_records: dict[str, dict] = {}
		self._review_live: dict[str, str] = {}
		self._round: dict | None = None
		# Which concept the review state above belongs to. Without it a round
		# started on one concept was painted under whichever one was selected
		# when it finished.
		self._review_key = ''
		# What is running, for which call, and since when. One place, so the
		# header badge, the watch-list marker and the progress bar agree.
		self._activity_started = 0.0
		self._activity_topic = ''
		self._gen_live = ''
		self._gen_stage = ''
		# Which painted row the cursor is on, and the first row of the window
		# on show. Not the same as `selected`: the list paints a header row per
		# source group, so entry index and painted row drift apart.
		self._cursor_row = 0
		self._top_row = 0
		# What the watch list is narrowed to, if anything.
		self._filter = ''
		# Rows the scrape pane has, kept so _paint_log can show the newest ones.
		self._log_height = 9
		# The tree: which calls are open, and the flat row list `selected`
		# indexes. A call and one of its concepts are the same kind of thing to
		# move through, which is what removes the old dossier/concepts toggle.
		self._expanded: set[str] = set()
		self._entries: list[dict] = []
		self._stages: dict = {'concepts': {}, 'reviewed': {}, 'decided': {}, 'read': set()}
		self.opportunities = load_opportunities()
		self._refresh_stages()
		self.repaint()

	# -- the tree ------------------------------------------------------------

	def _refresh_stages(self) -> None:
		"""Re-read what has already happened to every call. Four stores get
		read, so this runs when the list changes or a job finishes, never on the
		paint tick."""
		self._stages = lifecycle.snapshot([row['call'] for row in self.opportunities])
		self._rebuild_tree()

	def _concepts_of(self, call: dict) -> list[dict]:
		return self._stages['concepts'].get(call['topic_code']) or []

	def _shown(self, call: dict) -> bool:
		"""Whether the filter lets this call through. Matches the title or the
		topic code, because a researcher remembers one or the other."""
		if not self._filter:
			return True
		needle = self._filter.lower()
		return (
			needle in str(call.get('title') or '').lower() or needle in call['topic_code'].lower()
		)

	def _rebuild_tree(self) -> None:
		"""Rebuild the visible rows, keeping the cursor on the row it was on.

		Worker threads rebuild the tree. A generate run that finishes inserts
		its concepts under an already-open call, and with the cursor restored
		by index the highlight silently moved to a different concept -- so the
		next p, k or x decided about one the researcher had never opened, into
		an append-only log with no undo.
		"""
		keep = _identity(self._entry())
		entries: list[dict] = []
		for row in self.opportunities:
			if not self._shown(row['call']):
				continue
			entries.append({'kind': 'call', 'row': row, 'item': None})
			if row['call']['topic_code'] in self._expanded:
				entries += [
					{'kind': 'concept', 'row': row, 'item': item}
					for item in self._concepts_of(row['call'])
				]
		self._entries = entries
		clamped = max(0, min(self.selected, len(entries) - 1))
		self.selected = (
			next((i for i, e in enumerate(entries) if _identity(e) == keep), clamped)
			if keep
			else clamped
		)

	def _entry(self) -> dict | None:
		if not self._entries:
			return None
		return self._entries[min(self.selected, len(self._entries) - 1)]

	def _selected_concept(self) -> dict | None:
		entry = self._entry()
		return entry['item'] if entry else None

	# -- painting ------------------------------------------------------------

	def _paint_sources(self) -> None:
		rows = []
		for provider in german_funding.all_sources():
			status = german_funding.source_status(provider)
			# Stored in UTC, shown on the researcher's clock like the log beside it.
			stamp = status.get('fetched_at')
			try:
				fetched = (
					f'{datetime.fromisoformat(stamp).astimezone():%H:%M}' if stamp else 'never'
				)
			except ValueError:
				fetched = 'never'
			count = len(status.get('items') or [])
			error = status.get('error')
			# One Text per row, not a Text whose content is a list of Texts:
			# that nesting renders empty.
			detail = error[:24] if error else f'{count:>3} notices  {fetched}'
			rows.append(
				(
					Run(text=f'{status["name"][:16]:<18}', modifiers=('bold',)),
					Run(text=detail, foreground='red' if error else MUTED),
				)
			)
		self.left.sources = RowList(rows=tuple(rows))

	def _call_row(self, entry: dict, chosen: bool) -> tuple[Run, ...]:
		call, fit, assessment = (
			entry['row']['call'],
			entry['row']['fit'],
			entry['row']['eligibility'],
		)
		code = call['topic_code']
		state = assessment['state']
		rank = fit.get('rank')
		marker = '▌' if chosen else ' '
		if code == self._activity_topic:
			# The call an agent is working on right now.
			marker = spinner(time.monotonic() - self._activity_started)
		count = len(self._concepts_of(call))
		disclosure = ('▾' if code in self._expanded else '▸') if count else ' '
		title = str(call.get('title') or code)[:26]
		# TextBlock(lines=...) is the only way to get one styled row per entry:
		# a Text whose content is a list of Texts concatenates them inline.
		runs = [
			Run(
				text=f'{marker} {disclosure} {title:<26} ',
				foreground=ACCENT if chosen else None,
				modifiers=('bold',) if chosen else (),
			)
		]
		# Four dots for read, concepts, reviewed, decided: the list is a work
		# queue, so a row has to say what has already been done to it.
		runs += [
			Run(text='●' if done else '○', foreground=ACCENT if done else MUTED)
			for done in lifecycle.call_progress(call, self._stages)
		]
		runs.append(
			Run(
				text=f' {"#" + str(rank) if rank else "-":>4} {state[:3]}',
				foreground=ELIGIBILITY_COLOR.get(state, MUTED),
			)
		)
		return tuple(runs)

	def _concept_row(self, entry: dict, chosen: bool) -> tuple[Run, ...]:
		item = entry['item']
		state = lifecycle.concept_state(item, self._stages)
		title = str(item['concept'].get('title') or '')[:22]
		# A run seeded from the private application archive is marked
		# wherever it appears, so it is never mistaken for one built from the
		# researcher's own portfolio.
		if item.get('archive'):
			title = f'‡ {title}'[:22]
		return (
			Run(text='▌' if chosen else ' ', foreground=ACCENT),
			Run(text='   └ ', foreground=MUTED),
			Run(
				text=f'{title:<22} ',
				foreground=ACCENT if chosen else None,
				modifiers=('bold',) if chosen else (),
			),
			Run(text=state[:14], foreground=state_color(state)),
		)

	@property
	def typing(self) -> bool:
		"""An input field suppresses hooks on its own grid only, so every binding
		on this one has to stand down while either box is capturing keys."""
		return self.adding or self.filtering

	def _list_capacity(self) -> int:
		"""Painted rows the watch list can show, derived from the slots around
		it. The field declares scroll=True, but assigning it a fresh RowList
		each repaint resets the offset, so the window is chosen here instead.
		"""
		used = SHELL_CHROME + SOURCES_HEIGHT + (ADD_SOURCE_HEIGHT if self.typing else 0)
		return max(3, self._rows - used - 2)  # less the rounded border, top and bottom

	def _window(self, rows: list) -> list:
		"""The slice to paint, moved only when the cursor would leave it.

		Without this the list simply ran past the bottom of its pane: with a
		call open the cursor could sit well below the last visible row, and
		p, k or x then decided about a concept nobody could see.
		"""
		capacity = self._list_capacity()
		if len(rows) <= capacity:
			self._top_row = 0
			return rows
		top = self._top_row
		if self._cursor_row < top:
			top = self._cursor_row
		elif self._cursor_row >= top + capacity:
			top = self._cursor_row - capacity + 1
		self._top_row = max(0, min(top, len(rows) - capacity))
		return rows[self._top_row : self._top_row + capacity]

	def _paint_opportunities(self) -> None:
		rows = []
		current_source = None
		for index, entry in enumerate(self._entries[:150]):
			chosen = index == self.selected
			if chosen:
				self._cursor_row = len(rows)
			if entry['kind'] == 'concept':
				rows.append(self._concept_row(entry, chosen))
				continue
			# A group header when the source changes. Headers are decoration:
			# `selected` indexes self._entries, not painted rows.
			key = source_key(entry['row']['call'])
			if key != current_source:
				current_source = key
				count = sum(1 for r in self.opportunities if source_key(r['call']) == key)
				rows.append(
					(
						Run(
							text=f'{source_label(key)[:30]} ',
							foreground=ACCENT,
							modifiers=('bold',),
						),
						Run(text=f'({count})', foreground=MUTED),
					)
				)
			rows.append(self._call_row(entry, chosen))
		if not rows:
			self.left.opportunities = RowList(
				rows=message_rows(
					f'No call matches “{self._filter}”. Press / to change it.'
					if self._filter
					else 'Nothing yet. Press s to scrape.'
				)
			)
			return
		self.left.opportunities = RowList(rows=tuple(self._window(rows)))

	def _paint_log(self) -> None:
		if not self._log:
			self.right.log = Text(
				content='Press s to refresh the official sources and import what is new.',
				foreground=MUTED,
			)
			return
		# The newest lines, not the oldest of the last two hundred: the pane
		# does not scroll itself, and on a short window it is three rows deep.
		# Counted in wrapped rows, not lines: a line wider than the pane takes
		# two, and counted as one it pushed the newest line under the border.
		# The pane is as wide as the dossier below it.
		visible = max(1, min(LOG_LINES, self._log_height - 2))
		rows = [
			part
			for line in self._log[-visible:]
			for part in textwrap.wrap(line, self._dossier_width()) or ['']
		]
		self.right.log = Text(content='\n'.join(rows[-visible:]), foreground=MUTED)

	def _dossier_width(self) -> int:
		"""Right pane content width. Tracked from resize events because rows are
		wrapped by hand: xnano lays a Text content list out as blocks once any
		segment contains a newline, so exact rows are the only way to align
		columns.

		52 for the left column, 1 for the gap, 4 for this pane's border and
		padding. The old floor of 40 was wider than the pane itself below about
		100 columns, so every hand-wrapped row was re-wrapped by the renderer
		and the label columns came apart.
		"""
		return max(20, self._width - LEFT_COLUMN - 5)

	def _reading_rows(self) -> list[tuple[Run, ...]]:
		"""Progress while the model reads. Shown whether or not this call already
		has fields, so a re-read is visibly a re-read."""
		elapsed = time.monotonic() - self._refine_started
		# int, not round: round(9.5) is 10, a full bar for a read still going.
		filled = int(min(0.95, elapsed / REFINE_BUDGET_S) * BAR_CELLS)
		rows = [
			(
				Run(text=f'{"READING":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(text='█' * filled, foreground=ACCENT),
				Run(text='░' * (BAR_CELLS - filled), foreground=MUTED),
				Run(text=f'  {elapsed:.0f}s', foreground=ACCENT),
				Run(text='   the model is reading this call', foreground=MUTED),
			)
		]
		# The stream itself, so the wait shows work rather than a spinner.
		rows += [
			(Run(text=' ' * LABEL_WIDTH), Run(text=line[:120], foreground=MUTED))
			for line in self._refine_live.splitlines()[-REFINE_TAIL_LINES:]
		]
		return rows

	def _generating_rows(self) -> list[tuple[Run, ...]]:
		"""Progress while the model develops concepts, shown beside the call it
		is working from rather than on a screen you have to switch to."""
		elapsed = time.monotonic() - self._activity_started
		filled = int(min(0.95, elapsed / GENERATE_BUDGET_S) * BAR_CELLS)
		rows = [
			(
				Run(text=f'{"DEVELOPING":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(text='█' * filled, foreground=ACCENT),
				Run(text='░' * (BAR_CELLS - filled), foreground=MUTED),
				Run(text=f'  {elapsed:.0f}s  ', foreground=ACCENT),
				Run(text=self._gen_stage.replace('_', ' '), foreground=MUTED),
			)
		]
		rows += [
			(Run(text=' ' * LABEL_WIDTH), Run(text=line[:110], foreground=MUTED))
			for line in self._gen_live.splitlines()[-8:]
		]
		return rows

	def _paint_dossier(self) -> None:
		entry = self._entry()
		if not entry:
			self.right.dossier = RowList()
			self.right.calltext = Text(content='')
			return
		row = entry['row']
		call, fit, assessment = row['call'], row['fit'], row['eligibility']
		# Only the call an agent is actually working on shows its progress. The
		# bars and the streamed text are keyed on _activity_topic, so without
		# this they were drawn into whichever dossier happened to be open.
		working = call['topic_code'] == self._activity_topic
		width = self._dossier_width()
		body = width - LABEL_WIDTH

		rows: list[tuple[Run, ...]] = []
		title = str(call.get('title') or call['topic_code'])
		for line in textwrap.wrap(title, width) or [title]:
			rows.append((Run(text=line, modifiers=('bold',)),))
		rows.append(
			(
				Run(
					text=f'{source_label(source_key(call))}  ·  {call["topic_code"]}',
					foreground=MUTED,
				),
			)
		)
		rows.append(())

		# Fit first: it is the question the researcher is actually asking, and
		# bm25 is gone because a raw retrieval score means nothing to a reader.
		overlap = fit.get('keyword_overlap', 0.0)
		strength, color = match_strength(overlap)
		filled = round(overlap * BAR_CELLS)
		rank = fit.get('rank')
		rows.append(
			(
				Run(text=f'{"MATCH":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(text='█' * filled, foreground=color),
				Run(text='░' * (BAR_CELLS - filled), foreground=MUTED),
				Run(text=f'  {overlap:.0%} {strength}', foreground=color),
				Run(
					text=f'   {f"rank {rank} of {len(self.opportunities)}" if rank else "not ranked"}',
					foreground=MUTED,
				),
			)
		)
		terms = ', '.join(display_terms(call, self._portfolio))
		rows += _wrapped('', terms or 'no distinctive terms in common', body, MUTED, LABEL_WIDTH)
		rows.append(())

		state = assessment['state']
		rows.append(
			(
				Run(text=f'{"ELIGIBILITY":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(
					text=state.upper(),
					foreground=ELIGIBILITY_COLOR.get(state, MUTED),
					modifiers=('bold',),
				),
			)
		)
		rows += _wrapped('', assessment['reason'], body, MUTED, LABEL_WIDTH)
		rows.append(())

		if self.refining and working:
			rows.append(())
			rows += self._reading_rows()
		if self.generating and working:
			rows.append(())
			rows += self._generating_rows()

		refined = self._refined.get(call['topic_code']) or refinement.cached(call)
		if refined:
			self._refined[call['topic_code']] = refined
			# Only over values that are there: a read that kept nothing still
			# claimed quotes the call never gave.
			if refined['fields']:
				rows.append(())
				rows.append(
					(
						Run(text=f'{"FROM TEXT":<{LABEL_WIDTH}}', foreground=MUTED),
						Run(text='every value below is quoted from the call', foreground=MUTED),
					)
				)
			for name, field in refined['fields'].items():
				rows += _wrapped(name.upper(), field['value'], body, None, LABEL_WIDTH)
				rows += _wrapped('', f'„{field["quote"][:200]}"', body, MUTED, LABEL_WIDTH)
			if refined.get('synopsis'):
				rows.append(())
				rows += _wrapped('SYNOPSIS', refined['synopsis'], body, None, LABEL_WIDTH)
				rows += _wrapped(
					'',
					f'written by {refined.get("model") or "the model"} from the call text; '
					f'figures of {refinement.MIN_FIGURE_CHARS}+ characters were checked '
					'against it, shorter numbers were not',
					body,
					MUTED,
					LABEL_WIDTH,
				)
			if refined.get('invented_figures'):
				rows += _wrapped(
					'',
					'synopsis dropped, these figures are not in the call: '
					+ ', '.join(refined['invented_figures']),
					body,
					'yellow',
					LABEL_WIDTH,
				)
			if refined['dropped']:
				rows += _wrapped(
					'',
					'dropped, quote did not match the page: ' + ', '.join(refined['dropped']),
					body,
					'yellow',
					LABEL_WIDTH,
				)
		rows.append(())

		files = documents.stored(call)
		if files or (self.pulling and working):
			rows.append(())
			rows.append(
				(
					Run(text=f'{"FILES":<{LABEL_WIDTH}}', foreground=MUTED),
					Run(
						text='downloading…'
						if (self.pulling and working)
						else f'{len(files)} saved',
						foreground=ACCENT if (self.pulling and working) else MUTED,
					),
				)
			)
			for path in files[:8]:
				rows.append(
					(Run(text=' ' * LABEL_WIDTH), Run(text=path.name[:90], foreground=MUTED))
				)

		recorded = [short_date(d) for d in call.get('deadlines') or []]
		if recorded:
			rows.append((Run(text=f'{"DEADLINE":<{LABEL_WIDTH}}'), Run(text=' · '.join(recorded))))
		else:
			hint = deadline_in_text(call)
			rows.append(
				(
					Run(text=f'{"DEADLINE":<{LABEL_WIDTH}}'),
					Run(text='none recorded', foreground=MUTED),
					Run(
						text=f'   text mentions {hint} · unverified' if hint else '',
						foreground='yellow',
					),
				)
			)
		rows += _wrapped('SOURCE', call.get('source_url') or '—', body, MUTED, LABEL_WIDTH)
		if call.get('source_warning'):
			rows.append(())
			warning = str(call['source_warning']).split('. ')[0].rstrip('.')
			rows += _wrapped('', f'⚠  {warning}.', width, 'yellow', 0)
		self.right.dossier = RowList(rows=tuple(rows))
		self.right.calltext = Text(content=excerpt(call.get('scope_text') or ''))

	def _selected_call(self) -> dict | None:
		"""The call the selection sits on, whether that is the call row itself or
		one of the concepts nested under it."""
		entry = self._entry()
		return entry['row']['call'] if entry else None

	def _paint_concept(self) -> None:
		view = self.right.concept_view
		entry = self._entry()
		entry_call = entry['row']['call'] if entry else None
		item = self._selected_concept()
		if not item:
			view.detail = RowList(
				rows=message_rows(
					'Open a call with → to see its concepts, or press g to develop one.'
				)
			)
			view.panes.paint({}, {}, False)
			view.summary = summary_content(None)
			return
		concept = item['concept']
		width = self._dossier_width()
		body = width - LABEL_WIDTH
		rows: list[tuple[Run, ...]] = []
		title = str(concept.get('title') or '')
		for line in textwrap.wrap(title, width) or [title]:
			rows.append((Run(text=line, modifiers=('bold',)),))
		rows.append(
			(
				Run(
					text=f'run {item["job_id"]}  ·  {item.get("model") or "unknown model"}',
					foreground=MUTED,
				),
				Run(
					text='  ·  exploratory trial' if item['trial'] else '',
					foreground='yellow',
				),
			)
		)
		rows.append(())
		if item.get('archive'):
			rows += _wrapped(
				'ARCHIVE',
				'this run drew on the private proposal archive; its text may echo an '
				'archived application and must not be shown or shared',
				body,
				'yellow',
				LABEL_WIDTH,
			)
			rows.append(())
		state = lifecycle.concept_state(item, self._stages)
		rows.append(
			(
				Run(text=f'{"STATE":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(text=state, foreground=state_color(state), modifiers=('bold',)),
			)
		)
		rows.append(())
		# Beside the state, not below the concept: whether a write-up exists is a
		# status, and the fields below it run past the bottom of the pane.
		written = briefing.path_for(entry_call, item) if entry_call else None
		if written is not None and written.exists():
			rows += _wrapped('WRITE-UP', written.name, body, MUTED, LABEL_WIDTH)
			rows.append(())
		for label, key in lifecycle.CONCEPT_FIELDS:
			value = concept.get(key)
			if not value:
				continue
			if isinstance(value, list):
				rows += _wrapped(label, f'· {value[0]}', body, None, LABEL_WIDTH)
				for extra in value[1:]:
					rows += _wrapped('', f'· {extra}', body, None, LABEL_WIDTH)
			else:
				rows += _wrapped(label, str(value), body, None, LABEL_WIDTH)
			rows.append(())
		rows.append(
			(
				Run(text=f'{"EVIDENCE":<{LABEL_WIDTH}}', foreground=MUTED),
				Run(
					text=f'{len(concept.get("call_evidence") or [])} from the call  ·  '
					f'{len(concept.get("researcher_evidence") or [])} from your portfolio',
					foreground=MUTED,
				),
			)
		)
		view.detail = RowList(rows=tuple(rows))
		# Review state belongs to the concept it was asked about. A round that
		# finished while the researcher had moved on was otherwise painted
		# under whichever concept happened to be selected.
		mine = lifecycle.concept_key(item['job_id'], concept['concept_id']) == self._review_key
		view.panes.paint(
			self._review_records if mine else {},
			self._review_live if mine else {},
			self.reviewing and mine,
		)
		view.summary = summary_content(
			self._round if mine else None,
			running=self.reviewing and mine,
			live=self._review_live if mine else {},
			records=self._review_records if mine else {},
		)

	def _sync_panes(self) -> None:
		"""Which half of the right column is on show. Driven by what the
		selection is, so there is no mode to remember or toggle."""
		showing = self._selected_concept() is not None
		self.right.grid_update_field('concept_view', visible=showing)
		for field in ('dossier', 'calltext'):
			self.right.grid_update_field(field, visible=not showing)

	def _load_review(self) -> None:
		"""Show the panel's last verdict on this concept if it already has one,
		rather than an empty pane for work that was already done."""
		if self.reviewing:
			return
		self._review_records, self._review_live, self._round = {}, {}, None
		self._review_key = ''
		item = self._selected_concept()
		if not item:
			return
		key = lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
		stored = self._stages['reviewed'].get(key)
		if not stored:
			return
		try:
			round_ = reviews.load(stored['id'])
		except (OSError, ValueError, KeyError):
			return
		# round.json stores each reviewer in exactly the record shape the panes
		# expect, so it can be handed over as it is.
		self._round = round_
		self._review_records = round_.get('reviews') or {}
		self._review_key = key

	def _after_move(self) -> None:
		self._load_review()
		self._sync_panes()
		self._paint_opportunities()
		self._paint_dossier()
		self._paint_concept()

	def repaint(self) -> None:
		self._paint_sources()
		self._paint_opportunities()
		self._paint_log()
		self._paint_dossier()
		self._paint_concept()

	# -- events --------------------------------------------------------------

	def grid_post_init(self, ctx) -> None:
		# First real width, once there is a terminal to ask.
		self._adopt_width(ctx)

	@on_resize
	def track_width(self, ctx) -> None:
		self._adopt_width(ctx)

	def _adopt_width(self, ctx) -> None:
		"""Viewport size: width for the hand-wrapped dossier rows, height to
		decide what still fits.

		From terminal.size, which is a (cols, rows) tuple. Not ctx.render_size:
		that is a breakpoint tier name such as 'medium', and indexing it yielded
		'm', which crashed the app on the first resize.
		"""
		try:
			width, rows = int(ctx.terminal.size[0]), int(ctx.terminal.size[1])
		except (AttributeError, IndexError, TypeError, ValueError):
			return
		if width <= 0 or rows <= 0 or (width == self._width and rows == self._rows):
			return
		self._width, self._rows = width, rows
		self.apply_layout()
		# Everything that depends on the size, not just the dossier: the concept
		# detail and the log wrap to the same width, and the watch list's visible
		# window is derived from the row count, so a shorter window left the
		# cursor outside it until the next keypress.
		self._paint_log()
		self._paint_opportunities()
		self._paint_dossier()
		self._paint_concept()

	def apply_layout(self) -> None:
		"""Fit the right column to the terminal.

		Heights that suit a 46-row window overflow a 30-row one and push the
		footer off the screen entirely, and three reviewer panes side by side
		need about 150 columns before their text is readable. Below that the
		consolidated verdict takes the whole space: it carries the outcome, the
		tally, every unsupported claim and who raised it.
		"""
		tall, medium = self._rows >= 42, self._rows >= 34
		self._log_height = 9 if tall else 5 if medium else 3
		self.right.grid_update_field('log', height=self._log_height)
		self.right.grid_update_field('calltext', height=12 if tall else 8 if medium else 5)
		# Height as well as width: at 170x30 three bordered boxes 14 rows deep
		# left the concept itself three rows, which is not a view of anything.
		show_panes = self._width >= WIDE_ENOUGH_FOR_PANES and medium
		view = self.right.concept_view
		view.grid_update_field('panes', visible=show_panes, height=14 if tall else 10)
		view.grid_update_field('summary', height=(10 if tall else 6) if show_panes else '1fr')

	@on_tick(200)
	def refresh_view(self) -> None:
		"""The only place worker output reaches a rendered field."""
		if self.filtering:
			typed = str(self.left.filter_box.content or '').strip()
			if typed != self._filter:
				self._filter = typed
				self._rebuild_tree()
				with self._lock:
					self._dirty = True
		with self._lock:
			if self._dirty:
				self._dirty = False
				self.repaint()
			elif self.busy():
				# The clock and the streamed tail move on their own; repaint just
				# the dossier rather than re-reading every source from disk.
				self._paint_dossier()

	def _begin(self, topic_code: str) -> None:
		with self._lock:
			self._activity_started = time.monotonic()
			self._activity_topic = topic_code
			self._dirty = True

	def _end(self) -> None:
		with self._lock:
			self._activity_topic = ''
			self._gen_live = ''
			self._gen_stage = ''
			self._dirty = True

	def activity(self) -> tuple[str, float]:
		"""What is in flight and for how long, for anything that wants to say so."""
		label = self.busy()
		return (label, time.monotonic() - self._activity_started if label else 0.0)

	def busy(self) -> str:
		"""What long model job is in flight, if any.

		One 12B model serves the scrape, the extractor, concept generation and
		the review panel, and the watch loop is a fifth caller. Running two at
		once on one machine thrashes, so they take turns and the reason is shown.
		"""
		for flag, label in (
			(self.scraping, 'a scrape'),
			(self.refining, 'a call being read'),
			(self.pulling, 'a download'),
			(self.reviewing, 'a review round'),
			(self.generating, 'concept generation'),
		):
			if flag:
				return label
		return ''

	@on_keyboard('g')
	def generate_concepts(self) -> None:
		"""Develop concepts for the selected call."""
		if not self.active or self.typing:
			return
		blocked = self.busy()
		if blocked:
			self._log_event(f'busy with {blocked}; try again when it finishes')
			return
		call = self._selected_call()
		if not call:
			return
		portfolio = self._portfolio
		state = eligibility.assess(call, portfolio)['state']
		if state == 'ineligible':
			# The same deterministic gate the watch loop applies; a passed
			# deadline is not something to spend model time on.
			self._log_event(f'{call["topic_code"]} is ineligible: not generating')
			return
		trial = state != 'eligible'
		self.generating = True
		self._begin(call['topic_code'])
		self._log_event(
			f'generating concepts for {call["topic_code"]}'
			+ (' (exploratory trial, eligibility unconfirmed)' if trial else '')
		)
		self._expanded.add(call['topic_code'])
		self._rebuild_tree()
		threading.Thread(
			target=self._generate_worker, args=(call, portfolio, trial), daemon=True
		).start()

	def _generate_worker(self, call: dict, portfolio: list[dict], trial: bool) -> None:
		if not refinement.cached(call):
			# Read the call first so the dossier is filled in by the time
			# concepts appear rather than staying blank behind them. Both read
			# the same scope_text, so this adds nothing to the concept packet;
			# it only stops the researcher developing a call nobody has read.
			self.refining = True
			with self._lock:
				self._refine_live = ''
				self._refine_started = time.monotonic()
				self._dirty = True
			self._log_event(f'reading {call["topic_code"]} before developing it…')
			try:
				read = refinement.refine(call, on_text=self._on_refine_text)
			except Exception as exc:  # noqa: BLE001 -- generation still goes ahead
				self._log_event(f'could not read {call["topic_code"]}: {exc}')
			else:
				# Same report the explicit `e` gives, so this path is not silent.
				kept = ', '.join(read['fields']) or 'nothing verifiable'
				dropped = f' · dropped {len(read["dropped"])}' if read['dropped'] else ''
				self._log_event(f'{call["topic_code"]}: {kept}{dropped}')
			finally:
				self.refining = False
				with self._lock:
					self._refine_live = ''
					# Restart the clock: the read took about half a minute, and
					# a bar that counted it would open a third full before
					# generation had begun.
					self._activity_started = time.monotonic()
					self._dirty = True
		try:
			job = concepts.start_job(call, portfolio, trial=trial)
		except Exception as exc:  # noqa: BLE001 -- reported in the log
			self._log_event(f'could not start: {exc}')
			self.generating = False
			self._end()
			return
		while True:
			time.sleep(0.5)
			state = concepts.get_job(job['id'])
			if not state or state['status'] != 'running':
				break
			running = next((s for s in state['stages'] if s['status'] == 'running'), None)
			with self._lock:
				self._gen_stage = running['name'] if running else 'working'
				self._gen_live = (running or {}).get('live') or ''
				self._dirty = True
		self.generating = False
		self._end()
		final = concepts.get_job(job['id']) or {}
		if final.get('status') == 'succeeded':
			self._log_event(f'{job["id"]}: {len(final.get("concepts") or [])} concepts')
		else:
			self._log_event(f'{job["id"]}: {final.get("status")} · {final.get("error") or ""}')
		self._refresh_stages()
		with self._lock:
			self._dirty = True

	@on_keyboard('r')
	def review_concept(self) -> None:
		"""Send the selected concept to the review panel."""
		if not self.active or self.typing:
			return
		item = self._selected_concept()
		if not item:
			self._log_event('select a concept first: → opens the call you are on')
			return
		blocked = self.busy()
		if blocked:
			self._log_event(f'busy with {blocked}; try again when it finishes')
			return
		self.reviewing = True
		self._begin(self._selected_call()['topic_code'] if self._selected_call() else '')
		with self._lock:
			self._review_records, self._review_live, self._round = {}, {}, None
			self._review_key = lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
			self._dirty = True
		self._log_event(f'reviewing "{str(item["concept"].get("title"))[:50]}"…')
		threading.Thread(target=self._review_worker, args=(item,), daemon=True).start()

	def _review_worker(self, item: dict) -> None:
		def on_text(role: str, text: str) -> None:
			with self._lock:
				self._review_live[role] = text
				self._dirty = True

		def on_done(role: str, record: dict) -> None:
			with self._lock:
				self._review_records[role] = record
				self._dirty = True

		try:
			job = json.loads((JOBS_DIR / f'{item["job_id"]}.json').read_text())
			document = reviews.from_concept(job, item['concept']['concept_id'])
			result = reviews.run_round(document, on_text=on_text, on_done=on_done)
		except Exception as exc:  # noqa: BLE001 -- reported in the log
			self._log_event(f'review failed: {exc}')
			return
		finally:
			self.reviewing = False
			self._end()
		with self._lock:
			self._round = result
			self._dirty = True
		self._refresh_stages()
		self._log_event(f'panel says {result["consolidated"]["outcome"]}')

	@on_keyboard('down')
	def select_next(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = min(self.selected + 1, len(self._entries) - 1)
			self._after_move()

	@on_keyboard('up')
	def select_previous(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = max(self.selected - 1, 0)
			self._after_move()

	@on_keyboard('right')
	def open_branch(self) -> None:
		"""Show the selected call's concepts, or step into them."""
		entry = self._entry()
		if not self.active or self.typing or not entry or entry['kind'] != 'call':
			return
		call = entry['row']['call']
		if not self._concepts_of(call):
			self._log_event(f'{call["topic_code"]} has no concepts yet; press g to develop some')
			return
		if call['topic_code'] in self._expanded:
			self.selected += 1  # already open, so step onto the first concept
		else:
			self._expanded.add(call['topic_code'])
		self._rebuild_tree()
		self._after_move()

	@on_keyboard('left')
	def close_branch(self) -> None:
		"""Step back out to the call, then fold its concepts away."""
		entry = self._entry()
		if not self.active or self.typing or not entry:
			return
		code = entry['row']['call']['topic_code']
		if entry['kind'] == 'concept':
			# Defaulted: a worker can rebuild the tree between the read of
			# `entry` and this scan, and a bare next() then raised StopIteration
			# out of the key handler and took the app down.
			self.selected = next(
				(
					index
					for index, other in enumerate(self._entries)
					if other['kind'] == 'call' and other['row']['call']['topic_code'] == code
				),
				self.selected,
			)
		else:
			self._expanded.discard(code)
			self._rebuild_tree()
		self._after_move()

	@on_keyboard('p')
	def pursue_concept(self) -> None:
		"""Take this concept forward, which opens a working draft for it."""
		self._decide('develop', 'pursuing')

	@on_keyboard('k')
	def park_concept(self) -> None:
		self._decide('save', 'parked')

	@on_keyboard('x')
	def drop_concept(self) -> None:
		self._decide('dismiss', 'dropped')

	def _decide(self, kind: str, word: str) -> None:
		"""Record what the researcher decided about a concept, and for a pursue
		open the draft it becomes.

		The decision is the point, not the draft: every press writes one new
		event and never edits an earlier one, so the Activity timeline stays a
		complete record of what was decided.
		"""
		if not self.active or self.typing:
			return
		item = self._selected_concept()
		if not item:
			self._log_event('select a concept first: → opens the call you are on')
			return
		concept_id = item['concept']['concept_id']
		draft_id = None
		if kind == 'develop':
			# The draft copies the run's archive_context, which for a run started
			# from the archive carries private material. Nothing here shows it,
			# but a pane that renders a draft would have to.
			try:
				draft_id = proposals.create(item['job_id'], concept_id, DRAFT_VARIANT)['id']
			except Exception as exc:  # noqa: BLE001 -- reported in the log
				self._log_event(f'could not open a draft: {exc}')
				return
		decisions.record(
			kind, 'concept', lifecycle.concept_key(item['job_id'], concept_id), draft_id
		)
		title = str(item['concept'].get('title') or '')[:40]
		self._log_event(f'{word} "{title}"' + (f' · draft {draft_id}' if draft_id else ''))
		self._refresh_stages()
		self._after_move()

	def _round_for(self, item: dict) -> dict | None:
		"""The stored review round for one concept, if it has one."""
		stored = self._stages['reviewed'].get(
			lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
		)
		if not stored:
			return None
		try:
			return reviews.load(stored['id'])
		except (OSError, ValueError, KeyError):
			return None

	def _draft_for(self, item: dict) -> dict | None:
		try:
			drafts = proposals.list_drafts(item['job_id'])
		except (OSError, ValueError):
			return None
		return next(
			(
				draft
				for draft in drafts
				if draft['concept_id'] == item['concept']['concept_id']
				and draft['variant'] == DRAFT_VARIANT
			),
			None,
		)

	@on_keyboard('w')
	def write_briefing(self) -> None:
		"""Put everything known about this concept into one Markdown file.

		The call, the facts quoted out of it, the concept, the panel's
		criticisms and the draft's open questions live in five stores. This is
		the only thing that hands the researcher all of it at once, in a file
		they can open somewhere else.
		"""
		if not self.active or self.typing:
			return
		entry = self._entry()
		item = self._selected_concept()
		if not item:
			self._log_event('select a concept first: → opens the call you are on')
			return
		self._write_briefing_for(entry['row'], item)
		self._paint_concept()

	def _write_briefing_for(self, row: dict, item: dict) -> Path | None:
		"""Compose and write one concept's briefing. Shared with the b chain."""
		call = row['call']
		text = briefing.compose(
			call=call,
			item=item,
			fit=row['fit'],
			assessment=row['eligibility'],
			refined=refinement.cached(call),
			round_=self._round_for(item),
			draft=self._draft_for(item),
			files=documents.stored(call),
			source=source_label(source_key(call)),
		)
		try:
			path = briefing.write(text, call, item)
		except OSError as exc:
			self._log_event(f'could not write the briefing: {exc}')
			return None
		self._log_event(f'wrote {path} · {len(text.splitlines())} lines')
		return path

	def _select_concept(self, item: dict) -> None:
		"""Move the cursor onto a concept, so the screen follows the work."""
		key = lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
		self._rebuild_tree()
		for index, entry in enumerate(self._entries):
			other = entry['item']
			if (
				other
				and lifecycle.concept_key(other['job_id'], other['concept']['concept_id']) == key
			):
				self.selected = index
				break
		self._sync_panes()

	@on_keyboard('b')
	def brief_call(self) -> None:
		"""Take this call all the way to a write-up in one press.

		Read it, develop concepts, put the first one to the panel, write the
		briefing: the four keys that were always pressed in that order anyway.
		It stops before pursuing on purpose. The decision log is a record of
		what the researcher chose, and a machine cannot choose on their behalf.
		"""
		if not self.active or self.typing:
			return
		blocked = self.busy()
		if blocked:
			self._log_event(f'busy with {blocked}; try again when it finishes')
			return
		entry = self._entry()
		if not entry:
			return
		call = entry['row']['call']
		state = eligibility.assess(call, self._portfolio)['state']
		if state == 'ineligible':
			# The same deterministic gate g applies.
			self._log_event(f'{call["topic_code"]} is ineligible: not working it through')
			return
		self.generating = True
		self._begin(call['topic_code'])
		self._expanded.add(call['topic_code'])
		self._rebuild_tree()
		self._log_event(
			f'working {call["topic_code"]} through: read, develop, review, write-up'
			+ (' (exploratory trial, eligibility unconfirmed)' if state != 'eligible' else '')
		)
		threading.Thread(
			target=self._brief_worker,
			args=(entry['row'], self._portfolio, state != 'eligible'),
			daemon=True,
		).start()

	def _brief_worker(self, row: dict, portfolio: list[dict], trial: bool) -> None:
		call = row['call']
		self._generate_worker(call, portfolio, trial)
		items = self._concepts_of(call)
		if not items:
			self._log_event('no concepts came back; stopping before the review')
			return
		item = items[0]
		self._select_concept(item)
		self.reviewing = True
		self._begin(call['topic_code'])
		with self._lock:
			self._review_records, self._review_live, self._round = {}, {}, None
			self._review_key = lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
			self._dirty = True
		self._review_worker(item)
		self._write_briefing_for(row, item)
		with self._lock:
			self._dirty = True

	@on_keyboard('u')
	def undo_decision(self) -> None:
		"""Take back the decision standing on this concept.

		A new event, never an edit: the log stays a faithful record of what
		happened. Undoing a pursue leaves its draft on disk -- the file is
		still there, it is just no longer what the researcher is pursuing.
		"""
		if not self.active or self.typing:
			return
		item = self._selected_concept()
		if not item:
			self._log_event('select a concept first: → opens the call you are on')
			return
		state = lifecycle.concept_state(item, self._stages)
		kind = lifecycle.UNDO_FOR.get(state)
		if not kind:
			self._log_event(f'nothing to undo: this concept is {state}')
			return
		key = lifecycle.concept_key(item['job_id'], item['concept']['concept_id'])
		decisions.record(kind, 'concept', key)
		note = ' (its draft is still on disk)' if state == 'pursued' else ''
		self._log_event(f'undid {state}{note}')
		self._refresh_stages()
		self._after_move()

	def hint(self) -> str:
		"""The keys that do something to whatever is selected right now.

		A flat list of every binding does not teach the order the work happens in,
		and a footer cut off mid-word teaches less than a short one, so this picks
		the longest wording that measurably fits the window.
		"""
		# An open box takes every key, so only its own two do anything.
		if self.adding:
			return 'enter add the feed   esc cancel'
		if self.filtering:
			return 'enter keep the filter   esc clear'
		entry = self._entry()
		if not entry:
			return 's scrape   a add a source'
		budget = self._width - FOOTER_CHROME
		if entry['kind'] == 'concept':
			return first_that_fits(
				(
					(
						'r review   p pursue   k park   x drop   u undo   w write-up   '
						'← back to the call'
					),
					'r review  p pursue  k park  x drop  u undo  w write-up  ← back',
					'r review  p pursue  w write-up  u undo  ← back',
					'r review  p pursue  w write-up',
					'r review  p pursue',
				),
				budget,
			)
		call = entry['row']['call']
		count = len(self._concepts_of(call))
		# g applies the same deterministic gate the watch loop does, so offering
		# it on a call it will refuse only costs the reader a keypress.
		ineligible = entry['row']['eligibility']['state'] == 'ineligible'
		if ineligible:
			wide_develop, terse_develop = 'not eligible, so g will refuse', 'not eligible'
		elif count:
			wide_develop = terse_develop = 'g develop more'
		else:
			wide_develop, terse_develop = 'g develop concepts', 'g develop'
		# b refuses whatever g refuses, so the develop slot names the gate once.
		chain = '' if ineligible else 'b work it through'
		# EU topic files sit on the portal page, which d cannot fetch from.
		files = '' if source_key(call) == EU_SOURCE else 'd files'
		if not count:
			return first_that_fits(
				(
					key_list(chain, wide_develop, 'e read the call', files, 'enter open page'),
					key_list(chain, terse_develop, 'e read', files, sep='  '),
					key_list(chain, terse_develop, sep='  '),
					chain or terse_develop,
				),
				budget,
			)
		open_now = call['topic_code'] in self._expanded
		wide_verb = '← hide concepts' if open_now else f'→ {count} concepts'
		terse_verb = '← hide' if open_now else f'→ {count}'
		return first_that_fits(
			(
				key_list(wide_verb, chain, wide_develop, 'e read the call', files, 'enter open'),
				key_list(terse_verb, chain, terse_develop, 'e read', files, sep='  '),
				key_list(terse_verb, chain or terse_develop, sep='  '),
				chain or terse_develop,
			),
			budget,
		)

	@on_keyboard('e')
	def refine_selected(self) -> None:
		"""Read the selected call's own text for the facts it buries in prose."""
		if not self.active or self.typing or not self._entries:
			return
		blocked = self.busy()
		if blocked:
			self._log_event(f'busy with {blocked}; try again when it finishes')
			return
		call = self._selected_call()
		# Pressing the key is an explicit request, so it re-reads rather than
		# reporting a cache hit and appearing to do nothing. The cache still
		# serves the dossier when a call is merely selected.
		again = bool(refinement.cached(call))
		self.refining = True
		self._begin(call['topic_code'])
		with self._lock:
			self._refine_live = ''
			self._refine_started = time.monotonic()
			self._dirty = True
		self._log_event(f'{"re-reading" if again else "reading"} {call["topic_code"]}…')
		threading.Thread(target=self._refine_worker, args=(call, again), daemon=True).start()

	def _on_refine_text(self, text: str) -> None:
		with self._lock:
			self._refine_live = text

	def _refine_worker(self, call: dict, again: bool = False) -> None:
		try:
			result = refinement.refine(call, on_text=self._on_refine_text, use_cache=not again)
		except Exception as exc:  # noqa: BLE001 -- reported in the log
			self._log_event(f'could not read {call["topic_code"]}: {exc}')
			return
		finally:
			self.refining = False
			self._end()
			with self._lock:
				self._refine_live = ''
		kept = ', '.join(result['fields']) or 'nothing verifiable'
		note = f' · dropped {len(result["dropped"])}' if result['dropped'] else ''
		self._log_event(f'{call["topic_code"]}: {kept}{note}')
		with self._lock:
			self._refined[call['topic_code']] = result
			self._dirty = True
		self._refresh_stages()

	@on_keyboard('d')
	def pull_documents(self) -> None:
		"""Fetch the templates and guidance the call page links to."""
		if not self.active or self.typing or self.busy():
			return
		call = self._selected_call()
		if not call:
			return
		if not call.get('source_url'):
			self._log_event(f'{call["topic_code"]} has no page to read files from')
			return
		if source_key(call) == EU_SOURCE:
			# The portal is not a host the fetcher may reach, so this only ever
			# failed with an error about the provider.
			self._log_event('EU topic files are on the portal page: press enter to open it')
			return
		self.pulling = True
		self._begin(call['topic_code'])
		threading.Thread(target=self._pull_worker, args=(call,), daemon=True).start()

	def _pull_worker(self, call: dict) -> None:
		provider = call.get('provider') or 'eu'
		try:
			self._log_event(f'looking for files on {call["topic_code"]}…')
			html = german_funding.fetch(call['source_url'], provider)
			found = documents.discover(call, html)
		except Exception as exc:  # noqa: BLE001 -- reported in the log
			self._log_event(f'could not read the page: {exc}')
			self.pulling = False
			self._end()
			return
		if not found:
			self._log_event('no documents linked from that page')
			self.pulling = False
			self._end()
			with self._lock:
				self._dirty = True
			return
		saved = 0
		for document in found[:MAX_DOWNLOADS]:
			try:
				documents.download(call, document)
				saved += 1
				self._log_event(f'saved {document["filename"]} ({document["kind"]})')
			except Exception as exc:  # noqa: BLE001 -- one bad file is not fatal
				self._log_event(f'skipped {document["filename"]}: {exc}')
		self.pulling = False
		self._end()
		self._log_event(f'{saved} of {len(found)} file(s) saved')
		with self._lock:
			self._dirty = True

	@on_keyboard('/')
	def begin_filter(self, ctx) -> None:
		"""Narrow the watch list by typing.

		On a long watch list grouped by source, reaching a call in the middle took
		dozens of presses of the down arrow, which is most of the work of using
		this screen.
		"""
		if not self.active or self.typing:
			return
		self.filtering = True
		self.left.filter_box = Text(
			content=self._filter,
			input=True,
			placeholder='type to narrow · enter to keep it · esc to clear',
		)
		self.left.grid_update_field('filter_box', visible=True)
		ctx.focus('filter_box')

	def _close_filter(self, ctx, *, clear: bool) -> None:
		self.filtering = False
		if clear:
			self._filter = ''
		# input=False again, or the box keeps eating every keystroke.
		self.left.filter_box = Text(content='', input=False)
		self.left.grid_update_field('filter_box', visible=False)
		ctx.blur()
		self._rebuild_tree()
		self.repaint()

	@on_keyboard('pageup')
	def page_up(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = max(0, self.selected - self._list_capacity())
			self._after_move()

	@on_keyboard('pagedown')
	def page_down(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = min(len(self._entries) - 1, self.selected + self._list_capacity())
			self._after_move()

	@on_keyboard('home')
	def go_to_top(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = 0
			self._after_move()

	@on_keyboard('end')
	def go_to_bottom(self) -> None:
		if self.active and not self.typing and self._entries:
			self.selected = len(self._entries) - 1
			self._after_move()

	@on_keyboard('a')
	def begin_add_source(self, ctx) -> None:
		if not self.active or self.typing:
			return
		self.adding = True
		self.left.add_source = Text(
			content='',
			input=True,
			placeholder='https://funder.example/feed.rss    enter to add · esc to cancel',
		)
		self.left.grid_update_field('add_source', visible=True)
		ctx.focus('add_source')

	@on_keyboard('escape')
	def cancel_typing(self, ctx) -> None:
		if self.adding:
			self._close_add_source(ctx)
		elif self.filtering:
			self._close_filter(ctx, clear=True)

	def _close_add_source(self, ctx) -> None:
		self.adding = False
		# input=False again, or the field keeps eating every keystroke.
		self.left.add_source = Text(content='', input=False)
		self.left.grid_update_field('add_source', visible=False)
		ctx.blur()

	@on_keyboard('enter')
	def on_enter(self, ctx) -> None:
		"""Submit the feed being typed, or open the selected call's page."""
		if not self.active:
			return
		if self.filtering:
			self._filter = str(self.left.filter_box.content or '').strip()
			self._close_filter(ctx, clear=False)
			return
		if not self.adding:
			self.open_selected()
			return
		url = str(self.left.add_source.content or '').strip()
		self._close_add_source(ctx)
		if not url:
			return
		# Adding fetches the feed to prove it parses, so it goes off-thread like
		# the scrape does.
		threading.Thread(target=self._add_worker, args=(url,), daemon=True).start()

	def open_selected(self) -> None:
		call = self._selected_call()
		if not call:
			return
		url = call.get('source_url')
		if not url:
			self._log_event(f'{call["topic_code"]} has no source URL')
			return
		if urlsplit(url).scheme not in ('http', 'https'):
			# The URL came from a scraped page; a file: or javascript: link must
			# never reach the browser.
			self._log_event(f'refused to open a non-web link: {url[:60]}')
			return
		self._log_event(f'opening {url[:70]}')
		# Off-thread: launching a browser shells out, and the panel should not
		# freeze while it does.
		threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

	def _add_worker(self, url: str) -> None:
		name = urlsplit(url).hostname or url
		try:
			added = german_funding.add_source(name, url)
		except Exception as exc:  # noqa: BLE001 -- reported in the log
			self._log_event(f'could not add {url}: {exc}')
			return
		self._log_event(f'added source {added["name"]} · {len(added["items"])} entries')
		with self._lock:
			self._dirty = True

	@on_keyboard('s')
	def scrape(self) -> None:
		# `typing` too: this binding never had it, so an s typed into the
		# add-source box started a scrape underneath it.
		if not self.active or self.typing or self.busy():
			return
		self.scraping = True
		self._begin('')
		with self._lock:
			self._log.append(f'{datetime.now().astimezone():%H:%M:%S} starting scrape')
			self._dirty = True
		threading.Thread(target=self._worker, daemon=True).start()

	def _log_event(self, message: str) -> None:
		with self._lock:
			self._log.append(f'{datetime.now().astimezone():%H:%M:%S} {message}')
			self._dirty = True

	def _worker(self) -> None:
		try:
			watch.discover(MAX_IMPORTS, on_event=self._log_event)
		except Exception as exc:  # noqa: BLE001 -- shown in the log, never a traceback
			self._log_event(f'scrape failed: {type(exc).__name__}: {exc}')
		else:
			# Rescore only after the watch list actually changed.
			portfolio = load_portfolio()
			rows = load_opportunities()
			with self._lock:
				self._portfolio = portfolio
				self.opportunities = rows
				self._dirty = True
			self._refresh_stages()
		finally:
			self.scraping = False
			self._end()
