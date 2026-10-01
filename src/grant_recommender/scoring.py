"""Real, deterministic "scientific fit" between a portfolio and the watch-list
of calls, using retrieval.py's keyword-overlap and BM25 scorers.

Scored once across the whole watch-list (not per call) so results are
ranked and comparable in the feed -- BM25 IDF is meaningless over a single
doc. BM25 ranks, keyword overlap breaks ties. There is no dense ranker, so
the product needs no embedding-model download.
"""

import re

from grant_recommender.portfolio import usable
from grant_recommender.retrieval import bm25_scores, keyword_overlap_scores


def score_calls(calls: list[dict], portfolio: list[dict]) -> dict[str, dict | None]:
	"""One fit result per call's topic_code, or None for all of them if the
	portfolio has no included claims -- a fit against nothing isn't a 0."""
	if not calls:
		return {}
	# "missing" confidence records the absence of a fact -- scoring against it
	# would score the call against something the researcher doesn't actually
	# have, same reason concepts.py excludes it from generation evidence.
	claims = [a['claim'] for a in portfolio if usable(a)]
	if not claims:
		return dict.fromkeys((c['topic_code'] for c in calls), None)

	query = '. '.join(claims)
	docs = {c['topic_code']: c['scope_text'] for c in calls}
	keyword = keyword_overlap_scores(query, docs)
	bm25 = bm25_scores(query, docs)
	# keyword overlap only breaks BM25 ties, which are routine on a small
	# watch-list with short claims.
	ranked = sorted(docs, key=lambda code: (bm25[code], keyword[code]), reverse=True)
	rank = {code: i + 1 for i, code in enumerate(ranked)}

	return {
		code: {
			'keyword_overlap': round(keyword[code], 4),
			'bm25': round(bm25[code], 4),
			'rank': rank[code],
			'n_portfolio_claims': len(claims),
		}
		for code in docs
	}


def matched_terms(call: dict, portfolio: list[dict]) -> tuple[list[str], int]:
	"""The portfolio words that actually appear in this call, and how many
	distinct words the portfolio contributed.

	Uses the same tokenisation as keyword_overlap_scores, so the pair is the
	arithmetic behind that score rather than a second opinion about it: the
	score is len(matched) / total.
	"""
	claims = [a['claim'] for a in portfolio if usable(a)]
	if not claims:
		return [], 0
	query = set(re.findall(r'\w+', '. '.join(claims).lower()))
	found = query & set(re.findall(r'\w+', (call.get('scope_text') or '').lower()))
	# Longest first: the informative words are the long ones, and a caller
	# showing only the first few should get those.
	return sorted(found, key=lambda word: (-len(word), word)), len(query)


# Words a claim and a funding page share by accident: function words, and the
# ones a claim uses in passing ("over six months", "open-source", "real-time").
COMMON_WORDS = frozenset(
	[
		'a',
		'an',
		'and',
		'are',
		'as',
		'by',
		'for',
		'from',
		'in',
		'is',
		'it',
		'of',
		'on',
		'that',
		'the',
		'this',
		'to',
		'with',
		'studies',
		'study',
		'about',
		'after',
		'also',
		'based',
		'been',
		'before',
		'between',
		'both',
		'during',
		'each',
		'first',
		'have',
		'into',
		'months',
		'more',
		'only',
		'open',
		'other',
		'over',
		'real',
		'such',
		'than',
		'their',
		'there',
		'these',
		'they',
		'those',
		'through',
		'time',
		'under',
		'used',
		'using',
		'very',
		'well',
		'were',
		'what',
		'when',
		'which',
		'will',
		'within',
		'year',
		'years',
	]
)


def content_terms(call: dict, portfolio: list[dict]) -> list[str]:
	"""The informative part of matched_terms: words of four or more letters
	that are not COMMON_WORDS. Longest first, like matched_terms."""
	return [w for w in matched_terms(call, portfolio)[0] if len(w) >= 4 and w not in COMMON_WORDS]
