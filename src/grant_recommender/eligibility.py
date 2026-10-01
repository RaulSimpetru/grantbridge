"""Conservative eligibility gate with versioned, explicit researcher review.

External annexes cannot be inferred from similarity. A human review is scoped
to the exact call and portfolio snapshot; expiry always overrides that review.
"""

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REVIEW_PATH = Path(__file__).resolve().parents[2] / 'data/private/eligibility.json'
_LOCK = threading.RLock()


def fingerprint(call: dict, portfolio: list[dict]) -> str:
	content = {
		k: call.get(k) for k in ('topic_code', 'scope_text', 'topic_conditions', 'deadlines')
	}
	return hashlib.sha256(
		json.dumps([content, portfolio, availability(call)], sort_keys=True).encode()
	).hexdigest()


def availability(call: dict) -> str:
	# First-stage deadlines matter: a future second stage isn't open to new applicants.
	deadlines = []
	for value in call.get('deadlines', []):
		try:
			stamp = datetime.fromisoformat(value)
			deadlines.append(stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp)
		except ValueError:
			continue
	if not deadlines:
		period = re.fullmatch(r'\s*\d{4}\s*-\s*(\d{4})\s*', call.get('programme_period') or '')
		if period and int(period[1]) < datetime.now(UTC).year:
			return 'archived'
		return 'unknown'
	now = datetime.now(UTC)
	today = now.astimezone(ZoneInfo('Europe/Brussels')).date()

	# SEDIA sometimes encodes a date with 00:00 rather than the true closing
	# time. Do not close a call at the start of its final submission day.
	def expired(stamp):
		if stamp.hour == stamp.minute == stamp.second == 0:
			return stamp.date() < today
		return stamp < now

	if all(expired(d) for d in deadlines):
		return 'closed'
	if expired(min(deadlines)) and len(deadlines) > 1:
		return 'later_stage'
	if any(d.date() == today and d.hour == d.minute == d.second == 0 for d in deadlines):
		return 'closing_today'
	return 'upcoming_deadline'


def assess(call: dict, portfolio: list[dict]) -> dict:
	state = availability(call)
	if state in ('closed', 'archived'):
		return {
			'state': 'ineligible',
			'reason': 'All recorded deadlines have passed.'
			if state == 'closed'
			else 'Archived programme: no current submission window is recorded.',
			'basis': 'deadline',
		}
	reviews = json.loads(REVIEW_PATH.read_text()) if REVIEW_PATH.exists() else {}
	review = reviews.get(call['topic_code'])
	if review and review['fingerprint'] == fingerprint(call, portfolio):
		return {
			'state': review['state'],
			'reason': review['reason'],
			'basis': 'researcher_review',
			'source': review['source'],
			'reviewed_at': review['reviewed_at'],
		}
	reason = 'Read the conditions on the call and decide whether you can apply.'
	if review:
		reason = 'The call or portfolio changed after your review. Review eligibility again.'
	if state == 'later_stage':
		reason += ' The first deadline has passed; confirm an invitation to the later stage.'
	if state == 'unknown':
		reason += ' No deadline is recorded; verify the current submission window.'
	if state == 'closing_today':
		reason += (
			' The recorded deadline is today; verify the exact closing time in the official portal.'
		)
	return {'state': 'uncertain', 'reason': reason, 'basis': 'missing_review'}


def record_review(call: dict, portfolio: list[dict], state: str, reason: str, source: str) -> dict:
	with _LOCK:
		reviews = json.loads(REVIEW_PATH.read_text()) if REVIEW_PATH.exists() else {}
		reviews[call['topic_code']] = {
			'state': state,
			'reason': reason,
			'source': source,
			'fingerprint': fingerprint(call, portfolio),
			'reviewed_at': datetime.now(UTC).isoformat(),
		}
		REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
		temp = REVIEW_PATH.with_suffix('.tmp')
		temp.write_text(json.dumps(reviews, indent=2))
		temp.replace(REVIEW_PATH)
	return assess(call, portfolio)
