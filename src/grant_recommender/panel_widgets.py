"""Shared pieces for the terminal panels.

`RowList` exists because there is no other way in xnano to paint one styled row
per entry. A `Text` whose content is a list of `Text`s concatenates them inline,
and adding newlines double-spaces them, so a list that needs per-row colour has
to go through a component that returns `TextBlock(lines=...)`.
"""

import dataclasses

from xnano.components.component import Component, ComponentRenderContext
from xnano.core.content import Run, TextBlock

ACCENT = 'cyan'
MUTED = 'darkgray'
IDLE_BORDER = 'darkgray'

VERDICT_COLOR = {'fund': 'green', 'revise': 'yellow', 'reject': 'red'}
ELIGIBILITY_COLOR = {'eligible': 'green', 'uncertain': 'yellow', 'ineligible': 'red'}

Row = tuple[Run, ...]

# One shared spinner so every place that shows work in flight animates in step.
SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'


def spinner(elapsed: float) -> str:
	return SPINNER[int(elapsed * 8) % len(SPINNER)]


@dataclasses.dataclass
class RowList(Component):
	"""A list of styled rows. `rows` is a tuple of tuples of `Run`."""

	rows: tuple[Row, ...] = ()

	def compose(self, ctx: ComponentRenderContext) -> TextBlock:
		return TextBlock(lines=self.rows)


def message_rows(text: str, color: str = MUTED) -> tuple[Row, ...]:
	"""An empty-state message in the same shape as a row list."""
	return ((Run(text=text, foreground=color),),)
