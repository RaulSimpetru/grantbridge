"""Prompt construction, output schemas and validation for concept runs.

concepts.py builds the draft packet; the critique and revision builders here
extend it.
"""

import json
import re

N_CONCEPTS = 3

REQUIRED_CONCEPT_FIELDS = {
	'concept_id',
	'title',
	'problem',
	'central_hypothesis',
	'call_fit',
	'researcher_fit',
	'objectives',
	'required_assets_and_collaborators',
	'novelty',
	'principal_feasibility_risk',
	'call_evidence',
	'researcher_evidence',
}

CRITIQUE_SCHEMA = {
	'type': 'object',
	'properties': {
		'concept_critiques': {
			'type': 'array',
			'minItems': N_CONCEPTS,
			'maxItems': N_CONCEPTS,
			'items': {
				'type': 'object',
				'properties': {
					'concept_id': {'type': 'string'},
					'grounding_and_fit': {'type': 'string', 'maxLength': 240},
					'novelty_and_feasibility': {'type': 'string', 'maxLength': 240},
					'revision_action': {'type': 'string', 'maxLength': 240},
				},
				'required': [
					'concept_id',
					'grounding_and_fit',
					'novelty_and_feasibility',
					'revision_action',
				],
				'additionalProperties': False,
			},
		},
		'unsupported_claims': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 160},
		},
		'missing_requirements': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 160},
		},
		'diversity_problems': {
			'type': 'array',
			'maxItems': 3,
			'items': {'type': 'string', 'maxLength': 160},
		},
		'revision_priorities': {
			'type': 'array',
			'maxItems': 5,
			'items': {'type': 'string', 'maxLength': 160},
		},
	},
	'required': [
		'concept_critiques',
		'unsupported_claims',
		'missing_requirements',
		'diversity_problems',
		'revision_priorities',
	],
	'additionalProperties': False,
}


def concept_set_schema(
	*, allowed_evidence_ids: list[str], require_researcher_evidence: bool
) -> dict:
	"""Build a schema that structurally enforces call and researcher citations."""
	call_ids = sorted(
		source_id for source_id in allowed_evidence_ids if source_id.startswith('CALL_')
	)
	researcher_ids = sorted(
		source_id for source_id in allowed_evidence_ids if source_id.startswith('PROFILE_')
	)
	if not call_ids:
		raise ValueError('at least one CALL_* evidence id is required')
	if require_researcher_evidence and not researcher_ids:
		raise ValueError('at least one researcher evidence id is required')

	def citation_schema(source_ids: list[str]) -> dict:
		source_id_schema = {'type': 'string'}
		if source_ids:
			source_id_schema['enum'] = source_ids
		return {
			'type': 'object',
			'properties': {
				'source_id': source_id_schema,
				'claim_supported': {'type': 'string'},
			},
			'required': ['source_id', 'claim_supported'],
			'additionalProperties': False,
		}

	researcher_evidence_schema = {
		'type': 'array',
		'minItems': 1 if require_researcher_evidence else 0,
		'items': citation_schema(researcher_ids),
	}
	if not require_researcher_evidence:
		researcher_evidence_schema['maxItems'] = 0

	return {
		'type': 'object',
		'properties': {
			'concepts': {
				'type': 'array',
				'minItems': N_CONCEPTS,
				'maxItems': N_CONCEPTS,
				'items': {
					'type': 'object',
					'properties': {
						'concept_id': {'type': 'string'},
						'title': {'type': 'string'},
						'problem': {'type': 'string'},
						'central_hypothesis': {'type': 'string'},
						'call_fit': {'type': 'string'},
						'researcher_fit': {'type': 'string'},
						'objectives': {
							'type': 'array',
							'minItems': 2,
							'maxItems': 3,
							'items': {'type': 'string'},
						},
						'required_assets_and_collaborators': {
							'type': 'array',
							'items': {'type': 'string'},
						},
						'novelty': {'type': 'string'},
						'principal_feasibility_risk': {'type': 'string'},
						'call_evidence': {
							'type': 'array',
							'minItems': 1,
							'items': citation_schema(call_ids),
						},
						'researcher_evidence': researcher_evidence_schema,
					},
					'required': sorted(REQUIRED_CONCEPT_FIELDS),
					'additionalProperties': False,
				},
			},
		},
		'required': ['concepts'],
		'additionalProperties': False,
	}


