"""Local GrantBridge HTTP API: SEDIA-backed call data, plus a private portfolio.

Single user, no authentication: every API route answers loopback callers only
(see _local_only), and the launcher binds 127.0.0.1.

Calls: GET /calls (the curated watch-list -- see data/watched_topics.json,
there is no working browse/list mechanism against SEDIA) and GET /calls/{code}
(any topic code, looked up live/cached through catalog.get_topic).

Portfolio: local, single-user, source-backed assertions (see portfolio.py) --
GET/POST /portfolio, PATCH /portfolio/{id} for the correction path, and
POST /portfolio/import/openalex to auto-populate 'inferred' topic assertions
from a real OpenAlex author profile.

Concepts: POST /concepts/jobs starts an async, logged generation job (see
concepts.py) against a real Ollama endpoint; GET /concepts/jobs[/{id}] polls
it. Decisions: POST/GET /decisions is an append-only save/dismiss/develop/
choose log (see decisions.py).
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Literal

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from grant_recommender import (
	archive_rag,
	catalog,
	concepts,
	decisions,
	eligibility,
	generation_settings,
	german_funding,
	past_applications,
	proposals,
	watch,
)
from grant_recommender.dossier import build_dossier
from grant_recommender.portfolio import (
	Component,
	Confidence,
	CorruptPortfolio,
	Visibility,
	add_assertion,
	import_openalex_topics,
	load_portfolio,
	researcher_evidence,
	update_assertion,
	usable,
)
from grant_recommender.scoring import score_calls


class FitScore(BaseModel):
	keyword_overlap: float
	bm25: float
	rank: int
	n_portfolio_claims: int


class Eligibility(BaseModel):
	state: Literal['eligible', 'ineligible', 'uncertain']
	reason: str
	basis: str | None = None
	source: str | None = None
	reviewed_at: str | None = None


class Call(BaseModel):
	provider: str = 'eu'
	country: str | None = None
	topic_code: str
	title: str | None
	call_identifier: str | None
	call_title: str | None
	deadlines: list[str]
	status: str | None
	types_of_action: list[str]
	scope_text: str
	topic_conditions: str = ''
	topic_conditions_document: list[dict] = Field(default_factory=list)
	framework_programme: str | None = None
	programme_period: str | None = None
	source_url: str | None = None
	fetched_at: str | None = None
	scientific_fit: FitScore | None = None
	eligibility: Eligibility
	availability: str = 'unknown'
	dossier: dict = Field(default_factory=dict)
	source_warning: str | None = None


class Assertion(BaseModel):
	id: str
	component: Component
	claim: str
	source: str | None
	confidence: Confidence
	visibility: Visibility
	included: bool
	phd_award_date: date | None = None


class AssertionCreate(BaseModel):
	component: Component
	claim: str = Field(min_length=1, max_length=20000)
	source: str | None = None
	confidence: Confidence = 'needs_review'
	visibility: Visibility = 'private'
	phd_award_date: date | None = None

	@model_validator(mode='after')
	def validate_claim(self):
		self.claim = self.claim.strip()
		if not self.claim:
			raise ValueError('Claim cannot be empty')
		if self.phd_award_date and (
			self.phd_award_date > datetime.now(UTC).date() or self.component != 'career_eligibility'
		):
			raise ValueError('PhD date must be a past career/eligibility fact')
		return self


class AssertionUpdate(BaseModel):
	claim: str | None = None
	source: str | None = None
	confidence: Confidence | None = None
	visibility: Visibility | None = None
	included: bool | None = None
	phd_award_date: date | None = None

	@model_validator(mode='after')
	def reject_nulls(self):
		for key in self.model_fields_set - {'source', 'phd_award_date'}:
			if getattr(self, key) is None:
				raise ValueError(f'{key} cannot be null')
		if self.claim is not None and not self.claim.strip():
			raise ValueError('Claim cannot be empty')
		if self.phd_award_date and self.phd_award_date > datetime.now(UTC).date():
			raise ValueError('PhD date cannot be in the future')
		return self


class EligibilityReview(BaseModel):
	state: Literal['eligible', 'ineligible']
	reason: str = Field(min_length=10, max_length=5000)
	source: str = Field(min_length=10, max_length=2000)
	conditions_checked: Literal[True]

	@model_validator(mode='after')
	def nonblank_evidence(self):
		if min(len(self.reason.strip()), len(self.source.strip())) < 10:
			raise ValueError('Include a substantive rationale and official source')
		return self


class OpenAlexImportRequest(BaseModel):
	author_id: str


class Stage(BaseModel):
	name: str
	status: Literal['pending', 'running', 'succeeded', 'failed']
	wall_time_seconds: float | None = None
	detail: str | None = None
	# Tail of the model output while this stage is streaming; cleared when it
	# ends. Absent on jobs persisted before streaming existed.
	live: str | None = None
	started_at: str | None = None


class EvidenceItem(BaseModel):
	section: str
	text: str


class ConceptJob(BaseModel):
	trial: bool = False
	id: str
	topic_code: str
	status: Literal['running', 'succeeded', 'failed', 'interrupted']
	created_at: str
	retry_of: str | None
	seed_index: int
	model: str
	model_digest: str | None
	stages: list[Stage]
	draft: dict | None
	critique: dict | None
	concepts: list[dict] | None
	diversity: dict | None
	error: str | None
	evidence: dict[str, EvidenceItem]
	eligibility_snapshot: dict | None = None
	archive_context: dict | None = None
	revision_audit: dict | None = None


class ConceptJobCreate(BaseModel):
	trial: bool = False
	topic_code: str
	retry_of: str | None = None


class Decision(BaseModel):
	id: str
	kind: str
	target_type: str
	target_id: str
	reason: str | None
	actor: str = 'researcher'
	created_at: str


class DecisionCreate(BaseModel):
	kind: Literal[
		'save', 'unsave', 'dismiss', 'undo_dismiss', 'develop', 'undevelop', 'choose', 'unchoose'
	]
	target_type: Literal['call', 'concept']
	target_id: str
	reason: str | None = None


class ActivityItem(BaseModel):
	type: Literal['decision', 'concept_run']
	occurred_at: str
	decision: Decision | None = None
	concept_run: ConceptJob | None = None


def watched_topic_codes() -> list[str]:
	return catalog.watched_topic_codes()


@asynccontextmanager
async def lifespan(app: FastAPI):
	# Only an actual server restart interrupts jobs; imports for tests/tools
	# must never rewrite another running server's durable job snapshots.
	concepts._load_persisted_jobs()
	proposals.interrupt_pending()
	# The watch loop is off unless an interval is set, so importing the app for
	# tests or tooling never starts background work or spends model time.
	interval = float(os.environ.get('GRANTBRIDGE_WATCH_INTERVAL') or 0)
	stop = (
		watch.start_background(_loop_calls, load_portfolio, interval_s=interval)
		if interval > 0
		else None
	)
	try:
		yield
	finally:
		if stop:
			stop.set()


# The Vite dev server, plus this API's own /docs page.
LOCAL_ORIGINS = (
	'http://localhost:5173',
	'http://127.0.0.1:5173',
	'http://localhost:8010',
	'http://127.0.0.1:8010',
)


def _local_only(request: Request):
	# Binding 127.0.0.1 is not enough: a web page can still reach a loopback
	# server through DNS rebinding, so every route also checks Host and Origin.
	# Serving anyone else needs real authentication first.
	if request.client is None or request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
		raise HTTPException(403, 'GrantBridge is available on this computer only.')
	if request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}:
		raise HTTPException(403, 'Invalid host.')
	if request.headers.get('origin') not in {None, *LOCAL_ORIGINS}:
		raise HTTPException(403, 'This origin cannot use the local API.')
	if request.headers.get('sec-fetch-site') == 'cross-site' and not request.headers.get('origin'):
		raise HTTPException(403, 'Cross-site requests are not allowed.')


app = FastAPI(
	title='GrantBridge API',
	description=(
		'Local, single-user API behind the GrantBridge web UI: funding calls, the researcher '
		'portfolio, and evidence-grounded concept runs on an Ollama model.'
	),
	version='0.1.0',
	lifespan=lifespan,
	dependencies=[Depends(_local_only)],
)

app.add_middleware(
	CORSMiddleware,
	allow_origins=LOCAL_ORIGINS,
	allow_methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE'],
	allow_headers=['*'],
)


@app.exception_handler(requests.RequestException)
async def source_unavailable(request: Request, exc: requests.RequestException):
	# Only what escapes a route lands here, so catalog.watched_calls still skips
	# a single dead topic. Never echo the URL: it can carry an API key or mailto.
	status = getattr(exc.response, 'status_code', None)
	detail = (
		f'An external source answered HTTP {status}.'
		if status
		else 'An external source could not be reached. Retry shortly.'
	)
	return JSONResponse({'detail': detail}, status_code=503)


@app.exception_handler(CorruptPortfolio)
async def corrupt_portfolio(request: Request, exc: CorruptPortfolio):
	# Left as a 500 it would carry no CORS header, and the UI could only say
	# "Failed to fetch" on every page.
	return JSONResponse({'detail': str(exc)}, status_code=422)


@app.middleware('http')
async def private_evidence_headers(request: Request, call_next):
	response = await call_next(request)
	if request.url.path.startswith(
		('/past-app', '/archive-rag', '/concepts', '/activity', '/proposals')
	):
		response.headers['Cache-Control'] = 'no-store'
	return response


class ProposalCreate(BaseModel):
	job_id: str = Field(max_length=64)
	concept_id: str = Field(max_length=64)
	variant: Literal['original', 'revised']


class ProposalSection(BaseModel):
	revision: int = Field(ge=1)
	text: str = Field(max_length=30000)
	citations: list[dict] = Field(max_length=40)
	missing_info: list[str] = Field(max_length=40)


class ProposalPreview(BaseModel):
	section: Literal['aims', 'methods', 'work_packages', 'risks', 'impact']
	revision: int = Field(ge=1)
	instructions: str = Field(default='', max_length=4000)
	source_ids: list[str] = Field(min_length=1, max_length=100)


class ProposalApproval(BaseModel):
	approved: Literal[True]


def _proposal_action(action):
	try:
		return action()
	except ValueError as exc:
		if isinstance(exc, json.JSONDecodeError):
			raise HTTPException(422, 'The saved draft could not be read.') from None
		raise HTTPException(409, str(exc)) from None
	except (OSError, KeyError, TypeError):
		raise HTTPException(422, 'The local draft store is unavailable or invalid.') from None


@app.post('/proposals')
def create_proposal(body: ProposalCreate):
	return _proposal_action(lambda: proposals.create(body.job_id, body.concept_id, body.variant))


@app.get('/proposals')
def list_proposals(job_id: str):
	return _proposal_action(lambda: proposals.list_drafts(job_id))


@app.get('/proposals/previews/{preview_id}')
def get_proposal_preview(preview_id: str):
	return _proposal_action(lambda: proposals.get_preview(preview_id))


@app.post('/proposals/previews/{preview_id}/generate', status_code=202)
def generate_proposal_section(preview_id: str, body: ProposalApproval):
	return _proposal_action(lambda: proposals.generate(preview_id, body.approved))


@app.post('/proposals/previews/{preview_id}/accept')
def accept_proposal_section(preview_id: str):
	return _proposal_action(lambda: proposals.accept(preview_id))


@app.post('/proposals/previews/{preview_id}/discard')
def discard_proposal_section(preview_id: str):
	return _proposal_action(lambda: proposals.discard(preview_id))


@app.get('/proposals/{draft_id}')
def get_proposal(draft_id: str):
	return _proposal_action(lambda: proposals.get(draft_id))


@app.get('/proposals/{draft_id}/latest-preview')
def latest_proposal_preview(draft_id: str):
	return _proposal_action(lambda: proposals.latest_preview(draft_id))


@app.put('/proposals/{draft_id}/sections/{section}')
def save_proposal_section(draft_id: str, section: str, body: ProposalSection):
	return _proposal_action(
		lambda: proposals.save(
			draft_id, section, body.revision, body.model_dump(exclude={'revision'})
		)
	)


@app.post('/proposals/{draft_id}/preview')
def preview_proposal_section(draft_id: str, body: ProposalPreview):
	return _proposal_action(
		lambda: proposals.preview(
			draft_id, body.section, body.revision, body.instructions, body.source_ids
		)
	)


def _rag_action(action):
	try:
		return action()
	except ValueError as exc:
		# Only controlled errors from this module; parsing/path errors are not logged.
		if isinstance(exc, json.JSONDecodeError):
			raise HTTPException(422, 'Private index could not be read. Rebuild it.') from None
		raise HTTPException(409, str(exc)) from None
	except (OSError, KeyError, TypeError):
		raise HTTPException(
			422, 'Private sources are unavailable. Check the local archive and rebuild the index.'
		) from None


@app.get('/archive-rag/status')
def archive_index_status():
	return _rag_action(archive_rag.status)


@app.post('/archive-rag/index')
def build_archive_index():
	return _rag_action(archive_rag.build_index)


@app.post('/archive-rag/search')
def search_archive(body: archive_rag.Search):
	return _rag_action(lambda: archive_rag.search(body))


@app.post('/archive-rag/preview')
def preview_archive_generation(body: archive_rag.Preview):
	call = get_topic(body.topic_code)
	if not call:
		raise HTTPException(404, 'Call not found.')
	assertions = load_portfolio()
	# Check the same gates as concepts.start_job, without contacting a model.
	assessment = eligibility.assess(call, assertions)
	if not body.trial and assessment['state'] != 'eligible':
		raise HTTPException(409, assessment['reason'])
	if not any(usable(a) for a in assertions):
		raise HTTPException(
			409, 'Confirm at least one profile capability before generating concepts.'
		)
	return _rag_action(
		lambda: archive_rag.preview(body, call, assertions, concepts.connection_settings())
	)


@app.post('/archive-rag/generate', response_model=ConceptJob, status_code=202)
def generate_with_archive(body: archive_rag.Generate):
	def start():
		with archive_rag.LOCK:
			record = archive_rag.load_preview(body.preview_id)
			if record['job_id']:
				job = concepts.get_job(record['job_id'])
				if job:
					return job
				raise ValueError('Run no longer available. Create a new preview.')
			call = get_topic(record['topic_code'])
			if not call:
				raise ValueError('Call is unavailable. Preview again.')
			assertions = load_portfolio()
			connection = concepts.connection_settings()
			archive_rag.validate_preview(record, call, assertions, connection)
			# Health checks send no proposal content. Generation uses exactly the
			# disclosed connection snapshot even if preferences change concurrently.
			health = concepts.connection_status(connection)
			if not health['ready']:
				raise HTTPException(503, health['detail'])
			job = concepts.start_job(
				call, assertions, trial=record['trial'], archive_context=record
			)
			archive_rag.mark_started(record, job['id'])
			if not record['trial']:
				decisions.record('develop', 'call', record['topic_code'], reason=job['id'])
			return job

	return _rag_action(start)


@app.get('/past-applications')
def list_past_applications():
	return past_applications.library()


@app.post('/past-applications/import')
def import_past_applications():
	try:
		return past_applications.import_archive()
	except (OSError, ValueError, KeyError, TypeError):
		raise HTTPException(
			422,
			'Archive import could not be validated. Check the configured local manifest, catalog and source files.',
		) from None


@app.patch('/past-applications/{application_id}')
def review_past_application(application_id: str, body: past_applications.Review):
	try:
		return past_applications.review(application_id, body)
	except KeyError:
		raise HTTPException(404, 'Application not found.') from None
	except ValueError as exc:
		raise HTTPException(422, str(exc)) from None


@app.get('/past-application-documents/{doc_id}/{kind}')
def read_past_document(doc_id: str, kind: Literal['original', 'text', 'ocr']):
	try:
		path, name = past_applications.document(doc_id, kind)
		if kind != 'original':
			return JSONResponse(
				{'text': path.read_text(), 'kind': kind}, headers={'Cache-Control': 'no-store'}
			)
		return FileResponse(
			path,
			filename=name,
			media_type='application/octet-stream',
			headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
		)
	except KeyError:
		raise HTTPException(404, 'Document or text version not found.') from None
	except (OSError, ValueError):
		raise HTTPException(422, 'Source file is unavailable or failed validation.') from None


def get_topic(code: str) -> dict | None:
	return catalog.get_topic(code)


def _watched_calls() -> list[dict]:
	# Resolution is passed in so this module's seams stay patchable; the union
	# itself lives in catalog.py, which the terminal panels use too.
	try:
		return catalog.watched_calls(codes=watched_topic_codes(), resolve=get_topic)
	except catalog.SourcesUnavailable as exc:
		raise HTTPException(status_code=503, detail=str(exc)) from None


def _loop_calls() -> list[dict]:
	# The watch loop's call source. SourcesUnavailable means nothing resolved
	# and nothing is imported yet: a fresh clone during an EU portal outage.
	# Raising there would stop every tick before discover() reaches the German
	# feeds, so the loop starts from an empty list. GET /calls still answers 503.
	try:
		return _watched_calls()
	except HTTPException:
		return []


def _enrich(call: dict, assertions: list[dict], fit=None) -> dict:
	return {
		**call,
		'scientific_fit': fit,
		'eligibility': eligibility.assess(call, assertions),
		'availability': eligibility.availability(call),
		'dossier': build_dossier(call, assertions),
	}


@app.get('/calls', response_model=list[Call])
def list_calls() -> list[dict]:
	calls = _watched_calls()
	assertions = load_portfolio()
	fit = score_calls(calls, assertions)
	# Build new dicts, never mutate `call`: scores derived from the private
	# portfolio must not end up in the source records.
	return [_enrich(call, assertions, fit[call['topic_code']]) for call in calls]


@app.get('/funding-sources')
def funding_sources() -> list[dict]:
	return [german_funding.source_status(provider) for provider in german_funding.all_sources()]


def _known_source(provider: str) -> None:
	# Built-in and user-added feeds alike; any other name never reaches a fetch.
	if provider not in german_funding.all_sources():
		raise HTTPException(status_code=422, detail='Unknown funding source.')


@app.post('/funding-sources/{provider}/refresh')
def refresh_funding_source(provider: str) -> dict:
	_known_source(provider)
	return german_funding.refresh(provider)


@app.post('/funding-sources/{provider}/notices/{notice_id}/import', response_model=Call)
def import_funding_notice(provider: str, notice_id: str) -> dict:
	_known_source(provider)
	try:
		call = german_funding.import_notice(provider, notice_id)
	except KeyError as exc:
		raise HTTPException(status_code=404, detail=str(exc)) from None
	except (requests.RequestException, ValueError) as exc:
		raise HTTPException(
			status_code=503, detail=f'Official source import failed: {exc}'
		) from None
	return _enrich(call, load_portfolio())


@app.get('/calls/{topic_code}', response_model=Call)
def read_call(topic_code: str) -> dict:
	call = get_topic(topic_code)
	if call is None:
		raise HTTPException(status_code=404, detail=f'topic {topic_code!r} not found')
	# scored against the same watch-list batch as GET /calls so the rank is
	# comparable; a code outside that batch has no rank to be comparable to.
	fit = score_calls(_watched_calls(), load_portfolio())
	return _enrich(call, load_portfolio(), fit.get(topic_code))


@app.post('/calls/{topic_code}/eligibility', response_model=Eligibility)
def review_eligibility(topic_code: str, body: EligibilityReview) -> dict:
	call = get_topic(topic_code)
	if not call:
		raise HTTPException(status_code=404, detail='Call not found')
	return eligibility.record_review(
		call, load_portfolio(), body.state, body.reason.strip(), body.source.strip()
	)


@app.get('/generation/health')
def generation_health() -> dict:
	return concepts.connection_status()


@app.get('/generation/settings')
def read_generation_settings() -> dict:
	return concepts.connection_settings()


@app.post('/generation/test')
def test_generation_connection(body: generation_settings.Connection) -> dict:
	return concepts.connection_status(body.model_dump())


@app.put('/generation/settings')
def save_generation_settings(body: generation_settings.Connection) -> dict:
	health = concepts.connection_status(body.model_dump())
	if not health['ready']:
		raise HTTPException(status_code=503, detail=health['detail'])
	generation_settings.save(body)
	return {**health, **concepts.connection_settings()}


@app.delete('/generation/settings')
def reset_generation_settings() -> dict:
	generation_settings.reset()
	return concepts.connection_status()


@app.get('/portfolio', response_model=list[Assertion])
def list_portfolio() -> list[dict]:
	return load_portfolio()


@app.get('/researcher-profile')
def researcher_profile() -> dict:
	assertions = load_portfolio()
	evidence = researcher_evidence(assertions)
	return {
		'assertions': assertions,
		'evidence': evidence,
		'context': concepts._format_evidence(evidence),
		'used_count': sum(usable(a) for a in assertions),
		'not_used_count': sum(not usable(a) for a in assertions),
		'connection': concepts.connection_settings(),
	}


@app.post('/portfolio', response_model=Assertion, status_code=201)
def create_portfolio_assertion(body: AssertionCreate) -> dict:
	return add_assertion(
		body.component,
		body.claim,
		body.source,
		body.confidence,
		body.visibility,
		body.phd_award_date.isoformat() if body.phd_award_date else None,
	)


@app.patch('/portfolio/{assertion_id}', response_model=Assertion)
def patch_portfolio_assertion(assertion_id: str, body: AssertionUpdate) -> dict:
	fields = body.model_dump(exclude_unset=True, mode='json')
	try:
		return update_assertion(assertion_id, **fields)
	except KeyError:
		raise HTTPException(
			status_code=404, detail=f'assertion {assertion_id!r} not found'
		) from None


@app.post('/portfolio/import/openalex', response_model=list[Assertion])
def import_openalex(body: OpenAlexImportRequest) -> list[dict]:
	return import_openalex_topics(body.author_id)


@app.post('/concepts/jobs', response_model=ConceptJob, status_code=202)
def create_concept_job(body: ConceptJobCreate) -> dict:
	call = get_topic(body.topic_code)
	if call is None:
		raise HTTPException(status_code=404, detail=f'topic {body.topic_code!r} not found')
	assertions = load_portfolio()
	assessment = eligibility.assess(call, assertions)
	if assessment['state'] != 'eligible' and not body.trial:
		raise HTTPException(status_code=409, detail=assessment['reason'])
	health = concepts.connection_status()
	if not health['ready']:
		raise HTTPException(status_code=503, detail=health['detail'])
	try:
		job = concepts.start_job(call, assertions, retry_of=body.retry_of, trial=body.trial)
	except ValueError as exc:
		raise HTTPException(status_code=400, detail=str(exc)) from None
	if not body.trial:
		decisions.record('develop', 'call', body.topic_code, reason=job['id'])
	return job


@app.get('/concepts/jobs/{job_id}', response_model=ConceptJob)
def read_concept_job(job_id: str) -> dict:
	job = concepts.get_job(job_id)
	if job is None:
		raise HTTPException(status_code=404, detail=f'job {job_id!r} not found')
	return job


@app.get('/concepts/jobs', response_model=list[ConceptJob])
def list_concept_jobs(topic_code: str | None = None) -> list[dict]:
	return concepts.list_jobs(topic_code)


@app.post('/decisions', response_model=Decision, status_code=201)
def create_decision(body: DecisionCreate) -> dict:
	return decisions.record(body.kind, body.target_type, body.target_id, body.reason)


@app.get('/decisions', response_model=list[Decision])
def list_decisions() -> list[dict]:
	return decisions.load()


@app.get('/activity', response_model=list[ActivityItem])
def list_activity() -> list[dict]:
	"""Merges the decision log and concept-job history into one compact,
	newest-first timeline. Deliberately has no "calls considered" / ranking-
	history item type: GET /calls recomputes scores against the current
	portfolio and keeps no record of past rankings, so a historical ranking
	entry here would misrepresent recomputed-now as recorded-then. A
	`develop` decision's `reason` field is the id of the concept_run it
	started, not a human-written rationale -- callers should treat it as a
	cross-reference, not display text.
	"""
	items = [
		{'type': 'decision', 'occurred_at': d['created_at'], 'decision': d, 'concept_run': None}
		for d in decisions.load()
	]
	items += [
		{'type': 'concept_run', 'occurred_at': j['created_at'], 'decision': None, 'concept_run': j}
		for j in concepts.list_jobs()
	]
	items.sort(key=lambda item: item['occurred_at'], reverse=True)
	return items
