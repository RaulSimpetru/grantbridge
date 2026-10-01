import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_concepts import (
	CALL,
	PORTFOLIO,
	_fake_concept_set,
	_fake_critique,
	_FakeResponse,
	_wait_for,
)
from test_past_applications import prepared  # noqa: F401 -- shared pytest fixture

from grant_recommender import api, concepts, eligibility, past_applications, portfolio
from grant_recommender import archive_rag as rag


@pytest.fixture
def indexed(prepared, monkeypatch, tmp_path):  # noqa: F811 -- injected shared pytest fixture
	monkeypatch.setattr(rag, 'ROOT', tmp_path / 'rag')
	monkeypatch.setattr(concepts, 'JOBS_DIR', tmp_path / 'jobs')
	monkeypatch.setattr(concepts, 'JOBS', {})
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'profile.json')
	monkeypatch.setattr(eligibility, 'REVIEW_PATH', tmp_path / 'reviews.json')
	monkeypatch.setattr(api, 'get_topic', lambda code: CALL)
	monkeypatch.setattr(api, 'load_portfolio', lambda: PORTFOLIO)
	past_applications.import_archive()
	rag.build_index()
	return prepared


def search_request(**kwargs):
	return rag.Search(query='equipment', application_ids=['2001', '2002'], **kwargs)


def test_index_and_search_are_offline_deduplicated_and_source_linked(indexed, monkeypatch):
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: pytest.fail('No upload'))
	monkeypatch.setattr(concepts.requests, 'get', lambda *a, **k: pytest.fail('No network'))
	assert rag.status()['documents_indexed'] == 3
	result = rag.search(search_request())
	assert len(result['hits']) == 1
	hit = result['hits'][0]
	assert len(hit['duplicate_sources']) == 1
	assert hit['line_start'] == 1 and hit['kind'] == 'text'
	path, _ = past_applications.document(hit['document_id'], 'text')
	assert path.read_text()[hit['start'] : hit['end']] == hit['text']
	assert 'original-' not in json.dumps(result)
	assert not portfolio.PORTFOLIO_PATH.exists()


def test_ocr_and_application_filters(indexed):
	assert not rag.search(rag.Search(query='zxqvfoobar', application_ids=['2001']))['hits']
	assert not rag.search(rag.Search(query='recognition', application_ids=['2002']))['hits']
	ocr = rag.search(rag.Search(query='recognition', application_ids=['2002'], include_ocr=True))[
		'hits'
	]
	assert len(ocr) == 1 and ocr[0]['kind'] == 'ocr'
	assert ocr[0]['warnings'] == ['Title does not match the catalog entry']
	assert not rag.search(
		rag.Search(query='recognition', application_ids=['2001'], include_ocr=True)
	)['hits']
	with pytest.raises(ValueError, match='Unknown application'):
		rag.search(rag.Search(query='equipment', application_ids=['unknown']))


def test_preferred_and_attachments_filters(indexed):
	library = past_applications.library()
	doc = library['documents'][1]
	past_applications.review('2002', past_applications.Review(preferred={'Part B2': doc['id']}))
	assert rag.status()['stale']
	with pytest.raises(ValueError, match='Rebuild'):
		rag.search(search_request())
	rag.build_index()
	hits = rag.search(search_request(preferred_only=True))['hits']
	assert len(hits) == 1 and hits[0]['document_id'] == doc['id']
	data = past_applications._read()
	for d in data['documents']:
		d['group'] = 'CV and publication list'
	past_applications._save(data)
	rag.build_index()
	assert not rag.search(search_request())['hits']
	assert rag.search(search_request(include_attachments=True))['hits']


def test_a_proposal_not_named_part_b_is_searched_by_default(indexed):
	"""An unrecognised file keeps its stem as its group. Only Part B1/B2 used to
	count as narrative, so example_proposal.txt was left out unless the
	search also took in CVs and consent forms."""
	data = past_applications._read()
	for d in data['documents']:
		d['group'] = 'example_proposal'
	past_applications._save(data)
	rag.build_index()

	assert rag.search(search_request())['hits']


def test_verbatim_chunks_have_no_dropped_text_or_invented_page_numbers():
	text = 'A very long line ' * 300 + '\nAnother paragraph\n' * 100
	parts = list(rag.chunks(text))
	assert ''.join(p['text'] for p in parts) == text
	assert all(len(p['text']) <= rag.MAX_CHARS for p in parts)
	assert all('page' not in p for p in parts)


def test_passage_provenance_names_the_text_kind_once():
	passage = {
		'document_name': 'd.docx',
		'application_title': 'Example',
		'applicant': 'Unknown',
		'programme': 'Unknown',
		'year': '2002',
		'ownership': 'unclassified',
		'line_start': 1,
		'line_end': 4,
		'warnings': [],
		'text': 'body',
	}
	native, ocr = rag.evidence([{**passage, 'kind': 'text'}, {**passage, 'kind': 'ocr'}])
	assert 'native text lines 1–4' in native['text'] and 'text text' not in native['text']
	assert 'OCR text lines 1–4' in ocr['text']


def make_preview():
	hit = rag.search(search_request())['hits'][0]
	return rag.preview(
		rag.Preview(
			chunk_ids=[hit['source_id']],
			topic_code=CALL['topic_code'],
			query='new equipment concepts',
		),
		CALL,
		PORTFOLIO,
		concepts.connection_settings(),
	)


