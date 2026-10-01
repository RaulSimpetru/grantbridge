"""OpenAlex client: fetch one author's profile by OpenAlex ID, for the topic
import that adds inferred claims to the portfolio (portfolio.import_openalex_topics).

As of 2026, OpenAlex meters anonymous access to ~$0.10/day (~1000 requests) --
a free account raises that to $1/day. Set OPENALEX_API_KEY (from
openalex.org/settings/api) to use it; falls back to anonymous access, plus
OPENALEX_MAILTO as your polite-pool contact when that is set.
"""

import os
import time

import requests

BASE = 'https://api.openalex.org'
MAILTO = os.environ.get('OPENALEX_MAILTO')
API_KEY = os.environ.get('OPENALEX_API_KEY')


class OpenAlexBudgetExhausted(RuntimeError):
	"""Anonymous OpenAlex requests are metered (a small free daily budget) -- a
	hard daily wall, not transient throttling. Callers should
	stop and report, not block for hours."""


MAX_RETRY_SLEEP_S = 30


def _get(url: str, params: dict) -> requests.Response:
	"""GET with retry/backoff on 429. A short Retry-After is transient
	throttling (retry it); a long one means the daily budget is spent -- fail
	fast instead of sleeping for hours."""
	for attempt in range(5):
		extra = {'api_key': API_KEY} if API_KEY else {'mailto': MAILTO} if MAILTO else {}
		r = requests.get(url, params={**params, **extra}, timeout=30)
		if r.status_code != 429:
			return r
		retry_after = int(r.headers.get('Retry-After', 2**attempt))
		if retry_after > MAX_RETRY_SLEEP_S:
			raise OpenAlexBudgetExhausted(
				f'429 with Retry-After={retry_after}s (>{MAX_RETRY_SLEEP_S}s cap) -- '
				f'daily anonymous budget likely exhausted. Body: {r.text[:300]}'
			)
		time.sleep(retry_after)
	r.raise_for_status()
	return r


def get_author(author_id: str) -> dict:
	author_id = author_id.rsplit('/', 1)[-1]
	r = _get(f'{BASE}/authors/{author_id}', {})
	r.raise_for_status()
	return r.json()
