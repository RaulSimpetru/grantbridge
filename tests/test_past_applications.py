import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from grant_recommender import api
from grant_recommender import past_applications as archive


@pytest.fixture
def prepared(tmp_path, monkeypatch):
	root = tmp_path / 'archive'
	root.mkdir()
	monkeypatch.setattr(archive, 'ARCHIVE', root)
	monkeypatch.setattr(archive, 'LIBRARY', tmp_path / 'private' / 'library.json')
	records = []
	for i, year in enumerate(('2001', '2002', '2002')):
		original = f'original-{i}.docx'
		(root / original).write_bytes(b'fictional original')
		(root / f'text-{i}.txt').write_text('Proposed equipment; another applicant.')
		records.append(
			{
				'key': f'batch/doc-{i}',
				'batch': 'batch',
				'archive_path': f'Example_{year}_B2_FINAL.docx',
				'original': original,
				'text': f'text-{i}.txt',
				'sha256': hashlib.sha256(b'fictional original').hexdigest(),
				'characters': 42,
				'status': 'extracted',
				'warnings': [],
				'bytes': 18,
				'source_url': 'https://example.org/archive',
				'exact_duplicate_of': 'batch/doc-1' if i == 2 else None,
			}
		)
	(root / 'ocr.txt').write_text('Unverified recognition')
	records[2]['ocr_text'] = 'ocr.txt'
	records[2]['characters'] = 0
	(root / 'manifest.json').write_text(
		json.dumps({'records': records, 'limitations': ['No outcomes verified.']})
	)
	(root / 'application-catalog.json').write_text(
		json.dumps(
			{
				'applications': [
					{'id': year, 'family': 'Example', 'title': f'Example {year}'}
					for year in ('2001', '2002')
				],
				'batches': {'batch': '2002'},
				'assignments': {'batch/doc-0': '2001'},
				'warnings': {'batch/doc-2': 'Title does not match the catalog entry'},
			}
		)
	)
	return root


def test_import_is_offline_private_and_idempotent(prepared, monkeypatch):
	def forbidden(*args, **kwargs):
		raise AssertionError('Import must not contact a model or alter a profile')

	monkeypatch.setattr(api, 'add_assertion', forbidden)
	monkeypatch.setattr(api.concepts, 'connection_status', forbidden)
	assert archive.library()['imported_at'] is None
	result = archive.import_archive()
	assert len(result['applications']) == 2  # years are not collapsed
	assert len(result['documents']) == 3
	assert result['unique_files'] == 1
	assert all(
		a['ownership'] == 'unclassified'
		and a['outcome'] == 'unknown'
		and a['submission'] == 'unknown'
		for a in result['applications']
	)
	assert all(
		'original' not in d and 'text' not in d and 'ocr_text' not in d for d in result['documents']
	)
	assert 'fictional original' not in json.dumps(result)
	archive.review('2002', archive.Review(ownership='reference', submission='submitted'))
	again = archive.import_archive()
	assert again['applications'][1]['ownership'] == 'reference'
	assert again['applications'][1]['outcome'] == 'unknown'
	assert again['imported_at'] == result['imported_at']


def test_preferred_version_is_validated_and_non_destructive(prepared):
	data = archive.import_archive()
	a, b, c = data['documents']
	with pytest.raises(ValueError):
		archive.review('2002', archive.Review(preferred={'Part B2': a['id']}))
	with pytest.raises(ValueError):
		archive.review('2002', archive.Review(preferred={'wrong group': b['id']}))
	updated = archive.review('2002', archive.Review(preferred={'Part B2': c['id']}))
	assert updated['preferred']['Part B2'] == c['id']
	assert updated['outcome'] == 'unknown'
	assert len(archive.library()['documents']) == 3
	assert len(list(prepared.glob('original*'))) == 3


@pytest.mark.parametrize('mutation', ['traversal', 'symlink', 'hash', 'missing'])
def test_import_rejects_invalid_sources_atomically(prepared, mutation):
	manifest = json.loads((prepared / 'manifest.json').read_text())
	if mutation in {'traversal', 'symlink'}:
		outside = prepared.parent / 'outside.docx'
		outside.write_bytes(b'fictional original')
		if mutation == 'symlink':
			(prepared / 'link.docx').symlink_to(outside)
		manifest['records'][0]['original'] = (
			'../outside.docx' if mutation == 'traversal' else 'link.docx'
		)
	elif mutation == 'hash':
		(prepared / 'original-0.docx').write_bytes(b'changed original')
	else:
		manifest['records'][0]['text'] = 'missing.txt'
	(prepared / 'manifest.json').write_text(json.dumps(manifest))
	with pytest.raises(ValueError):
		archive.import_archive()
	assert not archive.LIBRARY.exists()


def test_document_ids_originals_and_ocr(prepared):
	data = archive.import_archive()
	client = TestClient(api.app)
	doc = data['documents'][2]
	assert doc['has_ocr'] and doc['identity_warning']
	response = client.get(f'/past-application-documents/{doc["id"]}/original')
	assert response.content == b'fictional original'
	assert response.headers['cache-control'] == 'no-store'
	assert response.headers['content-disposition'].startswith('attachment')
	assert (
		client.get(f'/past-application-documents/{doc["id"]}/ocr').json()['text']
		== 'Unverified recognition'
	)
	assert client.get('/past-application-documents/not-a-document/text').status_code == 404
	assert client.get(f'/past-application-documents/{doc["id"]}/invalid').status_code == 422
	(prepared / 'original-2.docx').write_bytes(b'changed')
	assert client.get(f'/past-application-documents/{doc["id"]}/original').status_code == 422


def test_review_api_validation_and_durable_readback(prepared):
	client = TestClient(api.app)
	assert client.post('/past-applications/import').status_code == 200
	assert (
		client.patch(
			'/past-applications/2002',
			json={'ownership': 'mine', 'outcome': 'pending', 'note': 'User-reviewed'},
		).status_code
		== 200
	)
	assert client.get('/past-applications').json()['applications'][1]['note'] == 'User-reviewed'
	for invalid in (
		{'ownership': 'other'},
		{'ownership': None},
		{'path': '/tmp'},
		{'note': 'x' * 4001},
	):
		assert client.patch('/past-applications/2002', json=invalid).status_code == 422
	assert client.patch('/past-applications/unknown', json={'ownership': 'mine'}).status_code == 404


@pytest.mark.parametrize(
	'headers',
	[
		{'origin': 'https://untrusted.example'},
		{'host': 'untrusted.example'},
		{'sec-fetch-site': 'cross-site'},
	],
)
def test_rejects_untrusted_browser_access(prepared, headers):
	client = TestClient(api.app)
	assert client.get('/past-applications', headers=headers).status_code == 403
	assert client.post('/past-applications/import', headers=headers).status_code == 403


def test_safe_import_error_does_not_disclose_paths(prepared):
	(prepared / 'manifest.json').write_text('{broken')
	response = TestClient(api.app).post('/past-applications/import')
	assert response.status_code == 422
	assert str(prepared) not in response.text
