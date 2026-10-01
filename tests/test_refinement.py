"""Field extraction from a call's text. The point of these is the quote check:
a value the announcement does not support must never reach the dossier."""

import json

import pytest

from grant_recommender import concepts, refinement

SOURCE = """Richtlinie zur Förderung von Projekten zum Thema klinische Studien.
vom 01.06.2026 - Abgabetermin: 02.09.2026
Antragsberechtigt sind Hochschulen und außeruniversitäre Forschungseinrichtungen.
Die Zuwendung kann bis zu 500.000 Euro je Vorhaben betragen.
"""

CALL = {'topic_code': 'BMFTR-1', 'title': 'Klinische Studien', 'scope_text': SOURCE}


def _reply(payload):
	class _Streamed:
		status_code = 200

		def raise_for_status(self):
			pass

		def json(self):
			return {'message': {'content': json.dumps(payload)}}

		def iter_lines(self):
			text = json.dumps(payload)
			yield json.dumps({'message': {'content': text}, 'done': False}).encode()
			yield json.dumps({'message': {'content': ''}, 'done': True}).encode()

	return _Streamed()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
	monkeypatch.setattr(refinement, 'ROOT', tmp_path)
	monkeypatch.setattr(
		concepts, 'connection_settings', lambda: {'base_url': 'http://x', 'model': 'm'}
	)


def test_a_quote_that_appears_in_the_source_verifies():
	assert refinement.verify('Abgabetermin: 02.09.2026 Antragsberechtigt sind', SOURCE)


def test_whitespace_and_case_differences_do_not_break_a_real_quote():
	# The model re-wraps text; that must not invalidate a genuine quote.
	assert refinement.verify('abgabetermin:   02.09.2026\n  Antragsberechtigt SIND', SOURCE)


def test_an_invented_quote_fails():
	assert not refinement.verify('Abgabetermin: 01.01.2030 for all applicants', SOURCE)


def test_a_trivially_short_quote_cannot_verify_anything():
	# 'die' appears in the text, but matching it proves nothing.
	assert not refinement.verify('die', SOURCE)


def test_fields_with_unverifiable_quotes_are_dropped_not_shown():
	parsed = {
		'deadline': {'value': '02.09.2026', 'quote': 'vom 01.06.2026 - Abgabetermin: 02.09.2026'},
		'budget': {'value': '2.000.000 Euro', 'quote': 'Die Zuwendung kann bis zu 2.000.000 Euro'},
		'eligibility': None,
		'consortium': None,
		'focus': None,
	}

	kept, dropped = refinement.check(parsed, SOURCE)

	assert kept['deadline']['value'] == '02.09.2026'
	# The budget quote was altered, so the number it carries is not trustworthy.
	assert 'budget' not in kept
	assert dropped == ['budget']


def test_refine_keeps_only_verified_fields_and_records_the_rest(monkeypatch):
	payload = {
		'deadline': {'value': '02.09.2026', 'quote': 'Abgabetermin: 02.09.2026'},
		'eligibility': {
			'value': 'Universities and non-university research institutions',
			'quote': 'Antragsberechtigt sind Hochschulen und außeruniversitäre Forschungseinrichtungen.',
		},
		'budget': {'value': '9 million', 'quote': 'bis zu 9.000.000 Euro je Vorhaben'},
		'consortium': None,
		'focus': None,
	}
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _reply(payload))

	result = refinement.refine(CALL)

	assert set(result['fields']) == {'deadline', 'eligibility'}
	assert result['dropped'] == ['budget']
	assert result['fields']['deadline']['value'] == '02.09.2026'


def test_a_refinement_is_cached_and_not_recomputed(monkeypatch):
	payload = {
		'deadline': {'value': '02.09.2026', 'quote': 'Abgabetermin: 02.09.2026'},
		'eligibility': None,
		'budget': None,
		'consortium': None,
		'focus': None,
	}
	calls = []

	def post(*args, **kwargs):
		calls.append(1)
		return _reply(payload)

	monkeypatch.setattr(concepts.requests, 'post', post)

	refinement.refine(CALL)
	refinement.refine(CALL)

	assert len(calls) == 1


def test_changed_page_text_invalidates_the_cache(monkeypatch):
	payload = {
		'deadline': {'value': '02.09.2026', 'quote': 'Abgabetermin: 02.09.2026'},
		'eligibility': None,
		'budget': None,
		'consortium': None,
		'focus': None,
	}
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _reply(payload))
	refinement.refine(CALL)

	# The funder edited the page: the stored answer is about text that is gone.
	assert refinement.cached({**CALL, 'scope_text': SOURCE + '\nNeue Frist: 01.12.2026'}) is None


def test_a_call_with_no_text_is_refused(monkeypatch):
	with pytest.raises(ValueError, match='no text'):
		refinement.refine({'topic_code': 'X', 'scope_text': '   '})


