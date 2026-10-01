"""Transparent evidence matching, distinct from calibrated fit or eligibility."""

from grant_recommender.ideation import split_call_evidence
from grant_recommender.portfolio import usable
from grant_recommender.retrieval import keyword_overlap_scores


def build_dossier(call: dict, portfolio: list[dict]) -> dict:
	passages = split_call_evidence(call['scope_text'])
	claims = [a for a in portfolio if usable(a)]
	pairs = []
	for a in claims:
		scores = keyword_overlap_scores(a['claim'], {p['source_id']: p['text'] for p in passages})
		if not scores:
			continue
		best = max(scores, key=scores.get)
		if scores[best] > 0:
			p = next(p for p in passages if p['source_id'] == best)
			pairs.append(
				{
					'assertion_id': a.get('id'),
					'claim': a['claim'],
					'component': a['component'],
					'source': a.get('source'),
					'passage': p,
					'overlap': round(scores[best], 3),
				}
			)
	capabilities = [
		a
		for a in claims
		if a['component']
		in (
			'methods',
			'software',
			'datasets',
			'infrastructure',
			'populations_access',
			'collaborators',
		)
	]
	cap_matches = [
		p
		for p in pairs
		if p['component']
		in (
			'methods',
			'software',
			'datasets',
			'infrastructure',
			'populations_access',
			'collaborators',
		)
	]
	trajectory = [a for a in claims if a['component'] == 'topics_trajectory']
	strategic_matches = [p for p in pairs if p['component'] == 'topics_trajectory']
	gaps = [
		a['claim']
		for a in portfolio
		if a.get('included', True) and a.get('confidence') == 'missing'
	]
	unreviewed = sum(
		a.get('included', True) and a.get('confidence') in ('inferred', 'needs_review')
		for a in portfolio
	)
	return {
		'passages': passages,
		'evidence_pairs': sorted(pairs, key=lambda p: -p['overlap']),
		'capability': {
			'label': f'{len(cap_matches)} of {len(capabilities)} claims overlap'
			if capabilities
			else 'Needs evidence',
			'explanation': 'Keyword overlap with confirmed methods and assets; this does not certify that all call requirements are covered.',
		},
		'strategic': {
			'label': f'{len(strategic_matches)} of {len(trajectory)} trajectory claims overlap'
			if trajectory
			else 'Needs trajectory evidence',
			'explanation': 'Alignment with confirmed research trajectory. Add your research direction in Portfolio to assess this.',
		},
		'risk': {
			'label': f'{len(gaps)} declared gaps · {unreviewed} claims awaiting review',
			'explanation': 'Observed evidence gaps, not a prediction of funding success. Call-specific feasibility is examined in concept critique.',
		},
		'gaps': gaps,
	}
