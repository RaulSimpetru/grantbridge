"""Bounded official-source discovery and explicit imports.

Two built-in German sources plus any source the researcher adds. An added
source is read as RSS/Atom first, which is exact and costs no model time;
otherwise an LLM may only choose among the on-host links found on the page, by
position, so it cannot invent a URL. Every fetch stays pinned to the host the
source itself came from, so adding a source cannot widen what this module is
allowed to reach.
"""

import hashlib
import json
import re
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

DATA = Path(__file__).resolve().parents[2] / 'data'
CACHE = DATA / 'raw/german_funding'
WATCHLIST = DATA / 'private/german_watchlist.json'
_LOCK = threading.RLock()
DFG_PROGRAMME = (
	'https://www.dfg.de/de/foerderung/foerdermoeglichkeiten/programme/einzelfoerderung/sachbeihilfe'
)
CUSTOM_SOURCES = DATA / 'private/custom_sources.json'
BUILTIN_SOURCES = {
	'dfg': {
		'name': 'DFG',
		'url': 'https://www.dfg.de/service/rss/de/323556/feed.rss',
		'coverage': 'Latest 25 official research announcements (including funding news), plus Sachbeihilfe. Not a complete programme catalogue.',
	},
	'bmftr': {
		'name': 'BMFTR · Health research',
		'url': 'https://www.gesundheitsforschung-bmftr.de/',
		'coverage': 'Health-research notices linked from the official homepage and latest annual listing. Partial BMFTR coverage, not all German ministries.',
	},
}
# Built-ins only, kept for existing callers. all_sources() adds the user's feeds.
SOURCES = BUILTIN_SOURCES


def custom_sources() -> dict:
	return _read(CUSTOM_SOURCES, {})


def all_sources() -> dict:
	return {**BUILTIN_SOURCES, **custom_sources()}


def add_source(name: str, url: str) -> dict:
	"""Register an RSS/Atom feed after proving it parses.

	Nothing is persisted until a real fetch returns at least one notice, so a
	typo or a login wall fails loudly here instead of silently producing an
	empty source the watch loop then polls forever.
	"""
	name = (name or '').strip()
	url = (url or '').strip()
	if not name:
		raise ValueError('Give the source a name.')
	parts = urlsplit(url)
	if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
		raise ValueError('Use a plain https feed URL without credentials.')
	if parts.port not in (None, 443):
		raise ValueError('Use the standard https port.')

	provider = f'custom-{hashlib.sha256(url.encode()).hexdigest()[:10]}'
	existing = custom_sources()
	if provider in existing:
		raise ValueError('That feed is already a source.')
	record = {
		'name': name[:60],
		'url': url,
		'host': parts.hostname,
		'kind': 'rss',
		'coverage': 'User-added source. Only what this page publishes, not a complete catalogue.',
		'added_at': datetime.now(UTC).isoformat(),
	}
	with _LOCK:
		# Register before fetching so allowed_url can authorise the host, then
		# roll back if nothing usable comes back.
		_write(CUSTOM_SOURCES, {**existing, provider: record})
		try:
			body = fetch(url, provider)
			# Syndication first: it is exact and costs no model time. A page that
			# is not a feed falls back to reading its links.
			try:
				items = parse_rss(body, provider)
			except ET.ParseError:
				items = []
			if not items:
				record['kind'] = 'html'
				items = parse_html_source(body, url, provider)
		except Exception:
			_write(CUSTOM_SOURCES, existing)
			raise
		if not items:
			_write(CUSTOM_SOURCES, existing)
			raise ValueError('Nothing on that page looked like a funding call.')
		_write(CUSTOM_SOURCES, {**existing, provider: record})
	return {**record, 'id': provider, 'items': items}


def remove_source(provider: str) -> None:
	with _LOCK:
		existing = custom_sources()
		if provider not in existing:
			raise KeyError(provider)
		del existing[provider]
		_write(CUSTOM_SOURCES, existing)


