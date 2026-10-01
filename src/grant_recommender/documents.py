"""Find and fetch the files a call hangs off its page: templates, forms,
guidelines, annexes.

Discovery is deterministic. The links are parsed out of the page and filtered
by extension and by the same host allowlist every other fetch obeys, so there
is no step here that could invent a file. Naming what each document *is* is a
guess from its filename and link text, and is labelled as such.

Downloads are explicit, one call at a time, size-capped, and land under
data/private/call_documents/<topic_code>/. Nothing is fetched merely because a
call was selected.
"""

import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests

from grant_recommender.german_funding import Page, allowed_url

ROOT = Path(__file__).resolve().parents[2] / 'data/private/call_documents'
MAX_BYTES = 25 * 1024 * 1024
MAX_DOCUMENTS = 40
TIMEOUT_S = 60

EXTENSIONS = {
	'.pdf': 'PDF',
	'.doc': 'Word',
	'.docx': 'Word',
	'.rtf': 'Rich text',
	'.odt': 'OpenDocument',
	'.xls': 'Spreadsheet',
	'.xlsx': 'Spreadsheet',
	'.zip': 'Archive',
}

# Filename and link-text hints. A guess about what a file is, never a claim.
KINDS = (
	('template', ('vorlage', 'muster', 'template', 'formular', 'form')),
	('guidance', ('leitfaden', 'hinweis', 'guide', 'guidance', 'faq', 'merkblatt')),
	('call text', ('bekanntmachung', 'richtlinie', 'ausschreibung', 'call')),
	('annex', ('anlage', 'anhang', 'annex', 'appendix')),
)


def classify(name: str, label: str) -> str:
	haystack = f'{name} {label}'.lower()
	for kind, needles in KINDS:
		if any(needle in haystack for needle in needles):
			return kind
	return 'document'


def filename_of(url: str) -> str:
	return unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'download'


def discover(call: dict, html: str) -> list[dict]:
	"""Every document linked from the call page, on the call's own host."""
	provider = call.get('provider') or 'eu'
	base = call.get('source_url') or ''
	found, seen = [], set()
	for href, label in Page(html).links:
		url = urljoin(base, (href or '').strip())
		name = filename_of(url)
		extension = Path(name).suffix.lower()
		if extension not in EXTENSIONS or url in seen:
			continue
		if not allowed_url(url, provider):
			continue
		seen.add(url)
		found.append(
			{
				'url': url,
				'filename': name,
				'format': EXTENSIONS[extension],
				'label': ' '.join((label or '').split())[:120],
				'kind': classify(name, label or ''),
			}
		)
		if len(found) >= MAX_DOCUMENTS:
			break
	return found


def _target(topic_code: str, filename: str) -> Path:
	safe_code = re.sub(r'[^A-Za-z0-9._-]', '_', topic_code)[:120]
	safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename)[:160] or 'download'
	return ROOT / safe_code / safe_name


def stored(call: dict) -> list[Path]:
	"""The documents actually saved for this call.

	A download writes to a `.part` file first, so an interrupted fetch leaves a
	stub behind. Counting those made the dossier claim files it did not have.
	"""
	folder = _target(call['topic_code'], 'x').parent
	if not folder.exists():
		return []
	return sorted(p for p in folder.glob('*') if p.is_file() and p.suffix != '.part')


def download(call: dict, document: dict) -> Path:
	"""Fetch one document. Refuses anything off-host or over the size cap."""
	provider = call.get('provider') or 'eu'
	url = document['url']
	if not allowed_url(url, provider):
		raise ValueError("That file is not on this source's host.")

	target = _target(call['topic_code'], document['filename'])
	if target.exists():
		return target
	target.parent.mkdir(parents=True, exist_ok=True)

	with requests.get(url, timeout=TIMEOUT_S, stream=True, allow_redirects=False) as response:
		if 300 <= response.status_code < 400:
			raise ValueError('The file redirected; not following it.')
		response.raise_for_status()
		declared = int(response.headers.get('content-length') or 0)
		if declared > MAX_BYTES:
			raise ValueError(f'File is {declared // 1024 // 1024} MB, over the cap.')
		written = 0
		temp = target.with_suffix(target.suffix + '.part')
		with temp.open('wb') as handle:
			for chunk in response.iter_content(64 * 1024):
				written += len(chunk)
				if written > MAX_BYTES:
					temp.unlink(missing_ok=True)
					raise ValueError('File exceeded the size cap while downloading.')
				handle.write(chunk)
		temp.replace(target)
	return target
