import pytest

from grant_recommender import (
	concepts,
	decisions,
	discovery_panel,
	documents,
	generation_settings,
	german_funding,
	lifecycle,
	portfolio,
	proposals,
	refinement,
	reviews,
	watch,
)


@pytest.fixture(autouse=True)
def isolate_product_connections(monkeypatch, tmp_path):
	"""Never read or mutate a user's live source watch-list or model preferences."""
	monkeypatch.setattr(german_funding, 'CACHE', tmp_path / 'source_cache')
	monkeypatch.setattr(german_funding, 'WATCHLIST', tmp_path / 'german_watchlist.json')
	monkeypatch.setattr(generation_settings, 'SETTINGS_PATH', tmp_path / 'generation_settings.json')


@pytest.fixture(autouse=True)
def isolate_private_stores(monkeypatch, tmp_path):
	"""Point every store under data/private at a temp directory.

	Without this the suite wrote to the researcher's own records: a full run
	appended `develop`/`watcher` rows to the real decision log and started real
	concept jobs against Ollama, because test_watch drives `watch.run` with the
	real `discover`.

	Nothing in the suite needs the real jobs: clearing `concepts.JOBS` as well
	and re-running leaves every test passing.
	"""
	private = tmp_path / 'private'
	private.mkdir()
	monkeypatch.setattr(decisions, 'DECISIONS_PATH', private / 'decisions.jsonl')
	monkeypatch.setattr(concepts, 'JOBS_DIR', private / 'concept_jobs')
	# concepts.py calls _load_persisted_jobs() at import, long before any fixture
	# can run, so JOBS already holds every real run on disk. Patching the
	# directory alone left list_jobs/get_job/load_job returning the researcher's
	# own jobs to any test that did not stub them -- which is how test_watch's
	# tick() quietly took its early-return branch and asserted nothing.
	monkeypatch.setattr(concepts, 'JOBS', {})
	monkeypatch.setattr(lifecycle, 'JOBS_DIR', private / 'concept_jobs')
	monkeypatch.setattr(discovery_panel, 'JOBS_DIR', private / 'concept_jobs')
	monkeypatch.setattr(proposals, 'ROOT', private / 'proposals')
	monkeypatch.setattr(reviews, 'ROOT', private / 'reviews')
	monkeypatch.setattr(refinement, 'ROOT', private / 'refinements')
	monkeypatch.setattr(documents, 'ROOT', private / 'call_documents')
	# Computed at import from DATA, so patching DATA alone would not move it.
	monkeypatch.setattr(portfolio, 'PORTFOLIO_PATH', private / 'portfolio.json')
	monkeypatch.setattr(portfolio, 'DATA', private)


# Captured before anything can patch it, so the discovery tests can opt back in.
REAL_DISCOVER = watch.discover


@pytest.fixture(autouse=True)
def never_scrape_for_real(monkeypatch):
	"""The watch loop's own tests drove `run()` with the real `discover`, which
	hit the ministries' feeds on every suite run and could start a real concept
	job against Ollama. A test that means to exercise discovery asks for the
	`real_discover` fixture."""
	monkeypatch.setattr(watch, 'discover', lambda *args, **kwargs: ({}, []))


@pytest.fixture
def real_discover(monkeypatch):
	"""Opt back in to the real discover(), for the tests that are about it.
	Those stub `german_funding.refresh` and `import_notice` themselves, so no
	request leaves the machine."""
	monkeypatch.setattr(watch, 'discover', REAL_DISCOVER)
	return REAL_DISCOVER
