"""Pull the facts a researcher needs out of a call's own text, with every value
tied to a quote that must actually appear in that text.

An imported announcement keeps its deadline, its eligibility rules and its
budget in the prose rather than in fields, which is why a dossier can say "no
deadline recorded" while the text three lines below gives one. This module asks
the model to find those facts and, critically, to quote the span it read them
from. Deterministic code then checks that the quote appears verbatim in the
source. A field whose quote does not verify is dropped, never shown.

That is the same rule the rest of the product runs on: the model proposes, it
does not testify. Nothing here can introduce a deadline the page does not
contain, because a fabricated quote fails containment.

Results are cached against a hash of the source text, so a refinement is
recomputed only when the page itself changes.
"""

import hashlib
import json
import re
from pathlib import Path

from grant_recommender import concepts

ROOT = Path(__file__).resolve().parents[2] / 'data/private/refinements'
# Enough of the page for the facts, comfortably inside the context window. The
# deadline and eligibility block sit near the top of a German announcement.
MAX_SOURCE_CHARS = 20000
# A safety stop, not a budget: it has to cover what the caps below allow (about
# 6,800 characters of JSON) or a model that fills every field is cut off
# mid-JSON. 900 did that to qwen3:4b-instruct-2507 on two German notices; it
# wrote 533-1252 tokens per call, gemma4:12b-mlx 141-628.
NUM_PREDICT = 3072
# A quote has to be substantial enough that matching it means something; "die"
# would verify against any German page. Kept low enough that a real one-line
# fact such as "Abgabetermin: 02.09.2026" still passes. The quote itself is
# shown in the dossier, so the reader can judge the evidence, not just the
# verdict.
MIN_QUOTE_CHARS = 20
# Grammar caps (Ollama enforces maxLength while decoding), so a model still
# writing at the cap is stopped mid-word. finish() drops what the cut left over.
SYNOPSIS_CHARS = 700
VALUE_CHARS = 600

FIELDS = {
	'deadline': 'The date by which a proposal must be submitted.',
	'eligibility': 'Who may apply: career stage, institution type, nationality or consortium.',
	'budget': 'How much money is available, per project or in total.',
	'consortium': 'Whether partners are required, and of what kind.',
	'focus': 'The single sentence that best states what this call funds.',
}

# A synopsis is generated prose, not an extraction, so no quote can back it.
# What can be checked is every figure in it: a fabricated deadline, budget or
# percentage is the damaging kind of error, and those are all numerals. Any
# figure the source does not contain rejects the whole synopsis.
FIGURE = re.compile(r'\d[\d.,:/\u2013-]*\d|\d')
MIN_FIGURE_CHARS = 3

# A German notice states its deadline as 'bis (zum|spätestens) 13. Januar 2027'.
# When the model misses it, code reads it from the first such sentence about a
# proposal or an application: the first date alone was an event, a registration
# or a letter of intent on four of six real DFG notices.
DE_DEADLINE = re.compile(
	r'[^.\n]*\bbis\s+(?:spätestens\s+)?(?:zum\s+)?(\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni'
	r'|Juli|August|September|Oktober|November|Dezember)\s+\d{4})[^.\n]*',
	re.IGNORECASE,
)
DE_PROPOSAL = re.compile(r'antr(?:ag|äge)\b|bewerbung|vorschläge', re.IGNORECASE)

SCHEMA = {
	'type': 'object',
	'properties': {
		'synopsis': {'type': ['string', 'null'], 'maxLength': SYNOPSIS_CHARS},
		**{
			name: {
				# No null branch: offered one, gemma4:12b-mlx took it for all five
				# fields, even for a deadline stated word for word.
				'type': 'object',
				'properties': {
					'value': {'type': 'string', 'maxLength': VALUE_CHARS},
					'quote': {'type': 'string', 'maxLength': 600},
				},
				'required': ['value', 'quote'],
				'additionalProperties': False,
			}
			for name in FIELDS
		},
	},
	'required': ['synopsis', *FIELDS],
	'additionalProperties': False,
}

SYSTEM_PROMPT = '\n\n'.join(
	[
		'You read one funding announcement and report only what it actually says.',
		(
			'For each field return the value and, in `quote`, the exact run of '
			'text from the announcement you read it from. Copy the quote '
			'character for character; do not paraphrase, translate, tidy or '
			'shorten it. The quote is checked against the source and the field '
			'is discarded if it does not match.'
		),
		(
			'Return an empty value and quote for a field the announcement does '
			'not state. A missing fact is a useful answer; an invented one is '
			'not. Never infer a deadline, a budget or an eligibility rule from '
			'what is typical.'
		),
		(
			'`synopsis` is different: three or four sentences, in English, saying '
			'what this call funds, who it is for and what a proposal must deliver. '
			'Write it from the announcement only. Every number, date, sum and '
			'percentage you put in it must appear in the announcement; if you are '
			'not copying a figure from the text, leave the figure out.'
		),
		'Fields: ' + ' '.join(f'{name} = {what}' for name, what in FIELDS.items()),
	]
)


