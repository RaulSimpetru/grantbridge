"""Private researcher portfolio: source-backed assertions with confidence,
visibility, and an easy correction path. Stored under data/private/, which is
gitignored -- the file never leaves the machine, only its usable() claims
reach the model.
data/example_portfolio.json is an invented portfolio to copy there for a demo.
"""

import json
import threading
import uuid
from pathlib import Path
from typing import Literal

DATA = Path(__file__).resolve().parent.parent.parent / 'data'
PORTFOLIO_PATH = DATA / 'private' / 'portfolio.json'
_LOCK = threading.RLock()


def usable(assertion: dict) -> bool:
	return assertion.get('included', True) and assertion.get('confidence') == 'confirmed'


def researcher_evidence(assertions: list[dict]) -> list[dict]:
	"""The exact researcher evidence supplied to every concept-generation packet."""
	return [
		{
			'source_id': f'PROFILE_{i + 1:03d}',
			'section': a['component'],
			'text': f'{a["claim"]} (confidence: {a["confidence"]}; source: {a.get("source") or "none given"})'
			+ (f' PhD award date: {a["phd_award_date"]}.' if a.get('phd_award_date') else '')
			+ (
				' Evidence type: prior proposal / research intention, not proof of funding, '
				'completed work, validated assets, or secured data and partner access.'
				if a['component'] == 'prior_proposals'
				else ''
			),
		}
		for i, a in enumerate(a for a in assertions if usable(a))
	]


Component = Literal[
	'topics_trajectory',
	'methods',
	'software',
	'datasets',
	'infrastructure',
	'preliminary_findings',
	'populations_access',
	'collaborators',
	'career_eligibility',
	'prior_proposals',
]
Confidence = Literal['confirmed', 'inferred', 'needs_review', 'missing']
Visibility = Literal['public', 'private']


class CorruptPortfolio(ValueError):
	"""The portfolio file exists but is not a list of claims, usually after a
	hand edit. Named so the API and the watch-loop log can say which file."""


def load_portfolio() -> list[dict]:
	if not PORTFOLIO_PATH.exists():
		return []
	try:
		assertions = json.loads(PORTFOLIO_PATH.read_text())
	except json.JSONDecodeError as exc:
		raise CorruptPortfolio(f'data/private/portfolio.json is not valid JSON: {exc}') from None
	if not isinstance(assertions, list):
		raise CorruptPortfolio('data/private/portfolio.json must be a JSON list of claims.')
	return assertions


def save_portfolio(assertions: list[dict]) -> None:
	PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
	temp = PORTFOLIO_PATH.with_suffix('.tmp')
	temp.write_text(json.dumps(assertions, indent=2))
	temp.replace(PORTFOLIO_PATH)


def add_assertion(
	component: Component,
	claim: str,
	source: str | None,
	confidence: Confidence,
	visibility: Visibility = 'private',
	phd_award_date: str | None = None,
) -> dict:
	record = {
		'id': uuid.uuid4().hex[:12],
		'component': component,
		'claim': claim,
		'source': source,
		'confidence': confidence,
		'visibility': visibility,
		'included': True,
	}
	if phd_award_date:
		record['phd_award_date'] = phd_award_date
	with _LOCK:
		assertions = load_portfolio()
		assertions.append(record)
		save_portfolio(assertions)
	return record


def update_assertion(assertion_id: str, **fields) -> dict:
	with _LOCK:
		assertions = load_portfolio()
		for record in assertions:
			if record['id'] == assertion_id:
				record.update(fields)
				save_portfolio(assertions)
				return record
	raise KeyError(assertion_id)


def import_openalex_topics(author_id: str, top_n: int = 8) -> list[dict]:
	"""Auto-populate topics_trajectory assertions from a real OpenAlex author
	profile. Always lands as confidence='inferred' -- the researcher reviews
	and confirms or corrects each one, never silently promoted."""
	from grant_recommender.openalex import get_author

	author = get_author(author_id)
	existing_claims = {
		a['claim'] for a in load_portfolio() if a['component'] == 'topics_trajectory'
	}
	ranked = sorted(author.get('topics', []), key=lambda t: -t['count'])
	created = []
	for topic in ranked[:top_n]:
		claim = topic['display_name']
		if claim in existing_claims:
			continue
		created.append(
			add_assertion(
				component='topics_trajectory',
				claim=claim,
				source=f'OpenAlex profile — {topic["count"]} tagged works ({author["display_name"]})',
				confidence='inferred',
			)
		)
	return created
