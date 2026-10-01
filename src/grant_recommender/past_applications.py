"""Offline, single-user library of past applications you have the right to use,
stored locally. Never calls a model or the portfolio. No archive ships with
GrantBridge.

Only the operator-configured archive is importable: GRANTBRIDGE_ARCHIVE_DIR,
default data/private/archive, holding manifest.json and application-catalog.json.
Client requests use opaque IDs, never filesystem paths. All source content and
review state remain private.
"""

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PRIVATE = Path(__file__).resolve().parents[2] / 'data' / 'private'
# A setting, not a request field: clients can never point the import elsewhere.
# `or`, not a default: an empty variable would otherwise make the cwd the archive.
ARCHIVE = Path(os.environ.get('GRANTBRIDGE_ARCHIVE_DIR') or PRIVATE / 'archive').expanduser()
LIBRARY = PRIVATE / 'past-applications.json'
LOCK = threading.RLock()


class Review(BaseModel):
	model_config = ConfigDict(extra='forbid')
	ownership: Literal['unclassified', 'mine', 'lab', 'reference', 'template'] | None = None
	submission: Literal['unknown', 'draft', 'submitted'] | None = None
	outcome: Literal['unknown', 'pending', 'funded', 'rejected'] | None = None
	preferred: dict[str, str] | None = None
	note: str | None = Field(default=None, max_length=4000)


def _path(relative: str) -> Path:
	root = ARCHIVE.resolve()
	path = (root / relative).resolve()
	if not path.is_relative_to(root) or not path.is_file():
		raise ValueError('Archive file is unavailable or outside the configured archive.')
	return path


def _read() -> dict:
	if not LIBRARY.exists():
		return {'applications': [], 'documents': [], 'imported_at': None, 'limitations': []}
	return json.loads(LIBRARY.read_text())


def _save(data: dict):
	LIBRARY.parent.mkdir(parents=True, exist_ok=True)
	temp = LIBRARY.with_suffix('.tmp')
	temp.write_text(json.dumps(data, indent=2))
	temp.replace(LIBRARY)


def _group(name: str) -> str:
	"""Conservative suggested document groups, not verified version chronology."""
	name = name.lower()
	if re.search(r'[_\s]b1[_\s.]', name):
		return 'Part B1'
	if re.search(r'[_\s]b2[_\s.]', name):
		return 'Part B2'
	if any(word in name for word in ('curriculum', 'cv_', 'publication')):
		return 'CV and publication list'
	if any(word in name for word in ('consent', 'einwilligung')):
		return 'Consent documents'
	return Path(name).stem


def import_archive() -> dict:
	with LOCK:
		# Explicit, idempotent import. No arbitrary request-supplied source path.
		if _read()['imported_at']:
			return library()
		manifest = json.loads(_path('manifest.json').read_text())
		catalog = json.loads(_path('application-catalog.json').read_text())
		documents = []
		applications = []
		for item in catalog['applications']:
			applications.append(
				{
					**{
						k: item.get(k, 'Unknown')
						for k in (
							'id',
							'family',
							'title',
							'funder',
							'programme',
							'year',
							'applicant',
							'summary',
							'source_key',
						)
					},
					'ownership': 'unclassified',
					'submission': 'unknown',
					'outcome': 'unknown',
					'preferred': {},
					'note': '',
					'reviewed_at': None,
				}
			)
		app_ids = {a['id'] for a in applications}
		if len(app_ids) != len(applications):
			raise ValueError('Duplicate application identifiers in catalog.')
		for record in manifest['records']:
			key = record['key']
			app_id = catalog.get('assignments', {}).get(key) or catalog.get('batches', {}).get(
				record['batch']
			)
			if app_id not in app_ids:
				raise ValueError('Every document requires a catalog application assignment.')
			original = _path(record['original'])
			if hashlib.sha256(original.read_bytes()).hexdigest() != record['sha256']:
				raise ValueError(
					'Original integrity check failed. Recheck the archive before importing.'
				)
			for field in ('text', 'ocr_text'):
				if record.get(field):
					_path(record[field])
			source_url = record.get('source_url', '')
			if not source_url.startswith('https://'):
				raise ValueError('Invalid archive source URL.')
			documents.append(
				{
					**record,
					'id': hashlib.sha256(key.encode()).hexdigest()[:24],
					'application_id': app_id,
					'name': record['archive_path'],
					'group': catalog.get('document_groups', {}).get(
						key, _group(record['archive_path'])
					),
					'identity_warning': catalog.get('warnings', {}).get(key),
				}
			)
		if len({d['id'] for d in documents}) != len(documents):
			raise ValueError('Duplicate document identifiers in archive.')
		_save(
			{
				'applications': applications,
				'documents': documents,
				'imported_at': datetime.now(UTC).isoformat(),
				'limitations': manifest.get('limitations', []),
			}
		)
		return library()


def library() -> dict:
	data = _read()
	# Never expose server paths. Text is fetched only when a document is opened.
	fields = (
		'id',
		'key',
		'application_id',
		'name',
		'group',
		'status',
		'sha256',
		'bytes',
		'warnings',
		'source_url',
		'exact_duplicate_of',
		'same_extracted_text_as',
		'identity_warning',
	)
	docs = [
		{
			**{k: d.get(k) for k in fields},
			'has_text': bool(d.get('characters')),
			'has_ocr': bool(d.get('ocr_text')),
		}
		for d in data['documents']
	]
	return {**data, 'documents': docs, 'unique_files': len({d['sha256'] for d in docs})}


def review(application_id: str, body: Review) -> dict:
	with LOCK:
		data = _read()
		application = next((a for a in data['applications'] if a['id'] == application_id), None)
		if application is None:
			raise KeyError('Application not found.')
		fields = body.model_dump(exclude_unset=True)
		if any(v is None for v in fields.values()):
			raise ValueError('Review fields cannot be null.')
		for group, doc_id in fields.get('preferred', {}).items():
			if not any(
				d['id'] == doc_id and d['application_id'] == application_id and d['group'] == group
				for d in data['documents']
			):
				raise ValueError(
					'Preferred version must belong to this application and document group.'
				)
		application.update(fields)
		application['reviewed_at'] = datetime.now(UTC).isoformat()
		_save(data)
		return application


def document(doc_id: str, kind: Literal['original', 'text', 'ocr']) -> tuple[Path, str]:
	record = next((d for d in _read()['documents'] if d['id'] == doc_id), None)
	if record is None:
		raise KeyError('Document not found.')
	field = 'ocr_text' if kind == 'ocr' else kind
	if not record.get(field):
		raise KeyError('This text version is unavailable.')
	path = _path(record[field])
	if kind == 'original' and hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
		raise ValueError('Original integrity check failed.')
	return path, Path(record['name']).name
