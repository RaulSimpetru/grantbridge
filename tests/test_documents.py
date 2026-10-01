"""Call attachments. Discovery is deterministic, so the tests worth having are
about what is refused: off-host links, oversized files, and redirects."""

import pytest

from grant_recommender import documents, german_funding

PAGE = """<html><body>
  <a href="/files/Vorlage_Projektskizze.docx">Vorlage Projektskizze</a>
  <a href="/files/Leitfaden.pdf">Leitfaden fuer Antragstellende</a>
  <a href="/files/anlage-1.pdf">Anlage 1</a>
  <a href="https://elsewhere.example/steal.pdf">A PDF on another host</a>
  <a href="/programme">A normal page, not a file</a>
  <a href="/files/Vorlage_Projektskizze.docx">The same file again</a>
</body></html>"""

CALL = {
	'topic_code': 'BMFTR-1',
	'provider': 'bmftr',
	'source_url': 'https://www.gesundheitsforschung-bmftr.de/de/19250.php',
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
	monkeypatch.setattr(documents, 'ROOT', tmp_path)


def test_only_files_on_the_calls_own_host_are_offered():
	found = documents.discover(CALL, PAGE)
	urls = [d['url'] for d in found]

	assert any('Vorlage_Projektskizze.docx' in u for u in urls)
	# A funder page can link anywhere; fetching those is not this module's job.
	assert not any('elsewhere.example' in u for u in urls)
	# And a page is not a document.
	assert not any(u.endswith('/programme') for u in urls)


def test_the_same_file_linked_twice_appears_once():
	assert len(documents.discover(CALL, PAGE)) == 3


def test_documents_are_labelled_by_what_they_look_like():
	by_name = {d['filename']: d for d in documents.discover(CALL, PAGE)}

	assert by_name['Vorlage_Projektskizze.docx']['kind'] == 'template'
	assert by_name['Leitfaden.pdf']['kind'] == 'guidance'
	assert by_name['anlage-1.pdf']['kind'] == 'annex'
	assert by_name['Leitfaden.pdf']['format'] == 'PDF'


def test_an_eu_call_offers_nothing_because_its_host_is_not_allowlisted():
	"""Not a silent failure: the allowlist is what stops this module reaching
	hosts it was never authorised for, and EU topic pages are not covered."""
	eu = {'topic_code': 'HORIZON-1', 'source_url': 'https://ec.europa.eu/info/x'}
	page = '<a href="https://ec.europa.eu/files/template.docx">Template</a>'

	assert documents.discover(eu, page) == []


def _response(monkeypatch, *, body=b'hello', status=200, headers=None):
	class _Resp:
		status_code = status

		def __enter__(self):
			return self

		def __exit__(self, *args):
			return False

		def raise_for_status(self):
			pass

		@property
		def headers(self):
			return headers or {}

		def iter_content(self, size):
			yield body

	monkeypatch.setattr(documents.requests, 'get', lambda *a, **k: _Resp())


def test_a_download_lands_under_the_calls_own_folder(monkeypatch, tmp_path):
	_response(monkeypatch, body=b'PDF-ish bytes')
	document = documents.discover(CALL, PAGE)[0]

	path = documents.download(CALL, document)

	assert path.read_bytes() == b'PDF-ish bytes'
	assert path.parent.name == 'BMFTR-1'
	assert documents.stored(CALL) == [path]


def test_a_second_download_reuses_the_file_already_on_disk(monkeypatch):
	_response(monkeypatch, body=b'once')
	document = documents.discover(CALL, PAGE)[0]
	first = documents.download(CALL, document)

	def explode(*args, **kwargs):
		raise AssertionError('should not refetch')

	monkeypatch.setattr(documents.requests, 'get', explode)

	assert documents.download(CALL, document) == first


def test_an_off_host_file_is_refused_even_if_asked_for_directly(monkeypatch):
	_response(monkeypatch)
	with pytest.raises(ValueError, match='host'):
		documents.download(CALL, {'url': 'https://elsewhere.example/x.pdf', 'filename': 'x.pdf'})


def test_a_file_that_declares_itself_too_large_is_refused(monkeypatch):
	_response(monkeypatch, headers={'content-length': str(documents.MAX_BYTES + 1)})
	document = documents.discover(CALL, PAGE)[0]

	with pytest.raises(ValueError, match='over the cap'):
		documents.download(CALL, document)


def test_a_file_that_grows_past_the_cap_mid_download_is_abandoned(monkeypatch):
	# A server can under-report content-length, so the cap is enforced on the
	# bytes actually arriving, not just on the header.
	_response(monkeypatch, body=b'x' * (documents.MAX_BYTES + 10))
	document = documents.discover(CALL, PAGE)[0]

	with pytest.raises(ValueError, match='exceeded the size cap'):
		documents.download(CALL, document)
	assert documents.stored(CALL) == []


def test_a_redirect_is_not_followed(monkeypatch):
	_response(monkeypatch, status=302)
	document = documents.discover(CALL, PAGE)[0]

	with pytest.raises(ValueError, match='redirected'):
		documents.download(CALL, document)


def test_a_custom_source_can_offer_its_own_files(monkeypatch, tmp_path):
	monkeypatch.setattr(german_funding, 'CUSTOM_SOURCES', tmp_path / 'sources.json')
	german_funding._write(
		german_funding.CUSTOM_SOURCES,
		{
			'custom-1': {
				'name': 'S',
				'url': 'https://stiftung.example/f',
				'host': 'stiftung.example',
			}
		},
	)
	call = {
		'topic_code': 'custom-1-a',
		'provider': 'custom-1',
		'source_url': 'https://stiftung.example/call',
	}

	found = documents.discover(call, '<a href="/forms/antrag.pdf">Antragsformular</a>')

	assert [d['filename'] for d in found] == ['antrag.pdf']
