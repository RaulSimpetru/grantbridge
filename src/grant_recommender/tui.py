"""GrantBridge review panel, as a terminal app.

One screen: pick a generated concept on the left, send it to the review panel,
and watch every reviewer think at once in its own pane. Reviewers run
concurrently against the local model, so the round costs about one reviewer's
wall time rather than the sum.

This reads concept runs straight off disk rather than through the API, and
never calls concepts._load_persisted_jobs(): that would mark a running server's
live jobs as interrupted and persist it. Reviews are written to their own
directory, so the TUI and the web app never contend for a file.

Run it with:  uv run python -m grant_recommender.tui
"""

import json
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from grant_recommender import concepts, reviews

JOBS_DIR = Path(__file__).resolve().parents[2] / 'data' / 'private' / 'concept_jobs'
VERDICT_STYLE = {'fund': 'bold green', 'revise': 'bold yellow', 'reject': 'bold red'}


def reviewable_documents() -> list[dict]:
	"""Every concept of every succeeded run, newest first. Read-only."""
	found = []
	for path in sorted(JOBS_DIR.glob('*.json')):
		try:
			job = json.loads(path.read_text())
		except (OSError, json.JSONDecodeError):
			continue
		if job.get('status') != 'succeeded' or not job.get('concepts'):
			continue
		for concept in job['concepts']:
			found.append(
				{
					'job': job,
					'concept_id': concept['concept_id'],
					'title': concept['title'],
					'topic_code': job['topic_code'],
					'created_at': job['created_at'],
				}
			)
	return sorted(found, key=lambda d: d['created_at'], reverse=True)


def _format_review(result: dict) -> str:
	"""The finished review as prose, not the JSON the model emitted. The reason
	for the verdict leads: it runs to several sentences, and the one-line
	status above the pane cut it off at the pane's edge."""
	blocks = [result['verdict_reason']] if result.get('verdict_reason') else []
	for heading, key in (
		('Strengths', 'strengths'),
		('Weaknesses', 'weaknesses'),
		('Questions for the applicant', 'questions_for_applicant'),
	):
		items = result.get(key) or []
		if items:
			blocks.append(heading + '\n' + '\n'.join(f'  • {item}' for item in items))
	claims = result.get('unsupported_claims') or []
	if claims:
		blocks.append(
			'Unsupported claims\n'
			+ '\n'.join(f'  • {c["claim"]}\n    → {c["why_unsupported"]}' for c in claims)
		)
	return '\n\n'.join(blocks)


class ReviewPane(Vertical):
	"""One reviewer: its remit, its live output, its verdict."""

	def __init__(self, role_key: str) -> None:
		super().__init__(id=f'pane-{role_key}', classes='pane')
		self.role_key = role_key

	def compose(self) -> ComposeResult:
		role = reviews.ROLES[self.role_key]
		yield Label(role['label'], classes='pane-title')
		yield Label('idle', id=f'status-{self.role_key}', classes='pane-status')
		# A Static that is rewritten wholesale, not a RichLog: RichLog is
		# line-oriented, so streaming deltas into it puts each fragment on its
		# own line and the output comes out one word wide.
		with VerticalScroll(id=f'scroll-{self.role_key}', classes='pane-log'):
			yield Static('', id=f'log-{self.role_key}', markup=False)

	def set_status(self, text: str, style: str = '') -> None:
		label = self.query_one(f'#status-{self.role_key}', Label)
		label.update(text)
		label.set_classes(f'pane-status {style}'.strip())

	def set_text(self, text: str, *, follow: bool = True) -> None:
		self.query_one(f'#log-{self.role_key}', Static).update(text)
		scroll = self.query_one(f'#scroll-{self.role_key}', VerticalScroll)
		# A stream is read at its end, where the model is; a finished review
		# from its start, where the reason for the verdict is.
		if follow:
			scroll.scroll_end(animate=False)
		else:
			scroll.scroll_home(animate=False)

	def clear(self) -> None:
		self.query_one(f'#log-{self.role_key}', Static).update('')