def normalise(text: str) -> str:
	"""Collapse whitespace and case so a quote survives re-wrapping."""
	return re.sub(r'\s+', ' ', str(text)).strip().lower()


def verify(quote: str, source: str) -> bool:
	"""True when the quote is substantial and appears verbatim in the source."""
	needle = normalise(quote)
	return len(needle) >= MIN_QUOTE_CHARS and needle in normalise(source)


def source_hash(call: dict) -> str:
	return hashlib.sha256((call.get('scope_text') or '').encode()).hexdigest()[:16]


def _path(topic_code: str) -> Path:
	safe = re.sub(r'[^A-Za-z0-9._-]', '_', topic_code)[:120]
	return ROOT / f'{safe}.json'


def cached(call: dict) -> dict | None:
	"""A stored refinement, only if it was made from the current page text."""
	path = _path(call['topic_code'])
	if not path.exists():
		return None
	try:
		stored = json.loads(path.read_text())
	except (OSError, json.JSONDecodeError):
		return None
	return stored if stored.get('source_hash') == source_hash(call) else None


def store(result: dict) -> None:
	ROOT.mkdir(parents=True, exist_ok=True)
	path = _path(result['topic_code'])
	temp = path.with_suffix('.tmp')
	temp.write_text(json.dumps(result, ensure_ascii=False, indent=1))
	temp.replace(path)


def figures(text: str) -> set[str]:
	"""Numerals worth checking: dates, sums, years, percentages."""
	return {
		match.group(0)
		for match in FIGURE.finditer(text or '')
		if len(match.group(0)) >= MIN_FIGURE_CHARS
	}


def finish(text: str, limit: int) -> str:
	"""Text that reached its cap without ending a sentence, cut back to its last
	full sentence, or failing that its last full word. Shorter text is only
	stripped: a value such as '13. Januar 2027' must keep what follows its dot."""
	stripped = text.strip()
	if len(text) < limit or not stripped or stripped.endswith(('.', '!', '?')):
		return stripped
	# A dot after a digit is a German date ('13. Januar'), not a sentence end.
	sentences = re.match(r'.*(?<!\d)[.!?](?=\s)', stripped, re.DOTALL)
	return sentences.group(0) if sentences else stripped.rsplit(None, 1)[0] + '…'


def check_synopsis(text: str, source: str) -> tuple[str | None, list[str]]:
	"""Keep the synopsis only if every figure in it is in the source."""
	text = finish(text or '', SYNOPSIS_CHARS)
	if not text:
		return None, []
	haystack = normalise(source)
	invented = sorted(f for f in figures(text) if f.lower() not in haystack)
	return (None, invented) if invented else (text, [])


def check(parsed: dict, source: str) -> tuple[dict, list[str]]:
	"""Keep the fields whose quote verifies; report the ones dropped."""
	kept, dropped = {}, []
	for name in FIELDS:
		field = parsed.get(name)
		if not isinstance(field, dict) or not field.get('value'):
			continue
		if verify(field.get('quote', ''), source):
			kept[name] = {'value': finish(field['value'], VALUE_CHARS), 'quote': field['quote']}
		else:
			dropped.append(name)
	return kept, dropped


def german_deadline(source: str) -> dict | None:
	"""A stated German deadline, quoted by its own sentence so it passes the same
	check as a model's quote."""
	for match in DE_DEADLINE.finditer(source):
		if DE_PROPOSAL.search(match.group(0)) and verify(match.group(0), source):
			return {'value': match.group(1), 'quote': match.group(0).strip()}
	return None


def refine(call: dict, *, on_text=None, use_cache: bool = True) -> dict:
	"""Extract the fields for one call. Returns the stored shape either way."""
	if use_cache:
		hit = cached(call)
		if hit:
			return hit
	source = call.get('scope_text') or ''
	if not source.strip():
		raise ValueError('That call has no text to read.')

	packet = {
		'system_prompt': SYSTEM_PROMPT,
		'user_prompt': '\n\n'.join(
			[
				f'CALL: {call["topic_code"]} — {call.get("title") or ""}',
				'ANNOUNCEMENT TEXT:',
				source[:MAX_SOURCE_CHARS],
			]
		),
	}
	parsed, meta = concepts._ollama_chat(
		{**packet, '_connection': concepts.connection_settings()},
		schema=SCHEMA,
		num_predict=NUM_PREDICT,
		seed=42,
		on_text=on_text,
	)
	# Verified against the whole source, not the truncated prompt: a quote from
	# beyond the cut is still a real quote.
	fields, dropped = check(parsed, source)
	if 'deadline' not in fields and (found := german_deadline(source)):
		fields = {'deadline': found, **fields}
		dropped = [name for name in dropped if name != 'deadline']
	synopsis, invented = check_synopsis(parsed.get('synopsis'), source)
	if invented:
		dropped = [*dropped, 'synopsis']
	result = {
		'topic_code': call['topic_code'],
		'source_hash': source_hash(call),
		'model': meta['request']['model'],
		'synopsis': synopsis,
		'invented_figures': invented,
		'fields': fields,
		'dropped': dropped,
	}
	store(result)
	return result