@pytest.mark.parametrize('change', ['connection', 'profile', 'call', 'source', 'expired'])
def test_generation_consent_is_bound_to_exact_snapshot(indexed, change):
	preview = make_preview()
	record = rag.load_preview(preview['id'])
	call, profile, connection = CALL, PORTFOLIO, concepts.connection_settings()
	if change == 'connection':
		connection = {**connection, 'base_url': 'https://another.example'}
	elif change == 'profile':
		profile = []
	elif change == 'call':
		call = {**call, 'scope_text': 'New scope'}
	elif change == 'source':
		(indexed / 'text-0.txt').write_text('changed text')
	else:
		record['created_at'] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
	with pytest.raises(ValueError):
		rag.validate_preview(record, call, profile, connection)


def test_unapproved_requests_and_unknown_passages_never_start_model(indexed, monkeypatch):
	monkeypatch.setattr(concepts, 'start_job', lambda *a, **k: pytest.fail('Not approved'))
	monkeypatch.setattr(
		concepts, 'connection_status', lambda *a, **k: pytest.fail('No model contact')
	)
	client = TestClient(api.app)
	preview = make_preview()
	assert (
		client.post(
			'/archive-rag/generate', json={'preview_id': preview['id'], 'approved': False}
		).status_code
		== 422
	)
	assert (
		client.post(
			'/archive-rag/preview',
			json={
				'chunk_ids': ['ARCHIVE_unknown'],
				'topic_code': CALL['topic_code'],
				'query': 'test',
			},
		).status_code
		== 409
	)
	assert (
		client.post(
			'/archive-rag/search',
			json=search_request().model_dump(),
			headers={'origin': 'https://untrusted.example'},
		).status_code
		== 403
	)
	response = client.post('/archive-rag/search', json=search_request().model_dump())
	assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'


def test_approved_archive_pipeline_cites_separately_and_uses_disclosed_endpoint(
	indexed, monkeypatch
):
	preview = make_preview()
	requests_seen = []
	monkeypatch.setattr(concepts, '_model_digest', lambda connection: None)
	monkeypatch.setattr(concepts, 'connection_status', lambda connection: {'ready': True})

	def post(url, json, timeout, allow_redirects, stream=False):
		assert allow_redirects is False
		requests_seen.append((url, json))
		props = json['format']['properties']
		if 'concept_critiques' in props:
			result = _fake_critique()
		else:
			fields = props['concepts']['items']['properties']
			call_ids = fields['call_evidence']['items']['properties']['source_id']['enum']
			profile_ids = fields['researcher_evidence']['items']['properties']['source_id']['enum']
			archive_ids = fields['archive_evidence']['items']['properties']['source_id']['enum']
			assert all(i.startswith('PROFILE_') for i in profile_ids)
			result = _fake_concept_set(call_ids + profile_ids, call_ids, profile_ids)
			for c in result['concepts']:
				c['archive_evidence'] = [
					{'source_id': archive_ids[0], 'claim_supported': 'Historical inspiration only'}
				]
		return _FakeResponse({'message': {'content': __import__('json').dumps(result)}})

	monkeypatch.setattr(concepts.requests, 'post', post)
	client = TestClient(api.app)
	response = client.post(
		'/archive-rag/generate', json={'preview_id': preview['id'], 'approved': True}
	)
	assert response.status_code == 202, response.text
	job = _wait_for(response.json()['id'])
	assert job['status'] == 'succeeded', job['error']
	assert len(requests_seen) == 3
	for url, body in requests_seen:
		assert url == preview['connection']['base_url'] + '/api/chat'
		assert 'ARCHIVE INSPIRATION RULES' in body['messages'][0]['content']
		assert preview['passages'][0]['source_id'] in body['messages'][1]['content']
	assert job['archive_context']['passages'] == preview['passages']
	assert (
		client.post(
			'/archive-rag/generate', json={'preview_id': preview['id'], 'approved': True}
		).json()['id']
		== job['id']
	)
	assert len(requests_seen) == 3  # retries of the same HTTP submission are idempotent
	with pytest.raises(ValueError, match='fresh passage preview'):
		concepts.start_job(CALL, PORTFOLIO, retry_of=job['id'], trial=True)
	assert not portfolio.PORTFOLIO_PATH.exists()
	assert (concepts.JOBS_DIR / f'{job["id"]}.json').exists()


def test_archive_citations_cannot_masquerade_as_profile_and_unknown_ids_fail(indexed):
	packet = concepts.build_generation_packet(
		job_id='test', call=CALL, portfolio=PORTFOLIO, archive_passages=make_preview()['passages']
	)
	ids = packet['allowed_evidence_ids']
	call_ids = [i for i in ids if i.startswith('CALL_')]
	profile_ids = [i for i in ids if i.startswith('PROFILE_')]
	archive_ids = [i for i in ids if i.startswith('ARCHIVE_')]
	result = _fake_concept_set(ids, call_ids, profile_ids)
	with pytest.raises(ValueError, match='must cite'):
		concepts._validate_product_concepts(
			result, allowed_evidence_ids=ids, require_researcher_evidence=True
		)
	for c in result['concepts']:
		c['archive_evidence'] = [{'source_id': archive_ids[0], 'claim_supported': 'Inspiration'}]
	result['concepts'][0]['researcher_evidence'][0]['source_id'] = archive_ids[0]
	with pytest.raises(ValueError, match='unknown researcher_evidence'):
		concepts._validate_product_concepts(
			result, allowed_evidence_ids=ids, require_researcher_evidence=True
		)
	result['concepts'][0]['researcher_evidence'][0]['source_id'] = profile_ids[0]
	result['concepts'][0]['archive_evidence'][0]['source_id'] = 'ARCHIVE_invented'
	with pytest.raises(ValueError, match='Invalid archive citation'):
		concepts._validate_product_concepts(
			result, allowed_evidence_ids=ids, require_researcher_evidence=True
		)
