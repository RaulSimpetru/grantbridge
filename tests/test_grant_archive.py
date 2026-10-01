import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
	'grant_archive', Path(__file__).resolve().parents[1] / 'scripts/extract_grant_archive.py'
)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def make_zip(path, files):
	with zipfile.ZipFile(path, 'w') as z:
		for name, content in files.items():
			z.writestr(name, content)


def test_private_inventory_duplicates_nested_files_and_unsafe_paths(tmp_path, monkeypatch):
	monkeypatch.setattr(archive, 'PRIVATE', tmp_path / 'private')
	nested = io.BytesIO()
	make_zip(nested, {'nested.txt': 'An old proposal'})
	source = tmp_path / 'source.zip'
	make_zip(
		source,
		{
			'folder/first.txt': 'An old proposal',
			'copy.txt': 'An old proposal',
			'../escape.txt': 'Unsafe',
			'nested.zip': nested.getvalue(),
			'image.png': b'not text',
		},
	)
	result = archive.extract(source, tmp_path / 'private/import', 'https://example.org/archive')
	assert result['complete_inventory'] is True
	assert len(result['records']) == 6
	assert result['records'][1]['duplicate_of'] == 'doc-0001'
	assert result['records'][2]['status'] == 'skipped_unsafe_or_oversized'
	assert result['records'][4]['archive_path'] == 'nested.zip!/nested.txt'
	assert not (tmp_path / 'escape.txt').exists()
	assert (tmp_path / 'private/import/source.zip').read_bytes() == source.read_bytes()
	assert all(r['outcome'] == 'unknown' for r in result['records'])


def test_refuses_public_destination_and_overwrite(tmp_path, monkeypatch):
	monkeypatch.setattr(archive, 'PRIVATE', tmp_path / 'private')
	source = tmp_path / 'source.zip'
	make_zip(source, {'file.txt': 'text'})
	with pytest.raises(ValueError, match='data/private'):
		archive.extract(source, tmp_path / 'public', 'https://example.org/archive')
	archive.extract(source, tmp_path / 'private/import', 'https://example.org/archive')
	with pytest.raises(FileExistsError):
		archive.extract(source, tmp_path / 'private/import', 'https://example.org/archive')


def test_nested_expansion_limit_marks_inventory_incomplete(tmp_path, monkeypatch):
	monkeypatch.setattr(archive, 'PRIVATE', tmp_path / 'private')
	monkeypatch.setattr(archive, 'MAX_ENTRIES', 1)
	nested = io.BytesIO()
	make_zip(nested, {'inner.txt': 'text'})
	source = tmp_path / 'source.zip'
	make_zip(source, {'nested.zip': nested.getvalue()})
	result = archive.extract(source, tmp_path / 'private/import', 'https://example.org/archive')
	assert result['complete_inventory'] is False
	assert 'limit' in result['error']


def test_docx_preserves_paragraphs_and_flags_changes_without_deleted_text():
	doc = io.BytesIO()
	make_zip(
		doc,
		{
			'word/document.xml': f'<w:document xmlns:w="{archive.W[1:-1]}"><w:p><w:r><w:t>Existing</w:t></w:r><w:del><w:r><w:delText>Deleted</w:delText></w:r></w:del><w:ins><w:r><w:t> proposed</w:t></w:r></w:ins></w:p><w:p><w:r><w:t>Next paragraph</w:t></w:r></w:p></w:document>'
		},
	)
	text, warnings = archive.docx_text(doc.getvalue())
	assert 'Existing proposed\nNext paragraph' in text
	assert 'Deleted' not in text
	assert warnings


def test_writes_an_archive_that_past_applications_imports(tmp_path, monkeypatch):
	"""The script used to write only inventory.json, so Past applications had
	nothing in the repo that could fill it."""
	from grant_recommender import past_applications

	private = tmp_path / 'private'
	monkeypatch.setattr(archive, 'PRIVATE', private)
	source = tmp_path / 'applications.zip'
	make_zip(
		source,
		{
			'Example_B2_final.txt': 'Fictional research narrative.',
			'budget.md': 'Fictional budget.',
			'copy/budget.md': 'Fictional budget.',
			'figure.png': b'\x89PNG not text',
		},
	)
	output = private / 'archive'
	archive.extract(source, output, 'https://example.org/applications.zip')

	monkeypatch.setattr(past_applications, 'ARCHIVE', output)
	monkeypatch.setattr(past_applications, 'LIBRARY', private / 'past-applications.json')
	library = past_applications.import_archive()

	assert [a['title'] for a in library['applications']] == ['applications']
	docs = {d['name']: d for d in library['documents']}
	# The PNG has no text to extract, so it stays in inventory.json only.
	assert set(docs) == {'Example_B2_final.txt', 'budget.md', 'copy/budget.md'}
	assert all(d['has_text'] for d in docs.values())
	assert docs['Example_B2_final.txt']['group'] == 'Part B2'
	assert docs['copy/budget.md']['exact_duplicate_of'] == docs['budget.md']['key']
	assert library['limitations'] and all(isinstance(x, str) for x in library['limitations'])


def test_refuses_a_source_url_the_importer_would_reject(tmp_path):
	with pytest.raises(ValueError, match='https'):
		archive.extract(tmp_path / 'x.zip', tmp_path / 'out', 'http://example.org/x.zip')