SYSTEM_PROMPT = """You are generating research-project concepts for a funding application.
Use only the evidence in the prompt. Do not use outside knowledge to assert that the
researcher owns an asset, has a collaborator, uses a method, or has access to a population.
Distinguish demonstrated capabilities from capabilities that a future consortium would need.
Return valid JSON only, with no Markdown fences or commentary."""


def split_call_evidence(scope_text: str) -> list[dict[str, str]]:
	"""Split a SEDIA description into stable, citable call passages."""
	markers = [
		('CALL_CHALLENGE', 'specific_challenge', 'Specific Challenge:'),
		('CALL_SCOPE', 'scope', 'Scope:'),
		('CALL_IMPACT', 'expected_impact', 'Expected Impact:'),
		('CALL_PRIORITIES', 'cross_cutting_priorities', 'Cross-cutting Priorities:'),
	]
	matches = []
	for source_id, section, marker in markers:
		match = re.search(re.escape(marker), scope_text, flags=re.IGNORECASE)
		if match:
			matches.append((match.start(), match.end(), source_id, section))
	if not matches:
		return [{'source_id': 'CALL_TEXT', 'section': 'call_text', 'text': scope_text.strip()}]

	matches.sort()
	passages = []
	if scope_text[: matches[0][0]].strip():
		passages.append(
			{
				'source_id': 'CALL_PREAMBLE',
				'section': 'preamble',
				'text': scope_text[: matches[0][0]].strip(),
			}
		)
	for i, (_start, content_start, source_id, section) in enumerate(matches):
		content_end = matches[i + 1][0] if i + 1 < len(matches) else len(scope_text)
		passages.append(
			{
				'source_id': source_id,
				'section': section,
				'text': scope_text[content_start:content_end].strip(),
			}
		)
	return passages


def _output_contract(call_ids: list[str], researcher_ids: list[str]) -> str:
	call_example = call_ids[0]
	if researcher_ids:
		researcher_rule = (
			'Each concept must put at least one supplied researcher citation in '
			'researcher_evidence.'
		)
		researcher_example = f'[{{"source_id": "{researcher_ids[0]}", "claim_supported": "..."}}]'
	else:
		researcher_rule = (
			'No researcher evidence is available. Say so in researcher_fit, do not invent any, '
			'and return an empty researcher_evidence array.'
		)
		researcher_example = '[]'
	return f"""Produce exactly {N_CONCEPTS} meaningfully different concepts. {researcher_rule}
Each concept must put at least one supplied call citation in call_evidence. Use this exact JSON shape:
{{
  "concepts": [
    {{
      "concept_id": "1",
      "title": "...",
      "problem": "...",
      "central_hypothesis": "...",
      "call_fit": "...",
      "researcher_fit": "...",
      "objectives": ["...", "..."],
      "required_assets_and_collaborators": ["..."],
      "novelty": "...",
      "principal_feasibility_risk": "...",
      "call_evidence": [{{"source_id": "{call_example}", "claim_supported": "..."}}],
      "researcher_evidence": {researcher_example}
    }}
  ]
}}
Use two or three objectives per concept. Required but unproven assets or collaborators must be
worded as needs, not existing facts. Favor substantive diversity over cosmetic rewording."""


def build_critique_packet(generation_packet: dict, draft: dict) -> dict:
	"""Build the grounding, feasibility, and diversity critique stage."""
	return {
		**{key: generation_packet[key] for key in ('pipeline', 'job_id')},
		'stage': 'critique',
		'system_prompt': SYSTEM_PROMPT,
		'user_prompt': (
			generation_packet['evidence_context']
			+ '\n\nDRAFT CONCEPT SET:\n'
			+ json.dumps(draft, ensure_ascii=False)
			+ '\n\nAudit evidence support, call fit, capability fit, novelty, feasibility, and '
			+ 'overlap. Be terse: never restate a concept, use one short sentence per '
			+ 'concept-level field, and list only the highest-priority global issues. Return JSON with keys '
			+ 'concept_critiques (one per concept), unsupported_claims, missing_requirements, '
			+ 'diversity_problems, and revision_priorities. Do not rewrite the concepts yet.'
		),
		'output_schema_version': 'concept-critique-v1',
	}


