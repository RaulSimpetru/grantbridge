"""Offline inventory and text extraction for a ZIP of past applications you have
the right to use, kept locally. No model calls.

    uv run python scripts/extract_grant_archive.py applications.zip data/private/archive \
        --source-url https://example.org/where-the-zip-came-from

Writes inventory.json (every entry, with its status) plus the manifest.json and
application-catalog.json that Past applications imports from
GRANTBRIDGE_ARCHIVE_DIR (default data/private/archive). PDFs need pdftotext
(poppler); .doc and .rtf need macOS textutil.

Preserves the source ZIP; never executes its contents, follows embedded
links, modifies source documents, or imports claims into the live profile.
Output must be a new directory under this repository's ignored data/private.
"""

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

PRIVATE = Path(__file__).resolve().parents[1] / 'data/private'
MAX_FILE = 100 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ENTRIES = 10000
TEXT_TYPES = {'.pdf', '.docx', '.doc', '.rtf', '.txt', '.md', '.tex', '.csv'}
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


class ArchiveLimitError(Exception):
	pass


def safe_name(name):
	path = PurePosixPath(name.replace('\\', '/'))
	return (
		bool(path.parts)
		and not path.is_absolute()
		and '..' not in path.parts
		and ':' not in path.parts[0]
	)


def docx_text(data):
	parts = []
	warnings = []
	with zipfile.ZipFile(io.BytesIO(data)) as document:
		for name in document.namelist():
			if name != 'word/document.xml' and not (
				name.startswith(
					(
						'word/header',
						'word/footer',
						'word/footnotes',
						'word/endnotes',
						'word/comments',
					)
				)
				and name.endswith('.xml')
			):
				continue
			if document.getinfo(name).file_size > MAX_FILE:
				raise ValueError('Oversized document XML')
			root = ET.fromstring(document.read(name))
			if root.find('.//' + W + 'del') is not None or root.find('.//' + W + 'ins') is not None:
				warnings.append(
					'Tracked changes present; extracted body omits deleted text and includes insertions. Review original.'
				)
			lines = []
			for paragraph in root.iter(W + 'p'):
				line = ''.join(
					e.text or '' if e.tag == W + 't' else '\t' if e.tag == W + 'tab' else '\n'
					for e in paragraph.iter()
					if e.tag in (W + 't', W + 'tab', W + 'br')
				)
				if line.strip():
					lines.append(line)
			parts.append(f'--- {name} ---\n' + '\n'.join(lines))
	return '\n\n'.join(parts), warnings


def extract_text(path, data):
	ext = path.suffix.lower()
	if ext == '.docx':
		return docx_text(data)
	if ext == '.pdf':
		result = subprocess.run(
			['pdftotext', '-layout', str(path), '-'], capture_output=True, timeout=90, check=False
		)
	elif ext in ('.doc', '.rtf'):
		result = subprocess.run(
			['textutil', '-convert', 'txt', '-stdout', str(path)],
			capture_output=True,
			timeout=90,
			check=False,
		)
	else:
		return data.decode('utf-8-sig'), []
	if result.returncode:
		raise ValueError(result.stderr.decode(errors='replace')[:500])
	return result.stdout.decode('utf-8', errors='replace'), []