LINK_CANDIDATES = 120
SELECT_NUM_PREDICT = 600


def link_candidates(html: str, base: str, provider: str) -> list[dict]:
	"""Every on-host link on a page, with its anchor text.

	Parsed deterministically. The model that follows only ever *chooses* from
	this list, so it cannot produce a URL the page does not contain.
	"""
	seen, candidates = set(), []
	page = Page(html)
	# A site's mega-menu comes first in document order and used to fill the whole
	# budget before the page body. A page without semantic tags keeps every link.
	for href, text in page.body_links or page.links:
		url = urljoin(base, (href or '').strip())
		label = ' '.join((text or '').split())
		if not label or len(label) < 12 or url in seen or not allowed_url(url, provider):
			continue
		seen.add(url)
		candidates.append({'url': url, 'title': label[:160]})
		if len(candidates) >= LINK_CANDIDATES:
			break
	return candidates


def select_call_links(candidates: list[dict], provider: str) -> list[dict]:
	"""Ask the model which of the page's links are funding announcements.

	It answers with positions in the candidate list, never with text, so an
	invented link is not expressible. Positions out of range are discarded.
	"""
	if not candidates:
		return []
	from grant_recommender import concepts

	listing = '\n'.join(f'{i}: {c["title"]}' for i, c in enumerate(candidates))
	schema = {
		'type': 'object',
		'properties': {
			'calls': {
				'type': 'array',
				'maxItems': 25,
				'items': {'type': 'integer', 'minimum': 0, 'maximum': len(candidates) - 1},
			}
		},
		'required': ['calls'],
		'additionalProperties': False,
	}
	packet = {
		'system_prompt': (
			'You are given the numbered links from one funder web page. Return the '
			'positions of the links that are funding announcements, calls for '
			'proposals or programme descriptions a researcher could apply to. '
			'Leave out navigation, news, events, contact pages, logins and press '
			'releases. Answer with positions only. If none qualify, return an '
			'empty list; a short honest answer beats a padded one.'
		),
		'user_prompt': f'LINKS:\n{listing}',
	}
	parsed, _meta = concepts._ollama_chat(
		{**packet, '_connection': concepts.connection_settings()},
		schema=schema,
		num_predict=SELECT_NUM_PREDICT,
		seed=42,
	)
	chosen = {i for i in parsed.get('calls', []) if 0 <= i < len(candidates)}
	return [notice(provider, candidates[i]['url'], candidates[i]['title']) for i in sorted(chosen)]


def parse_html_source(html: str, base: str, provider: str) -> list[dict]:
	return select_call_links(link_candidates(html, base, provider), provider)


def parse_rss(xml: str, provider: str) -> list[dict]:
	"""RSS and Atom, reduced to what a notice needs. Entries pointing off the
	feed's own host are dropped rather than followed."""
	root = ET.fromstring(xml)
	atom = '{http://www.w3.org/2005/Atom}'
	rows = root.findall('./channel/item') or root.findall(f'./{atom}entry')
	items = []
	for row in rows[:25]:
		link = (row.findtext('link') or '').strip()
		if not link:
			element = row.find(f'{atom}link')
			link = (element.get('href') if element is not None else '') or ''
		title = (row.findtext('title') or row.findtext(f'{atom}title') or '').strip()
		published = row.findtext('pubDate') or row.findtext(f'{atom}updated')
		if link and title and allowed_url(link, provider):
			items.append(notice(provider, link, title, published))
	return items


def allowed_url(url: str, provider: str) -> bool:
	parts = urlsplit(url)
	hosts = {'dfg': {'www.dfg.de', 'dfg.de'}, 'bmftr': {'www.gesundheitsforschung-bmftr.de'}}
	if provider in hosts:
		allowed = hosts[provider]
	else:
		# A user-added source may only reach the host its own feed came from.
		source = custom_sources().get(provider)
		allowed = {source['host']} if source else set()
	return (
		parts.scheme == 'https'
		and parts.hostname in allowed
		and not parts.username
		and not parts.password
		and parts.port in (None, 443)
	)


