import pytest

from grant_recommender.ideation import (
	build_critique_packet,
	build_revision_packet,
	concept_set_schema,
	split_call_evidence,
	validate_concept_set,
)


def _valid_concept(number: int, researcher_source='PROFILE_001'):
	return {
		'concept_id': str(number),
		'title': f'Concept {number}',
		'problem': 'Problem',
		'central_hypothesis': 'Hypothesis',
		'call_fit': 'Fit',
		'researcher_fit': 'Fit',
		'objectives': ['Objective one', 'Objective two'],
		'required_assets_and_collaborators': ['Needed asset'],
		'novelty': 'Novelty',
		'principal_feasibility_risk': 'Risk',
		'call_evidence': [{'source_id': 'CALL_SCOPE', 'claim_supported': 'Call claim'}],
		'researcher_evidence': [
			{'source_id': researcher_source, 'claim_supported': 'Capability claim'}
		],
	}


def test_splits_call_sections_into_stable_evidence_ids():
	text = 'Specific Challenge:Need. Scope:Do work. Expected Impact:Make change.'
	passages = split_call_evidence(text)
	assert [row['source_id'] for row in passages] == [
		'CALL_CHALLENGE',
		'CALL_SCOPE',
		'CALL_IMPACT',
	]
	assert passages[1]['text'] == 'Do work.'


def test_validator_rejects_missing_researcher_citation():
	payload = {'concepts': [_valid_concept(i) for i in range(1, 4)]}
	validate_concept_set(
		payload,
		allowed_evidence_ids=['CALL_SCOPE', 'PROFILE_001'],
		require_researcher_evidence=True,
	)
	payload['concepts'][0]['researcher_evidence'] = []
	with pytest.raises(ValueError, match='researcher evidence'):
		validate_concept_set(
			payload,
			allowed_evidence_ids=['CALL_SCOPE', 'PROFILE_001'],
			require_researcher_evidence=True,
		)


def test_schema_structurally_separates_required_citations():
	schema = concept_set_schema(
		allowed_evidence_ids=['CALL_SCOPE', 'PROFILE_001'],
		require_researcher_evidence=True,
	)
	concept = schema['properties']['concepts']['items']
	assert concept['properties']['call_evidence']['items']['properties']['source_id']['enum'] == [
		'CALL_SCOPE'
	]
	assert concept['properties']['researcher_evidence']['minItems'] == 1
	assert concept['properties']['researcher_evidence']['items']['properties']['source_id'][
		'enum'
	] == ['PROFILE_001']

	call_only = concept_set_schema(
		allowed_evidence_ids=['CALL_SCOPE'], require_researcher_evidence=False
	)
	assert (
		call_only['properties']['concepts']['items']['properties']['researcher_evidence'][
			'maxItems'
		]
		== 0
	)


def test_critique_and_revision_build_on_the_draft_packet():
	draft_packet = {
		'pipeline': 'test',
		'job_id': 'job_01',
		'evidence_context': 'CALL EVIDENCE:\n[CALL_SCOPE] scope\nScope',
		'allowed_evidence_ids': ['CALL_SCOPE', 'PROFILE_001'],
	}
	critique = build_critique_packet(draft_packet, {'concepts': []})
	revision = build_revision_packet(draft_packet, {'concepts': []}, {'revision_priorities': []})
	assert 'DRAFT CONCEPT SET' in critique['user_prompt']
	assert 'Use this exact JSON shape' not in critique['user_prompt']
	assert revision['stage'] == 'final'
	assert 'Use this exact JSON shape' in revision['user_prompt']
	assert revision['allowed_evidence_ids'] == ['CALL_SCOPE', 'PROFILE_001']