def test_a_quote_beyond_the_prompt_cut_still_verifies(monkeypatch):
	"""Only the first stretch of a page is sent to the model, but verification
	runs against the whole thing, so a quote from deeper in is still real."""
	long_source = ('filler line\n' * 4000) + 'Abgabetermin fuer diese Ausschreibung: 30.11.2026'
	payload = {
		'deadline': {
			'value': '30.11.2026',
			'quote': 'Abgabetermin fuer diese Ausschreibung: 30.11.2026',
		},
		'eligibility': None,
		'budget': None,
		'consortium': None,
		'focus': None,
	}
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _reply(payload))

	result = refinement.refine({'topic_code': 'LONG-1', 'scope_text': long_source})

	assert result['fields']['deadline']['value'] == '30.11.2026'


def test_a_null_field_is_simply_absent(monkeypatch):
	payload = dict.fromkeys(refinement.FIELDS)
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _reply(payload))

	result = refinement.refine(CALL)

	assert result['fields'] == {}
	assert result['dropped'] == []


def test_the_model_gets_no_null_branch_to_skip_a_field(monkeypatch):
	"""Offered a null branch, gemma4:12b-mlx answered null for all five fields
	under constrained decoding, even for a deadline stated word for word. A field
	the text lacks now comes back empty, which check() skips without a drop."""
	sent = []
	empty = {'synopsis': '', **{name: {'value': '', 'quote': ''} for name in refinement.FIELDS}}

	def post(url, json, **kwargs):
		sent.append(json)
		return _reply(empty)

	monkeypatch.setattr(concepts.requests, 'post', post)

	result = refinement.refine(CALL)

	schema = sent[0]['format']
	assert [schema['properties'][name]['type'] for name in refinement.FIELDS] == ['object'] * 5
	assert 'null' not in sent[0]['messages'][0]['content']
	assert result['fields'] == {} and result['dropped'] == []


def test_text_cut_off_at_its_length_cap_ends_on_its_last_full_sentence():
	"""Ollama stops a string at its maxLength mid-word, and the broken sentence
	used to be shown and saved. Text short of the cap keeps every dot."""
	cut = ('The call funds clinical studies. ' * 30)[: refinement.SYNOPSIS_CHARS]
	assert cut.endswith('The cal')

	synopsis, invented = refinement.check_synopsis(cut, SOURCE)

	assert synopsis.endswith('clinical studies.') and not invented
	kept, _ = refinement.check(
		{'deadline': {'value': '02. September 2026', 'quote': 'Abgabetermin: 02.09.2026'}}, SOURCE
	)
	assert kept['deadline']['value'] == '02. September 2026'


def test_the_token_stop_covers_all_the_text_the_schema_allows():
	"""Once every field had to be filled in, qwen3:4b-instruct-2507 hit the old
	900-token stop on two German notices and the cut JSON failed to parse. Its
	densest output ran 2.25 characters per token."""
	props = refinement.SCHEMA['properties']
	chars = props['synopsis']['maxLength'] + sum(
		cap['maxLength'] for name in refinement.FIELDS for cap in props[name]['properties'].values()
	)
	assert refinement.NUM_PREDICT * 2.25 >= chars


def test_a_german_deadline_the_model_missed_is_read_from_its_sentence(monkeypatch):
	"""qwen3:4b-instruct-2507 gave the right date for a DFG notice with a quote
	from another sentence, so the check dropped it and the notice showed no
	deadline. The first 'bis zum' date in that notice is a letter of intent."""
	source = (
		'Die Absichtserklärung reichen Sie bitte bis zum 8. Dezember 2026, 12 Uhr ein.\n'
		'Bitte beachten Sie, dass der Antrag bis spätestens 27. Januar 2027, 12 Uhr, '
		'bei der DFG eingereicht werden muss.'
	)
	payload = {
		'synopsis': '',
		**{name: {'value': '', 'quote': ''} for name in refinement.FIELDS},
		'deadline': {
			'value': '27. Januar 2027, 12 Uhr',
			'quote': 'Der Antrag wird von der antragstellenden Hochschule gestellt.',
		},
	}
	monkeypatch.setattr(concepts.requests, 'post', lambda *a, **k: _reply(payload))

	result = refinement.refine({'topic_code': 'DFG-1', 'scope_text': source})

	assert result['fields']['deadline'] == {
		'value': '27. Januar 2027',
		'quote': 'Bitte beachten Sie, dass der Antrag bis spätestens 27. Januar 2027, 12 Uhr, '
		'bei der DFG eingereicht werden muss',
	}
	assert result['dropped'] == []


@pytest.mark.parametrize(
	'sentence',
	[
		'Der Antrag muss bis zum 27. Januar 2027 eingereicht werden.',
		'Der Antrag muss bis spätestens 27. Januar 2027 eingereicht werden.',
		'Bitte reichen Sie den Antrag bis spätestens zum 27. Januar 2027 ein.',
	],
)
def test_the_german_deadline_fallback_reads_the_common_phrasings(sentence):
	match = refinement.DE_DEADLINE.search(sentence)
	assert match and match.group(1) == '27. Januar 2027'


def test_a_date_dot_is_not_taken_for_a_sentence_end():
	"""Cutting back at '13.' would leave a value ending in half a date."""
	text = 'Erster Satz. Die Anträge sind bis zum 13. Januar 2027 elektronisch einzureichen und'

	assert refinement.finish(text, len(text)) == 'Erster Satz.'