def fetch(url: str, provider: str) -> str:
	for _ in range(5):
		if not allowed_url(url, provider):
			raise ValueError('Source URL is outside the registered official provider.')
		with requests.get(url, timeout=15, allow_redirects=False, stream=True) as response:
			if response.is_redirect:
				url = urljoin(url, response.headers['Location'])
				continue
			response.raise_for_status()
			chunks, size = [], 0
			for chunk in response.iter_content(65536):
				size += len(chunk)
				if size > 2_000_000:
					raise ValueError('Source exceeds the 2 MB safety limit.')
				chunks.append(chunk)
			return b''.join(chunks).decode('utf-8-sig')
	raise ValueError('Too many source redirects.')


class Page(HTMLParser):
	def __init__(self, html: str):
		super().__init__(convert_charrefs=True)
		self.stack, self.parts, self.links = [], [], []
		# Links outside header, nav, footer and aside: the page body.
		self.body_links = []
		self.anchor = None
		self.feed(html)

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag not in {
			'area',
			'base',
			'br',
			'col',
			'embed',
			'hr',
			'img',
			'input',
			'link',
			'meta',
			'param',
			'source',
			'track',
			'wbr',
		}:
			self.stack.append(tag)
		if tag == 'a':
			self.anchor = {
				'url': attrs.get('href', ''),
				'title': attrs.get('title', ''),
				'parts': [],
				# An article's own <header> inside <main> is content, not site chrome.
				'chrome': bool({'nav', 'footer', 'aside'} & set(self.stack))
				or ('header' in self.stack and not {'main', 'article'} & set(self.stack)),
			}
		if tag == 'br':
			self.parts.append('\n')

	def handle_endtag(self, tag):
		if tag == 'a' and self.anchor:
			a = self.anchor
			self.links.append((a['url'], ' '.join(''.join(a['parts']).split()) or a['title']))
			if not a['chrome']:
				self.body_links.append(self.links[-1])
			self.anchor = None
		if tag in ('p', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'div'):
			self.parts.append('\n')
		if tag in self.stack:
			self.stack = self.stack[: len(self.stack) - 1 - self.stack[::-1].index(tag)]

	def handle_data(self, data):
		if self.anchor:
			self.anchor['parts'].append(data)
		if 'main' in self.stack and not set(self.stack) & {
			'script',
			'style',
			'nav',
			'aside',
			'footer',
		}:
			self.parts.append(data)

	def text(self):
		return '\n'.join(
			line for row in ''.join(self.parts).splitlines() if (line := ' '.join(row.split()))
		)


def notice(provider: str, url: str, title: str, published: str | None = None) -> dict:
	if title.startswith("Gesamten Artikel '") and title.endswith("' anzeigen"):
		title = title[len("Gesamten Artikel '") : -len("' anzeigen")]
	return {
		'id': f'{provider.upper()}-{hashlib.sha256(url.encode()).hexdigest()[:12]}',
		'provider': provider,
		'url': url,
		'title': title,
		'published': published,
	}


def parse_dfg(xml: str) -> list[dict]:
	root = ET.fromstring(xml)
	items = []
	for row in root.findall('./channel/item')[:25]:
		url = row.findtext('link', '').strip()
		if allowed_url(url, 'dfg') and '/info-wissenschaft/' in url:
			items.append(
				notice('dfg', url, row.findtext('title', '').strip(), row.findtext('pubDate'))
			)
	if not items:
		raise ValueError('DFG feed contained no recognised announcements.')
	return [
		notice('dfg', DFG_PROGRAMME, 'Sachbeihilfe · Research Grants (individual programme)'),
		*items,
	]


