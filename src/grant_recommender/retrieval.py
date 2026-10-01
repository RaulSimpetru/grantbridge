"""Two query-vs-pool rankers: keyword overlap and BM25.

Each ranker scores one query against a pool of calls and returns every doc's
score (not just top-k), so callers can rank the whole pool.
"""

import re

import bm25s


def keyword_overlap_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
	q = set(re.findall(r'\w+', query.lower()))
	if not q:
		return dict.fromkeys(docs, 0.0)
	return {
		doc_id: len(q & set(re.findall(r'\w+', text.lower()))) / len(q)
		for doc_id, text in docs.items()
	}


def bm25_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
	doc_ids = list(docs)
	corpus_tokens = bm25s.tokenize([docs[d] for d in doc_ids], show_progress=False)
	retriever = bm25s.BM25()
	retriever.index(corpus_tokens, show_progress=False)
	query_tokens = bm25s.tokenize([query], show_progress=False)
	results, scores = retriever.retrieve(query_tokens, k=len(doc_ids), show_progress=False)
	return {doc_ids[i]: float(s) for i, s in zip(results[0], scores[0], strict=True)}
