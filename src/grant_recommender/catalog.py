"""The watch list: which calls this researcher is tracking, from every source.

Lives here rather than in api.py so the terminal panels can assemble the same
list without importing FastAPI. api.py keeps its own thin wrappers because its
tests patch the resolver seams.
"""

import json
from pathlib import Path

import requests

from grant_recommender import german_funding
from grant_recommender.calls import get_current_topic as get_eu_topic

WATCHED_TOPICS_PATH = Path(__file__).resolve().parents[2] / 'data' / 'watched_topics.json'


class SourcesUnavailable(RuntimeError):
	"""No source answered. Distinct from an empty watch list, which is a fact."""


def watched_topic_codes() -> list[str]:
	# No curated list, or a blank one, means no EU topics rather than a 500; the
	# German and user-added sources still fill the watch list.
	text = WATCHED_TOPICS_PATH.read_text().strip() if WATCHED_TOPICS_PATH.exists() else ''
	return json.loads(text or '{}').get('topic_codes') or []


def get_topic(code: str) -> dict | None:
	# Imported notices (DFG, BMFTR, user-added feeds) answer from the local watch
	# list. A prefix check here missed the user-added ones, which SEDIA 404s.
	return german_funding.get_call(code) or get_eu_topic(code)


def watched_calls(codes=None, resolve=None) -> list[dict]:
	"""Curated EU topic codes plus every imported notice (DFG, BMFTR, added feeds).

	`codes` and `resolve` exist so a caller can substitute its own resolution,
	which is how api.py keeps the seams its tests patch.
	"""
	codes = watched_topic_codes() if codes is None else codes
	resolve = get_topic if resolve is None else resolve
	found = []
	for code in codes:
		try:
			call = resolve(code)
		except requests.RequestException:
			continue
		if call:
			found.append(call)
	found.extend(german_funding.watched_calls())
	# With no codes no source was asked, so empty is a fact. Raising would also
	# stop the watch loop before the discover() that fills the list.
	if not found and codes:
		raise SourcesUnavailable('Call sources are unavailable. Retry shortly.')
	return found