def build_revision_packet(generation_packet: dict, draft: dict, critique: dict) -> dict:
	"""Build the final revision stage using the original evidence."""
	return {
		**{key: generation_packet[key] for key in ('pipeline', 'job_id')},
		'stage': 'final',
		'system_prompt': SYSTEM_PROMPT,
		'user_prompt': (
			generation_packet['evidence_context']
			+ '\n\nDRAFT CONCEPT SET:\n'
			+ json.dumps(draft, ensure_ascii=False)
			+ '\n\nCRITIQUE:\n'
			+ json.dumps(critique, ensure_ascii=False)
			+ '\n\nRevise the concepts to address the critique. Return only the concept-set-v2 '
			+ 'JSON. Preserve three substantively different concepts and remove or qualify '
			+ 'every unsupported claim.\n\n'
			+ _output_contract(
				[
					source_id
					for source_id in generation_packet['allowed_evidence_ids']
					if source_id.startswith('CALL_')
				],
				[
					source_id
					for source_id in generation_packet['allowed_evidence_ids']
					if source_id.startswith('PROFILE_')
				],
			)
		),
		'allowed_evidence_ids': generation_packet['allowed_evidence_ids'],
		'require_researcher_evidence': True,
		'output_schema_version': 'concept-set-v2',
	}


def validate_concept_set(
	payload: dict,
	*,
	allowed_evidence_ids: list[str],
	require_researcher_evidence: bool,
) -> None:
	"""Reject malformed or uncited final outputs before they enter review."""
	concepts = payload.get('concepts') if isinstance(payload, dict) else None
	if not isinstance(concepts, list) or len(concepts) != N_CONCEPTS:
		raise ValueError(f'output must contain exactly {N_CONCEPTS} concepts')
	call_allowed = {
		source_id for source_id in allowed_evidence_ids if source_id.startswith('CALL_')
	}
	researcher_allowed = {
		source_id for source_id in allowed_evidence_ids if source_id.startswith('PROFILE_')
	}
	titles = set()
	for concept in concepts:
		if not isinstance(concept, dict):
			raise TypeError('each concept must be a JSON object')
		missing = REQUIRED_CONCEPT_FIELDS - set(concept)
		if missing:
			raise ValueError(f'concept missing fields: {sorted(missing)}')
		if not isinstance(concept['objectives'], list) or not 2 <= len(concept['objectives']) <= 3:
			raise ValueError('each concept must have two or three objectives')
		if not isinstance(concept['title'], str):
			raise TypeError('concept title must be a string')
		title = concept['title'].strip().casefold()
		if not title or title in titles:
			raise ValueError('concept titles must be non-empty and distinct')
		titles.add(title)
		for field, allowed in (
			('call_evidence', call_allowed),
			('researcher_evidence', researcher_allowed),
		):
			evidence = concept[field]
			if not isinstance(evidence, list):
				raise TypeError(f'{field} must be an array')
			if any(
				not isinstance(citation, dict)
				or set(citation) != {'source_id', 'claim_supported'}
				or not isinstance(citation.get('source_id'), str)
				or not isinstance(citation.get('claim_supported'), str)
				for citation in evidence
			):
				raise ValueError(
					f'each {field} citation must contain only string source_id and '
					'claim_supported fields'
				)
			unknown = {citation['source_id'] for citation in evidence} - allowed
			if unknown:
				raise ValueError(f'unknown {field} ids: {sorted(unknown)}')
		if not concept['call_evidence']:
			raise ValueError('each concept must cite call evidence')
		if require_researcher_evidence and not concept['researcher_evidence']:
			raise ValueError('each concept must cite researcher evidence')
		if not require_researcher_evidence and concept['researcher_evidence']:
			raise ValueError('researcher evidence must be empty when none was supplied')