class PanelApp(App):
	"""The review panel screen."""

	CSS = """
	Screen { layout: vertical; }
	#body { height: 1fr; }
	#documents { width: 42; border-right: solid $panel-lighten-2; }
	#documents ListItem { padding: 0 1; }
	#panes { height: 1fr; }
	.pane { border: round $panel-lighten-2; padding: 0 1; }
	.pane-title { text-style: bold; }
	.pane-status { color: $text-muted; }
	.pane-status.fund { color: $success; text-style: bold; }
	.pane-status.revise { color: $warning; text-style: bold; }
	.pane-status.reject { color: $error; text-style: bold; }
	.pane-status.failed { color: $error; }
	.pane-log { height: 1fr; background: $surface-darken-1; }
	#summary { height: auto; max-height: 14; border: round $panel-lighten-2; padding: 0 1; }
	"""

	BINDINGS: ClassVar = [
		('r', 'run_round', 'Review selected'),
		('c', 'clear', 'Clear panes'),
		('q', 'quit', 'Quit'),
	]

	def __init__(self) -> None:
		super().__init__()
		self.documents = reviewable_documents()
		self.running = False
		self._seen: dict[str, int] = {}

	def compose(self) -> ComposeResult:
		yield Header(show_clock=True)
		with Horizontal(id='body'):
			with Vertical(id='documents'):
				yield Label('Generated concepts', classes='pane-title')
				# markup=False: "[topic_code]" read as a style tag and vanished,
				# and so would brackets in a model-written title.
				yield ListView(
					*[
						ListItem(Label(f'{d["title"]}\n[{d["topic_code"]}]', markup=False))
						for d in self.documents
					],
					id='doclist',
				)
			with Vertical():
				with Horizontal(id='panes'):
					for key in reviews.ROLES:
						yield ReviewPane(key)
				# Model text, which cites passages as [SOURCE_ID]: never markup.
				yield VerticalScroll(Static('', id='summary-body', markup=False), id='summary')
		yield Footer()

	def on_mount(self) -> None:
		self.title = 'GrantBridge — review panel'
		model = concepts.connection_settings()['model']
		count = len(self.documents)
		self.sub_title = f'{count} reviewable concepts · {model} · press r to review'
		if self.documents:
			self.query_one('#doclist', ListView).index = 0

	def _selected(self) -> dict | None:
		listview = self.query_one('#doclist', ListView)
		if listview.index is None or not self.documents:
			return None
		return self.documents[listview.index]

	def action_clear(self) -> None:
		for key in reviews.ROLES:
			pane = self.query_one(f'#pane-{key}', ReviewPane)
			pane.clear()
			pane.set_status('idle')
		self.query_one('#summary-body', Static).update('')

	def action_run_round(self) -> None:
		if self.running:
			self.notify('A round is already in progress.', severity='warning')
			return
		document = self._selected()
		if not document:
			self.notify('No concept selected.', severity='error')
			return
		self.running = True
		self._seen = {}
		self.action_clear()
		for key in reviews.ROLES:
			self.query_one(f'#pane-{key}', ReviewPane).set_status('waiting for model…')
		self.sub_title = f'reviewing: {document["title"][:60]}'
		self.run_review(document)

	# -- streaming callbacks, called from the worker thread ------------------

	def _pane(self, role: str) -> ReviewPane | None:
		"""None once the screen is gone. Reviewer threads outlive a quit, so a
		late callback must be a no-op rather than an exception in a worker."""
		try:
			return self.query_one(f'#pane-{role}', ReviewPane)
		except NoMatches:
			return None

	def _on_text(self, role: str, text: str) -> None:
		"""run_round hands over the whole text so far, which is exactly what the
		pane renders. The tail is capped upstream, so rewriting is cheap."""
		first = self._seen.get(role, 0) == 0
		if len(text) <= self._seen.get(role, 0):
			return
		pane = self._pane(role)
		if pane is None:
			return
		self._seen[role] = len(text)
		if first:
			pane.set_status('writing…')
		pane.set_text(text)

	def _on_done(self, role: str, record: dict) -> None:
		pane = self._pane(role)
		if pane is None:
			return
		if record['status'] != 'succeeded':
			pane.set_status('failed', 'failed')
			pane.set_text(str(record.get('error', 'unknown error')))
			return
		result = record['result']
		pane.set_status(result['verdict'].upper(), result['verdict'])
		# The raw stream was the "watch it think" view; once the verdict is in,
		# the pane should show the review a person actually reads.
		pane.set_text(_format_review(result), follow=False)

	def _finish(self, result: dict) -> None:
		self.running = False
		if not self.is_running:
			return
		summary = result['consolidated']
		tally = summary['tally']
		counts = f'fund {tally["fund"]}   revise {tally["revise"]}   reject {tally["reject"]}'
		lines = [f'PANEL OUTCOME: {(summary["outcome"] or "no verdict").upper()}   {counts}']
		if summary['unsupported_claims']:
			lines.append('')
			lines.append('Unsupported claims raised:')
			lines += [
				f'  • ({c["raised_by"]}) {c["claim"]} — {c["why_unsupported"]}'
				for c in summary['unsupported_claims']
			]
		if summary['questions']:
			lines.append('')
			lines.append('Questions for the applicant:')
			lines += [f'  • ({q["role"]}) {q["question"]}' for q in summary['questions']]
		if summary['failed_roles']:
			lines.append('')
			lines.append(f'Reviewers that failed: {", ".join(summary["failed_roles"])}')
		lines.append('')
		lines.append(f'Saved as round {result["id"]}.')
		self.query_one('#summary-body', Static).update('\n'.join(lines))
		self.sub_title = f'round {result["id"]} complete'

	@work(thread=True, exclusive=True)
	def run_review(self, document: dict) -> None:
		try:
			prepared = reviews.from_concept(document['job'], document['concept_id'])
			result = reviews.run_round(
				prepared,
				on_text=lambda role, text: self.call_from_thread(self._on_text, role, text),
				on_done=lambda role, record: self.call_from_thread(self._on_done, role, record),
			)
		except Exception as exc:  # noqa: BLE001 -- surfaced in the UI, never a traceback dump
			self.call_from_thread(self._fail, f'{type(exc).__name__}: {exc}')
			return
		self.call_from_thread(self._finish, result)

	def _fail(self, message: str) -> None:
		self.running = False
		if not self.is_running:
			return
		self.query_one('#summary-body', Static).update(f'Round failed: {message}')
		self.notify(message, severity='error')


def main() -> None:
	PanelApp().run()


if __name__ == '__main__':
	main()
