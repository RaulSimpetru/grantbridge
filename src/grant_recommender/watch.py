"""Long-running watch loop -- the autonomous half of GrantBridge.

Chains existing modules only: refresh the official German sources, score the
watch list against the confirmed portfolio (scoring.py), apply the same
deterministic eligibility gate the UI applies (eligibility.py), and develop
concepts for the best candidate (concepts.py). Each run it starts is written
through decisions.record() as the watcher's, so the existing Activity view shows
what the agent did and no new read surface is needed. Calls the researcher
dismissed are skipped. Imports and idle ticks are printed to the server log.

The gate is the product's, not a looser copy: a call the deterministic check
calls 'eligible' is developed as a real run; an 'uncertain' call is developed
as a *trial* run, which is exactly the state the UI uses for "generate anyway,
pending human eligibility review". An 'ineligible' call (typically a passed
deadline) is never developed.

One job at a time. A 12B model takes minutes per run, so a tick that finds a
job still running does nothing rather than queueing work the user did not ask
for.
"""

import threading
import time
from datetime import UTC, datetime

from grant_recommender import concepts, decisions, eligibility, german_funding
from grant_recommender.portfolio import usable
from grant_recommender.scoring import content_terms, score_calls


def providers() -> tuple[str, ...]:
	"""Built-in sources plus whatever the researcher has added."""
	return tuple(german_funding.all_sources())


# Kept for tests and callers that want to pin the set.
PROVIDERS = None

# A ministry feed is mostly press releases. Before spending minutes of model
# time on a call, require this many distinct content words (scoring.content_terms)
# shared with the confirmed claims. One is too easy to share by accident: every
# DFG notice ends with the same data-protection paragraph, and its "confidential
# treatment" matched any claim about treatment.
# Known limit: lexical, so synonyms and other languages never match. Raise it, or
# add a dense ranker, if the loop develops notices the researcher would not open.
MIN_CONTENT_WORDS = 2
MIN_FIT = 0.0


def plan(calls: list[dict], portfolio: list[dict], jobs: list[dict]) -> dict:
	"""Pure: decide what this tick should do. No I/O, no side effects.

	Returns an action dict. 'develop' carries the chosen topic_code and whether
	it must run as a trial (eligibility uncertain); anything else is 'idle'
	with a reason that is safe to show a human.
	"""
	if any(job.get('status') == 'running' for job in jobs):
		return {'action': 'idle', 'reason': 'A concept run is still in progress.'}
	if not [a for a in portfolio if usable(a)]:
		return {
			'action': 'idle',
			'reason': 'No confirmed portfolio capability; nothing to match calls against.',
		}
	if not calls:
		return {'action': 'idle', 'reason': 'No calls on the watch list.'}

	# Known limit: a failed run also retires its call, so one transient Ollama
	# error means that call is never retried. Track attempts per call if that
	# starts losing real candidates.
	seen = {job['topic_code'] for job in jobs}
	fit = score_calls(calls, portfolio)
	candidates = []
	for call in calls:
		code = call['topic_code']
		if code in seen:
			continue
		state = eligibility.assess(call, portfolio)['state']
		if state == 'ineligible':
			continue
		score = fit.get(code) or {}
		# Without a floor the loop always develops the least-irrelevant notice and
		# burns minutes of model time on it every tick. keyword_overlap counts
		# every token, so a shared 'in' alone would clear it; see MIN_CONTENT_WORDS.
		if (
			score.get('keyword_overlap', 0) <= MIN_FIT
			or len(content_terms(call, portfolio)) < MIN_CONTENT_WORDS
		):
			continue
		# eligible outranks uncertain, then the project's own fit ranking
		candidates.append((0 if state == 'eligible' else 1, score.get('rank', 10**6), code, state))

	if not candidates:
		return {
			'action': 'idle',
			'reason': 'No new call clears the fit floor and the deadline check.',
		}

	_, _, code, state = min(candidates)
	return {
		'action': 'develop',
		'topic_code': code,
		'eligibility': state,
		'trial': state != 'eligible',
		'reason': f'Top-ranked {state} call with no concept run yet.',
	}


def discover(max_imports: int, on_event=None) -> tuple[dict, list[dict]]:
	"""Refresh the official sources and pull any notice that is not on the
	watch list yet into it. This is the step that makes the loop *find* calls
	rather than re-rank a hand-curated list forever.

	Each import costs one fetch of the notice page, so a first run against a
	fresh feed is capped: the rest arrive on later ticks.

	`on_event(message)` reports each step as it happens, so a caller watching
	the scrape can show it rather than waiting for the summary.
	"""
	report = on_event or (lambda _message: None)
	sources: dict[str, str] = {}
	imported: list[dict] = []
	skipped: list[str] = []
	known = {call['topic_code'] for call in german_funding.watched_calls()}
	for provider in PROVIDERS or providers():
		try:
			report(f'fetching {provider}…')
			status = german_funding.refresh(provider)
			items = status.get('items', [])
			sources[provider] = status.get('error') or f'{len(items)} notices'
			report(f'{provider}: {sources[provider]}')
			for item in items:
				if len(imported) >= max_imports:
					report(f'import cap of {max_imports} reached; the rest arrive next run')
					break
				if item['id'] in known:
					continue
				try:
					call = german_funding.import_notice(provider, item['id'])
					imported.append(call)
					report(f'imported {item["id"]} · {str(call.get("title"))[:60]}')
				except Exception as exc:  # noqa: BLE001 -- one bad notice is not fatal
					skipped.append(f'{item["id"]}: {exc}')
					report(f'skipped {item["id"]}: {exc}')
		except Exception as exc:  # noqa: BLE001 -- a dead source must not kill the loop
			sources[provider] = f'refresh failed: {exc}'
			report(f'{provider} failed: {exc}')
	if skipped:
		sources['skipped'] = '; '.join(skipped[:5])
	report(f'done · {len(imported)} new call(s)')
	return sources, imported


