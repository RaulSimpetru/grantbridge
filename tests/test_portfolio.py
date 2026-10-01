import re
import shutil
from pathlib import Path
from typing import get_args

import pytest

from grant_recommender import portfolio


@pytest.fixture(autouse=True)
def _private_portfolio(monkeypatch, tmp_path):
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', tmp_path / 'portfolio.json')


def test_load_portfolio_defaults_to_empty_when_no_file_yet():
	assert portfolio.load_portfolio() == []


def test_add_assertion_round_trips_and_defaults_included_true():
	record = portfolio.add_assertion(
		component='methods', claim='CGM signal processing', source='CV', confidence='confirmed'
	)
	assert record['included'] is True
	assert record['visibility'] == 'private'
	assert portfolio.load_portfolio() == [record]


def test_update_assertion_can_change_confidence_and_visibility():
	created = portfolio.add_assertion(
		component='software', claim='glucose-toolkit', source=None, confidence='needs_review'
	)
	updated = portfolio.update_assertion(created['id'], confidence='confirmed', visibility='public')
	assert updated['confidence'] == 'confirmed'
	assert updated['visibility'] == 'public'
	assert updated['claim'] == 'glucose-toolkit'  # untouched fields survive the partial update


def test_update_assertion_raises_for_unknown_id():
	with pytest.raises(KeyError):
		portfolio.update_assertion('does-not-exist', confidence='confirmed')


def test_import_openalex_topics_creates_inferred_assertions_and_skips_reimport(monkeypatch):
	fake_author = {
		'display_name': 'Test Researcher',
		'topics': [
			{'display_name': 'Glucose Monitoring', 'count': 10},
			{'display_name': 'Metabolic Health', 'count': 3},
		],
	}
	monkeypatch.setattr('grant_recommender.openalex.get_author', lambda author_id: fake_author)

	created = portfolio.import_openalex_topics('A123', top_n=8)

	assert [c['claim'] for c in created] == ['Glucose Monitoring', 'Metabolic Health']
	assert all(c['confidence'] == 'inferred' for c in created)
	assert all(c['component'] == 'topics_trajectory' for c in created)

	# a second import doesn't duplicate what's already there
	again = portfolio.import_openalex_topics('A123', top_n=8)
	assert again == []
	assert len(portfolio.load_portfolio()) == 2


def test_example_portfolio_loads_and_withholds_its_inferred_claims_from_the_model():
	# The README copies this file into a fresh clone, so it must stay in the
	# product schema, and its inferred claims must never reach the model.
	# conftest points portfolio.DATA at a temp dir, so find the shipped file here.
	example = Path(__file__).resolve().parents[1] / 'data' / 'example_portfolio.json'
	shutil.copy(example, portfolio.PORTFOLIO_PATH)
	assertions = portfolio.load_portfolio()

	assert len({a['id'] for a in assertions}) == len(assertions)
	for a in assertions:
		assert re.fullmatch(r'[0-9a-f]{12}', a['id'])
		assert a['component'] in get_args(portfolio.Component)
		assert a['confidence'] in get_args(portfolio.Confidence)
		assert a['visibility'] in get_args(portfolio.Visibility)
		assert a['included'] is True and a['claim'] and a['source']
	used = [a for a in assertions if portfolio.usable(a)]
	assert len(used) >= 3 and len(used) < len(assertions)
	assert len(portfolio.researcher_evidence(assertions)) == len(used)


def test_example_claims_sent_to_the_model_cite_a_source_not_an_instruction():
	"""The model reads each confirmed claim's source. 'Invented example - replace
	with your own CV' made reviewers flag the very claims it stood behind."""
	example = Path(__file__).resolve().parents[1] / 'data' / 'example_portfolio.json'
	shutil.copy(example, portfolio.PORTFOLIO_PATH)

	evidence = portfolio.researcher_evidence(portfolio.load_portfolio())

	assert evidence and not any('example' in e['text'].lower() for e in evidence)