def extract(archive_path, output, source_url):
	# The importer rejects anything else, so fail before doing the work.
	if not source_url.startswith('https://'):
		raise ValueError('--source-url must be an https:// address')
	output = output.resolve()
	if not output.is_relative_to(PRIVATE.resolve()) or output == PRIVATE.resolve():
		raise ValueError('Choose a new subdirectory of data/private')
	# Reading the central directory first prevents mistaking a truncated file
	# for a complete archive. No overwrite/resume semantics.
	with zipfile.ZipFile(archive_path) as archive:
		archive.infolist()
	output.mkdir(parents=True, exist_ok=False)
	shutil.copy2(archive_path, output / 'source.zip')
	(output / 'originals').mkdir()
	(output / 'text').mkdir()
	records, hashes = [], {}
	budget = {'bytes': 0, 'entries': 0}

	def visit(archive, prefix='', depth=0):
		for info in archive.infolist():
			if info.is_dir():
				continue
			budget['entries'] += 1
			budget['bytes'] += info.file_size
			if budget['entries'] > MAX_ENTRIES or budget['bytes'] > MAX_TOTAL:
				raise ArchiveLimitError('Archive expansion limit reached')
			record = {
				'id': f'doc-{len(records) + 1:04d}',
				'archive_path': prefix + info.filename,
				'bytes': info.file_size,
				'archive_modified': list(info.date_time),
				'outcome': 'unknown',
				'status': 'inventory_only',
			}
			records.append(record)
			if not info.filename or not safe_name(info.filename) or info.file_size > MAX_FILE:
				record['status'] = 'skipped_unsafe_or_oversized'
				continue
			try:
				data = archive.read(info)
				digest = hashlib.sha256(data).hexdigest()
				record['sha256'] = digest
				if digest in hashes:
					record['duplicate_of'] = hashes[digest]
				else:
					hashes[digest] = record['id']
				ext = PurePosixPath(info.filename).suffix.lower()
				if ext == '.zip':
					if depth < 2:
						with zipfile.ZipFile(io.BytesIO(data)) as nested:
							visit(nested, prefix + info.filename + '!/', depth + 1)
						record['status'] = 'nested_archive_indexed'
					else:
						record['status'] = 'nested_archive_depth_limit'
					continue
				if ext not in TEXT_TYPES:
					continue
				original = output / 'originals' / (record['id'] + ext)
				original.write_bytes(data)
				record['original'] = str(original.relative_to(output))
				content, warnings = extract_text(original, data)
				target = output / 'text' / (record['id'] + '.txt')
				target.write_text(content)
				record.update(
					text=str(target.relative_to(output)),
					characters=len(content),
					status='extracted' if content.strip() else 'empty_text_needs_review',
					warnings=warnings,
				)
			except (
				ValueError,
				OSError,
				RuntimeError,
				zipfile.BadZipFile,
				ET.ParseError,
				subprocess.TimeoutExpired,
			) as exc:
				record.update(status='extraction_error', error=str(exc)[:500])

	error = None
	try:
		with zipfile.ZipFile(output / 'source.zip') as archive:
			visit(archive)
	except ArchiveLimitError as exc:
		error = str(exc)
	manifest = {
		'source_url': source_url,
		'extracted_at': datetime.now(UTC).isoformat(),
		'complete_inventory': error is None,
		'error': error,
		'privacy': 'Local private archive. No LLM upload or portfolio import.',
		'limitations': 'Text extraction only; no OCR or layout validation. File names and dates do not establish submission or funding outcomes. Linked or externally hosted content is not archived.',
		'records': records,
	}
	(output / 'inventory.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
	# What past_applications.import_archive reads: every document that kept an
	# original, all filed under one application named after the ZIP.
	(output / 'manifest.json').write_text(
		json.dumps(
			{
				'records': [
					{
						**record,
						'key': record['id'],
						'batch': 'archive',
						'source_url': source_url,
						'warnings': record.get('warnings', []),
						'exact_duplicate_of': record.get('duplicate_of'),
					}
					for record in records
					if record.get('original')
				],
				'limitations': [manifest['limitations']],
			},
			ensure_ascii=False,
			indent=2,
		)
	)
	(output / 'application-catalog.json').write_text(
		json.dumps(
			{
				'applications': [{'id': 'archive', 'title': Path(archive_path).stem}],
				'batches': {'archive': 'archive'},
			},
			ensure_ascii=False,
			indent=2,
		)
	)
	print(
		json.dumps(
			{
				'output': str(output),
				'entries': len(records),
				'statuses': dict(Counter(r['status'] for r in records)),
				'error': error,
			}
		)
	)
	return manifest


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('archive', type=Path)
	parser.add_argument('output', type=Path)
	parser.add_argument('--source-url', required=True)
	args = parser.parse_args()
	extract(args.archive, args.output, args.source_url)
