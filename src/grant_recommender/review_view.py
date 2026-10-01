"""Rendering for a review round: one pane per reviewer, plus the consolidated
verdict.

Lives apart from any screen so the same panes can sit inside the opportunities
screen. Pure functions of a round's state, so nothing here touches a grid or a
thread.
"""

from xnano import BaseGrid, Field
from xnano.components.text import Text

from grant_recommender import reviews
from grant_recommender.panel_widgets import ACCENT, IDLE_BORDER, MUTED, VERDICT_COLOR

LIVE_TAIL_LINES = 24


def tail(text: str, lines: int = LIVE_TAIL_LINES) -> str:
	"""A pane shows the end of the stream, which is where the model is."""
	return '\n'.join(text.splitlines()[-lines:])


def format_review(result: dict) -> str:
	"""The finished review as prose, not the JSON the model emitted."""
	blocks = []
	for heading, key in (
		('STRENGTHS', 'strengths'),
		('WEAKNESSES', 'weaknesses'),
		('QUESTIONS', 'questions_for_applicant'),
	):
		items = result.get(key) or []
		if items:
			blocks.append(heading + '\n' + '\n'.join(f'· {item}' for item in items))
	claims = result.get('unsupported_claims') or []
	if claims:
		blocks.append(
			'UNSUPPORTED\n'
			+ '\n'.join(f'· {c["claim"]}\n  → {c["why_unsupported"]}' for c in claims)
		)
	return '\n\n'.join(blocks)


def pane_content(role: str, records: dict, live: dict, running: bool) -> tuple[Text, str]:
	"""One reviewer's pane and the border colour for its state."""
	record = records.get(role)
	if record and record.get('status') == 'succeeded':
		result = record['result']
		verdict = result['verdict']
		return (
			Text(
				content=[
					Text(
						content=verdict.upper() + '\n',
						foreground=VERDICT_COLOR[verdict],
						modifiers=('bold',),
					),
					Text(content=result['verdict_reason'] + '\n\n', foreground=MUTED),
					Text(content=format_review(result)),
				]
			),
			VERDICT_COLOR[verdict],
		)
	if record and record.get('status') == 'failed':
		return Text(content=str(record.get('error', 'failed')), foreground='red'), 'red'
	streamed = live.get(role, '')
	if streamed:
		return (
			Text(
				content=[
					Text(content='writing…\n', foreground=ACCENT),
					Text(content=tail(streamed), foreground=MUTED),
				]
			),
			ACCENT,
		)
	if running:
		return Text(content='waiting for the model…', foreground=MUTED), ACCENT
	return Text(content=''), IDLE_BORDER


def summary_content(
	round_result: dict | None,
	*,
	running: bool = False,
	live: dict | None = None,
	records: dict | None = None,
) -> Text:
	"""The consolidated verdict, assembled from the round rather than a model.

	Below the width where the three reviewer panes fit, this is the only thing
	on screen, so it carries the weaknesses and any failed reviewer too --
	without them a partial panel read as a complete one, and a round in
	progress read as one that had never been asked for.
	"""
	if running:
		parts = [Text(content='The panel is reading it now.\n\n', foreground=ACCENT)]
		for role in reviews.ROLES:
			streamed = (live or {}).get(role, '')
			state = f'writing, {len(streamed)} characters' if streamed else 'waiting for the model'
			# A reviewer that has landed is done, however much it streamed.
			record = (records or {}).get(role) or {}
			if record.get('status') == 'succeeded':
				state = f'done: {record["result"]["verdict"]}'
			elif record.get('status') == 'failed':
				state = 'failed'
			parts.append(Text(content=f'{role}: {state}\n', foreground=MUTED))
		newest = max((live or {}).values(), key=len, default='')
		if newest:
			parts.append(Text(content='\n' + tail(newest, 8), foreground=MUTED))
		return Text(content=parts)
	if not round_result:
		return Text(
			content='No review yet. Press r to send this concept to the panel.',
			foreground=MUTED,
		)
	summary = round_result['consolidated']
	outcome = (summary['outcome'] or 'no verdict').upper()
	tally = summary['tally']
	parts = [
		Text(
			content=f'PANEL OUTCOME  {outcome}\n',
			foreground=VERDICT_COLOR.get(summary['outcome'] or '', MUTED),
			modifiers=('bold',),
		),
		Text(
			content=f'fund {tally["fund"]}   revise {tally["revise"]}   reject {tally["reject"]}\n',
			foreground=MUTED,
		),
	]
	# .get(): rounds written before these keys existed do not carry them.
	failed = summary.get('failed_roles') or []
	if failed:
		parts.append(
			Text(
				content=f'{len(failed)} reviewer(s) failed ({", ".join(failed)}); '
				'this tally is incomplete\n',
				foreground='red',
			)
		)
	parts.append(Text(content='\n'))
	for weakness in summary.get('weaknesses') or []:
		parts.append(Text(content=f'– ({weakness["role"]}) {weakness["weakness"]}\n'))
	parts += [
		Text(content=f'· ({c["raised_by"]}) {c["claim"]}\n  → {c["why_unsupported"]}\n')
		for c in summary['unsupported_claims']
	]
	parts += [Text(content=f'? ({q["role"]}) {q["question"]}\n') for q in summary['questions']]
	return Text(content=parts)


class ReviewPanes(BaseGrid, direction='horizontal', gap=1):
	"""One bordered pane per reviewer, in the order the roles are declared."""

	methods: Text = Field(
		default_factory=lambda: Text(content=''),
		border='rounded',
		border_color=IDLE_BORDER,
		title=' Methodology & evaluation ',
		padding=(0, 1),
		scroll=True,
		group='review_methods',
	)
	feasibility: Text = Field(
		default_factory=lambda: Text(content=''),
		border='rounded',
		border_color=IDLE_BORDER,
		title=' Feasibility, resources & risk ',
		padding=(0, 1),
		scroll=True,
		group='review_feasibility',
	)
	fit: Text = Field(
		default_factory=lambda: Text(content=''),
		border='rounded',
		border_color=IDLE_BORDER,
		title=' Fit to this call ',
		padding=(0, 1),
		scroll=True,
		group='review_fit',
	)

	def paint(self, records: dict, live: dict, running: bool) -> None:
		for role in reviews.ROLES:
			content, color = pane_content(role, records, live, running)
			setattr(self, role, content)
			self.grid_update_field(role, border_color=color)