def tick(
	calls: list[dict],
	portfolio: list[dict],
	*,
	refresh: bool = True,
	max_imports: int = 5,
	on_event=None,
) -> dict:
	"""One pass of the loop. Returns the action taken, with the job id if any."""
	# Checked before any network work: while a run is in flight this tick can
	# do nothing useful, and hitting the ministries' servers anyway would be
	# rude at a short interval.
	jobs = concepts.list_jobs()
	if any(job.get('status') == 'running' for job in jobs):
		return {
			'action': 'idle',
			'reason': 'A concept run is still in progress.',
			'sources': {},
			'imported': [],
			'checked_at': datetime.now(UTC).isoformat(),
		}

	sources, imported = discover(max_imports, on_event) if refresh else ({}, [])
	# Read again: discovery takes seconds, and a run started from the UI in the
	# meantime must count as both running and already seen.
	jobs = concepts.list_jobs()
	# A call the researcher dismissed is not a candidate, however well it fits.
	dismissed = decisions.dismissed_calls()
	action = plan(
		[call for call in calls + imported if call['topic_code'] not in dismissed], portfolio, jobs
	)
	action['sources'] = sources
	action['imported'] = [call['topic_code'] for call in imported]
	action['checked_at'] = datetime.now(UTC).isoformat()
	if action['action'] != 'develop':
		return action

	call = next(c for c in calls + imported if c['topic_code'] == action['topic_code'])
	# A run started without a reachable model fails at its first stage, and a
	# failed run retires its call -- so a machine with no Ollama would quietly
	# burn its best match. Wait instead; the call stays a candidate for the next
	# tick, and discovery and scoring above have already run.
	health = concepts.connection_status()
	if not health.get('ready'):
		# Same shape as the other idle results: no topic_code, so the log
		# line prints the reason rather than the call it is waiting on.
		return {
			'action': 'idle',
			'reason': f'Model not ready, so nothing was started. {health.get("detail", "")}'.strip(),
			'sources': action['sources'],
			'imported': action['imported'],
			'checked_at': action['checked_at'],
		}
	if on_event:
		on_event(f'developing concepts for {str(call.get("title") or call["topic_code"])[:60]}')
	job = concepts.start_job(call, portfolio, trial=action['trial'])
	action['job_id'] = job['id']
	decisions.record('develop', 'call', action['topic_code'], reason=job['id'], actor='watcher')
	return action


def run(
	calls_fn,
	portfolio_fn,
	*,
	interval_s: float,
	iterations: int | None = None,
	stop: threading.Event | None = None,
	on_tick=None,
	on_event=None,
) -> None:
	"""Loop until `iterations` ticks have run or `stop` is set.

	Sources are re-read every tick through the callables, so a portfolio the
	researcher edits mid-run takes effect on the next pass.
	"""
	count = 0
	while not (stop and stop.is_set()):
		try:
			result = tick(calls_fn(), portfolio_fn(), on_event=on_event)
		except Exception as exc:  # noqa: BLE001 -- the loop is the product; it never exits on error
			result = {
				'action': 'error',
				'reason': str(exc),
				'checked_at': datetime.now(UTC).isoformat(),
			}
		if on_tick:
			on_tick(result)
		count += 1
		if iterations is not None and count >= iterations:
			return
		if stop:
			stop.wait(interval_s)
		else:
			time.sleep(interval_s)


def start_background(calls_fn, portfolio_fn, *, interval_s: float) -> threading.Event:
	"""Daemon thread version used by the API process. Returns its stop event."""
	stop = threading.Event()
	threading.Thread(
		target=run,
		args=(calls_fn, portfolio_fn),
		kwargs={'interval_s': interval_s, 'stop': stop, 'on_tick': _log, 'on_event': _progress},
		daemon=True,
	).start()
	return stop


def _progress(message: str) -> None:
	"""Each step of a tick as it happens. Without this the loop is silent for the
	seconds it spends on the ministries' servers, which reads as a hung process."""
	print(f'[watch]   {message}', flush=True)


def _log(result: dict) -> None:
	detail = result.get('topic_code') or result.get('reason', '')
	print(f'[watch] {result["checked_at"]} {result["action"]}: {detail}', flush=True)
