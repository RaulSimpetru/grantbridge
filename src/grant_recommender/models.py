"""The local model catalogue: what Ollama has, and pulling what it does not.

Thin on purpose. Listing is a read; pulling is a large download, so it reports
progress and is only ever started by an explicit choice.
"""

import json
import re

import requests

from grant_recommender import concepts, generation_settings

TIMEOUT_S = 10
# Ollama leaves details.parameter_size empty for safetensors/MLX builds while
# filling it for GGUF, so an MLX tag reports nothing where a GGUF tag reports
# its size. The tag itself carries the size the publisher chose, so read
# it from there when the metadata is silent.
NAME_PARAMETERS = re.compile(r'(?<![.\d])(\d{1,3}(?:\.\d)?)\s*b\b', re.IGNORECASE)
PULL_TIMEOUT_S = 60 * 60


def base_url() -> str:
	return concepts.connection_settings()['base_url']


def active() -> str:
	return concepts.connection_settings()['model']


def parameters_from_name(name: str) -> str:
	match = NAME_PARAMETERS.search(name)
	return f'{match.group(1)}B' if match else ''


def installed() -> list[dict]:
	"""Models Ollama already has, largest detail first: name, size, family."""
	response = requests.get(f'{base_url()}/api/tags', timeout=TIMEOUT_S, allow_redirects=False)
	response.raise_for_status()
	found = []
	for entry in response.json().get('models', []):
		name = entry.get('name')
		if not isinstance(name, str):
			continue
		details = entry.get('details') or {}
		found.append(
			{
				'name': name,
				'size': int(entry.get('size') or 0),
				'family': details.get('family') or '',
				'parameters': details.get('parameter_size') or parameters_from_name(name),
				'installed': True,
			}
		)
	return sorted(found, key=lambda m: m['name'])


def human_size(size: int) -> str:
	if not size:
		return ''
	gigabytes = size / 1024 / 1024 / 1024
	return f'{gigabytes:.1f} GB' if gigabytes >= 1 else f'{size / 1024 / 1024:.0f} MB'


def select(name: str) -> None:
	"""Make `name` the model every part of the product asks Ollama for."""
	generation_settings.save(
		generation_settings.Connection(base_url=base_url(), model=name.strip())
	)


def pull(name: str, on_progress=None) -> None:
	"""Download a model. Streams status lines so a caller can show progress.

	Ollama reports bytes for the layer in flight, not the whole pull, so the
	percentage here is per layer and the status text is what actually tells a
	reader where they are.
	"""
	report = on_progress or (lambda _message: None)
	with requests.post(
		f'{base_url()}/api/pull',
		json={'model': name.strip(), 'stream': True},
		timeout=PULL_TIMEOUT_S,
		stream=True,
		allow_redirects=False,
	) as response:
		response.raise_for_status()
		for line in response.iter_lines():
			if not line:
				continue
			try:
				event = json.loads(line)
			except json.JSONDecodeError:
				continue
			if event.get('error'):
				raise RuntimeError(event['error'])
			status = event.get('status', '')
			completed, total = event.get('completed'), event.get('total')
			if completed and total:
				report(f'{status} · {completed * 100 // total}% of {human_size(total)}')
			elif status:
				report(status)
