"""Where each call and each concept has got to.

The watch list is a work queue, not a ranking, so every row has to say what has
already happened to it. That state is spread over four stores -- refinement
caches, concept runs, review rounds and the decision log -- so it is gathered
once per refresh and handed to the painter, rather than recomputed per row. One
pass over the concept runs instead of one pass per call.

Nothing here knows about colours or widgets: it answers what happened, and the
panel decides how to draw it.
"""

import json
import re
from pathlib import Path

from grant_recommender import decisions, refinement, reviews

JOBS_DIR = Path(__file__).resolve().parents[2] / 'data' / 'private' / 'concept_jobs'

# The four things that can have happened to a call, in the order they happen.
STAGES = ('read', 'concepts', 'reviewed', 'decided')

# The concept's own fields, in the order a reader needs them: what it proposes,
# why it suits this call, why it suits this researcher, then the caveats. Lives
# here rather than in the panel so a document can use the same order without
# importing a screen.
CONCEPT_FIELDS = (
	('PROBLEM', 'problem'),
	('HYPOTHESIS', 'central_hypothesis'),
	('CALL FIT', 'call_fit'),
	('YOUR FIT', 'researcher_fit'),
	('OBJECTIVES', 'objectives'),
	('NEEDS', 'required_assets_and_collaborators'),
	('NOVELTY', 'novelty'),
	('RISK', 'principal_feasibility_risk'),
)


# `reviews.from_concept` records its provenance as prose rather than as fields.
# Parsing it keeps the rounds already on disk readable; adding a field would
# only have covered new ones.
# The run id is the half whose shape is fixed, so that is the half anchored: a
# concept id is a model-produced string and has been seen carrying spaces.
ORIGIN = re.compile(r'concept (.+) of run (\S+)$')

# A decision that settles a concept, and the undo that unsettles it again. The
# log is append-only, so an undo is a later event and never a deletion.
# What each decision means, and which of them undo another. Decisions come in
# families -- parking and pursuing are not exclusive -- so an undo clears only
# its own family. Keyed on the kind alone, unsaving a concept also erased the
# pursue that had opened its draft.
FAMILY = {
	'develop': 'pursued',
	'undevelop': 'pursued',
	'choose': 'chosen',
	'unchoose': 'chosen',
	'save': 'parked',
	'unsave': 'parked',
	'dismiss': 'dropped',
	'undo_dismiss': 'dropped',
}
UNDOES = frozenset({'unchoose', 'unsave', 'undo_dismiss', 'undevelop'})

# The undo for whatever a concept currently stands at. Undoing a pursue leaves
# the draft alone: the file exists, it is just no longer the one being pursued.
UNDO_FOR = {
	'pursued': 'undevelop',
	'chosen': 'unchoose',
	'parked': 'unsave',
	'dropped': 'undo_dismiss',
}


def concept_key(job_id: str, concept_id: str) -> str:
	"""A concept id is only unique inside its own run -- they are '1', '2', '3'
	-- so a decision needs both halves. Without the run id, deciding on run A's
	first concept would also mark run B's. The separator matches the rows the
	web UI already wrote."""
	return f'{job_id}/{concept_id}'


def concept_runs() -> dict[str, list[dict]]:
	"""topic_code -> its concepts, newest run first.

	Read-only: the server owns these files and must not have its job state
	rewritten by a second process.
	"""
	by_call: dict[str, list[dict]] = {}
	for path in sorted(JOBS_DIR.glob('*.json')):
		try:
			job = json.loads(path.read_text())
		except (OSError, json.JSONDecodeError):
			continue
		if job.get('status') != 'succeeded' or not job.get('topic_code'):
			continue
		by_call.setdefault(job['topic_code'], []).extend(
			{
				'job_id': job['id'],
				'created_at': job['created_at'],
				'trial': bool(job.get('trial')),
				'model': job.get('model', ''),
				# This run was seeded from the private application archive.
				# Anything that puts it on screen has to say so.
				'archive': bool(job.get('archive_context')),
				'concept': concept,
			}
			for concept in job.get('concepts') or []
		)
	for items in by_call.values():
		items.sort(key=lambda c: c['created_at'], reverse=True)
	return by_call


def reviewed() -> dict[str, dict]:
	"""concept key -> the most recent review round for it, so a caller can both
	report the outcome and reopen the round it came from."""
	found: dict[str, dict] = {}
	for round_ in reviews.list_rounds():  # already newest first
		match = ORIGIN.search(round_.get('origin') or '')
		# A round where every reviewer failed carries no outcome. It is not a
		# review, and counting it rendered the state as the string 'panel: None'.
		if match and round_.get('outcome'):
			found.setdefault(concept_key(match.group(2), match.group(1)), round_)
	return found


def decided() -> dict[str, str]:
	"""concept key -> what was decided about it, the latest decision that still
	stands winning.

	Each concept carries a set of standing decisions rather than one slot,
	because an undo names a family, not a target. With a single slot, unsaving
	a concept that had also been pursued dropped the pursue as well, and its
	draft went on existing with nothing in the panel pointing at it.
	"""
	standing: dict[str, dict[str, int]] = {}
	for order, event in enumerate(decisions.load()):  # append-only, oldest first
		if event.get('target_type') != 'concept':
			continue
		family = FAMILY.get(event.get('kind'))
		if not family:
			continue
		slots = standing.setdefault(event['target_id'], {})
		if event['kind'] in UNDOES:
			slots.pop(family, None)
		else:
			slots[family] = order
	return {
		target: max(slots, key=slots.__getitem__) for target, slots in standing.items() if slots
	}


def snapshot(calls: list[dict]) -> dict:
	"""Every store read once. Cheap enough to refresh when work finishes, far
	too slow to recompute on the 200 ms paint tick."""
	return {
		'concepts': concept_runs(),
		'reviewed': reviewed(),
		'decided': decided(),
		'read': {call['topic_code'] for call in calls if refinement.cached(call)},
	}


def keys_for(call: dict, snap: dict) -> list[str]:
	return [
		concept_key(item['job_id'], item['concept']['concept_id'])
		for item in snap['concepts'].get(call['topic_code']) or []
	]


def call_progress(call: dict, snap: dict) -> tuple[bool, bool, bool, bool]:
	"""Which of STAGES this call has reached, as one flag each."""
	keys = keys_for(call, snap)
	return (
		call['topic_code'] in snap['read'],
		bool(keys),
		any(key in snap['reviewed'] for key in keys),
		any(key in snap['decided'] for key in keys),
	)


def concept_state(item: dict, snap: dict) -> str:
	"""A short phrase for where one concept stands. A decision outranks a
	review, because deciding is the later step."""
	key = concept_key(item['job_id'], item['concept']['concept_id'])
	if key in snap['decided']:
		return snap['decided'][key]
	round_ = snap['reviewed'].get(key)
	return f'panel: {round_["outcome"]}' if round_ else 'not reviewed'