def parse_bmftr(html: str, base: str) -> list[dict]:
	items = {}
	for href, title in Page(html).links:
		url = urljoin(base, href)
		path = urlsplit(url).path
		is_notice = '/de/richtlinie-' in path or (
			re.fullmatch(r'/de/\d+\.php', path) and 'richtlinie' in title.lower()
		)
		if allowed_url(url, 'bmftr') and is_notice and len(title) > 25:
			items.setdefault(url, notice('bmftr', url, title))
	return list(items.values())


def _read(path: Path, default):
	return json.loads(path.read_text()) if path.exists() else default


def _write(path: Path, value):
	path.parent.mkdir(parents=True, exist_ok=True)
	temp = path.with_suffix('.tmp')
	temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
	temp.replace(path)


def source_status(provider: str) -> dict:
	return {
		**all_sources()[provider],
		'id': provider,
		**_read(CACHE / f'{provider}.json', {'items': [], 'fetched_at': None, 'error': None}),
	}


def refresh(provider: str) -> dict:
	with _LOCK:
		previous = source_status(provider)
		try:
			url = all_sources()[provider]['url']
			html = fetch(url, provider)
			if provider == 'dfg':
				items = parse_dfg(html)
			elif provider in custom_sources():
				source = custom_sources()[provider]
				if source.get('kind') == 'html':
					items = parse_html_source(html, source['url'], provider)
				else:
					items = parse_rss(html, provider)
			else:
				items = parse_bmftr(html, url)
				index = 'https://www.gesundheitsforschung-bmftr.de/bekanntmachungen-5786.php'
				links = Page(fetch(index, provider)).links
				annual = next(
					(
						urljoin(index, href)
						for href, _ in links
						if f'/bekanntmachungen-{datetime.now(UTC).year}-' in href
					),
					None,
				)
				if annual:
					items += parse_bmftr(fetch(annual, provider), annual)
				items = list({item['id']: item for item in items}.values())[:25]
			if not items:
				raise ValueError('No recognised notices; the source layout may have changed.')
			result = {'items': items, 'fetched_at': datetime.now(UTC).isoformat(), 'error': None}
		except (requests.RequestException, ValueError, ET.ParseError) as exc:
			result = {
				**previous,
				'error': f'Could not refresh official source: {exc}. Previously fetched notices, if any, are retained.',
			}
		_write(CACHE / f'{provider}.json', result)
		return source_status(provider)


def watched_calls() -> list[dict]:
	return list(_read(WATCHLIST, {}).values())


def get_call(code: str) -> dict | None:
	return _read(WATCHLIST, {}).get(code)


def import_notice(provider: str, notice_id: str) -> dict:
	with _LOCK:
		item = next((i for i in source_status(provider)['items'] if i['id'] == notice_id), None)
		if not item:
			raise KeyError('Notice not found. Refresh the source first.')
		text = Page(fetch(item['url'], provider)).text()
		if len(text) < 150 or len(text) > 100_000:
			raise ValueError('Could not extract a complete, bounded official source body.')
		call = {
			'topic_code': notice_id,
			'title': item['title'],
			'call_identifier': all_sources()[provider]['name'],
			'call_title': item['title'],
			'deadlines': [],
			'status': 'official_notice_unreviewed',
			'types_of_action': [],
			'scope_text': text,
			'topic_conditions': 'Review the complete official source and linked programme rules. Eligibility and deadlines have not been extracted automatically.',
			'framework_programme': all_sources()[provider]['name'],
			'programme_period': None,
			'source_url': item['url'],
			'fetched_at': datetime.now(UTC).isoformat(),
			'provider': provider,
			'country': 'DE',
			'source_warning': 'Imported official announcement/programme text, not a verified open call. Check deadlines, applicant requirements and linked rules at the source. Snapshot refreshes when you re-import.',
		}
		calls = _read(WATCHLIST, {})
		calls[notice_id] = call
		_write(WATCHLIST, calls)
		return call
