"""Append-only decision log: save/dismiss/develop/choose events on calls and
concepts. Single local user -- same identity boundary as portfolio.py: one
file, no auth, no per-user scoping.

An undo (unsave, undo_dismiss, unchoose) is a new event, never a delete or
edit of a prior row, so the log stays a faithful record of what actually
happened: each row has a timestamp, an actor and an optional reason, and the
Activity view reads them.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

DECISIONS_PATH = (
	Path(__file__).resolve().parent.parent.parent / 'data' / 'private' / 'decisions.jsonl'
)

Kind = Literal[
	'save', 'unsave', 'dismiss', 'undo_dismiss', 'develop', 'undevelop', 'choose', 'unchoose'
]
TargetType = Literal['call', 'concept']
# Rows written before the watch loop existed have no 'actor'; readers default
# them to 'researcher', which is what they were.
Actor = Literal['researcher', 'watcher']


def load() -> list[dict]:
	"""Every event, skipping any row that will not parse.

	The file is appended to by two processes and is read at startup by the
	terminal app. A process killed mid-append leaves a partial line, and
	raising here made every later launch fail with a JSONDecodeError instead
	of losing one row.
	"""
	if not DECISIONS_PATH.exists():
		return []
	events = []
	for line in DECISIONS_PATH.read_text().splitlines():
		if not line.strip():
			continue
		try:
			events.append(json.loads(line))
		except json.JSONDecodeError:
			continue
	return events


def dismissed_calls() -> set[str]:
	"""Calls the researcher dismissed and has not restored. The latest
	dismiss or undo_dismiss per call wins, as in web/src/decisions.ts."""
	dismissed = set()
	for event in load():
		if event.get('target_type') != 'call':
			continue
		if event.get('kind') == 'dismiss':
			dismissed.add(event['target_id'])
		elif event.get('kind') == 'undo_dismiss':
			dismissed.discard(event['target_id'])
	return dismissed


def record(
	kind: Kind,
	target_type: TargetType,
	target_id: str,
	reason: str | None = None,
	actor: Actor = 'researcher',
) -> dict:
	event = {
		'id': uuid.uuid4().hex[:12],
		'kind': kind,
		'target_type': target_type,
		'target_id': target_id,
		'reason': reason,
		'actor': actor,
		'created_at': datetime.now(UTC).isoformat(),
	}
	DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
	with DECISIONS_PATH.open('a') as f:
		f.write(json.dumps(event, ensure_ascii=False) + '\n')
	return event
