"""EU Funding & Tenders Portal (SEDIA) client for topic text and deadlines.

SEDIA is the portal's own search backend (public, no auth; apiKey=SEDIA is the
portal frontend's own key).
"""

import json
import re
import threading
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import requests

from grant_recommender.source_document import source_document

SEARCH_URL = 'https://api.tech.ec.europa.eu/search-api/prod/rest/search'
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'sedia_cache.json'
_cache: dict[str, dict | None] | None = None
_LIVE_PATH = _CACHE_PATH.with_name('sedia_live.json')
_LIVE_LOCK = threading.RLock()
# Bump when strip_html changes, so a capture parsed the old way is fetched
# again instead of being shown until it expires. Version 2 keeps paragraph and
# list boundaries.
TEXT_VERSION = 2


def get_current_topic(topic_code: str) -> dict | None:
	"""Topic text, cached for 24 hours. If a refresh fails, the last capture is
	returned with a source_warning."""
	with _LIVE_LOCK:
		cache = json.loads(_LIVE_PATH.read_text()) if _LIVE_PATH.exists() else {}
		previous = cache.get(topic_code)
		if (
			previous
			and previous.get('source_record_type') == '1'
			and previous.get('display_document_version') == 1
			and previous.get('text_version') == TEXT_VERSION
			and (datetime.now(UTC) - datetime.fromisoformat(previous['fetched_at'])).total_seconds()
			< 86400
		):
			return previous
		try:
			current = _fetch_topic(topic_code)
		except requests.RequestException:
			if previous:
				return {
					**previous,
					'source_warning': 'Source refresh failed; showing the previously captured version.',
				}
			raise
		if current:
			current = {**current, 'text_version': TEXT_VERSION}
			cache[topic_code] = current
			_LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
			temp = _LIVE_PATH.with_suffix('.tmp')
			temp.write_text(json.dumps(cache))
			temp.replace(_LIVE_PATH)
		return current


def _load_cache() -> dict[str, dict | None]:
	global _cache
	if _cache is None:
		_cache = json.loads(_CACHE_PATH.read_text()) if _CACHE_PATH.exists() else {}
	return _cache


def _save_cache() -> None:
	_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
	_CACHE_PATH.write_text(json.dumps(_cache))


class _HTMLStripper(HTMLParser):
	def __init__(self):
		super().__init__()
		self.parts: list[str] = []

	def handle_starttag(self, tag, attrs):
		# A block boundary becomes a line break; without it '<p>medicines.</p><p>However'
		# reads 'medicines.However'.
		if tag in {'p', 'li', 'br', 'div', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
			self.parts.append('\n')

	def handle_data(self, data: str) -> None:
		self.parts.append(data)


def strip_html(html: str) -> str:
	s = _HTMLStripper()
	s.feed(html)
	text = ''.join(s.parts)
	return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def get_topic(topic_code: str) -> dict | None:
	"""Fetch scope text and deadline(s) for one topic code, or None if not found.
	Disk-cached across runs."""
	cache = _load_cache()
	if topic_code in cache:
		return cache[topic_code]
	result = _fetch_topic(topic_code)
	cache[topic_code] = result
	_save_cache()
	return result


def _fetch_topic(topic_code: str) -> dict | None:
	r = requests.post(
		SEARCH_URL,
		params={'apiKey': 'SEDIA', 'text': f'"{topic_code}"', 'pageSize': 50, 'pageNumber': 1},
		json={'query': {'bool': {'must': []}}},
		timeout=30,
	)
	r.raise_for_status()
	results = r.json().get('results', [])
	# SEDIA returns language variants in arbitrary order. Prefer English.
	for res in sorted(results, key=lambda r: r['metadata'].get('language', ['en'])[0] != 'en'):
		meta = res['metadata']
		if meta.get('identifier', [None])[0] == topic_code and meta.get('type', ['1'])[0] == '1':
			return {
				'source_record_type': '1',
				'display_document_version': 1,
				'topic_conditions_document': source_document(meta.get('topicConditions', [''])[0]),
				'topic_code': topic_code,
				'title': meta.get('title', [None])[0],
				'call_identifier': meta.get('callIdentifier', [None])[0],
				'call_title': meta.get('callTitle', [None])[0],
				'deadlines': meta.get('deadlineDate', []),
				# opaque source status code -- SEDIA gives no documented decode
				# table, so this is passed through raw rather than guessed at.
				'status': meta.get('status', [None])[0],
				'types_of_action': meta.get('typesOfAction', []),
				'scope_text': strip_html(meta.get('descriptionByte', [''])[0]),
				# mostly boilerplate pointing at externally-referenced General
				# Annexes rather than call-specific structured rules -- kept
				# for display, not treated as a source for deterministic checks.
				'topic_conditions': strip_html(meta.get('topicConditions', [''])[0]),
				'framework_programme': meta.get('frameworkProgramme', [None])[0],
				'programme_period': meta.get('programmePeriod', [None])[0],
				# the portal's own JSON record for this topic, not a formatted
				# page -- the closest thing to a source URL SEDIA exposes.
				'source_url': meta.get('url', [None])[0],
				'fetched_at': datetime.now(UTC).isoformat(),
			}
	return None
