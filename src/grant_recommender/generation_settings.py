"""The researcher's saved Ollama connection, kept in data/private."""

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

SETTINGS_PATH = Path(__file__).resolve().parents[2] / 'data/private/generation_settings.json'
_LOCK = threading.Lock()


class Connection(BaseModel):
	base_url: str = Field(min_length=1, max_length=2000)
	model: str = Field(min_length=1, max_length=200)

	@model_validator(mode='after')
	def validate_connection(self):
		self.base_url = self.base_url.strip().rstrip('/')
		self.model = self.model.strip()
		url = urlsplit(self.base_url)
		if (
			url.scheme not in ('http', 'https')
			or not url.hostname
			or url.username is not None
			or url.password is not None
			or url.query
			or url.fragment
			or any(c.isspace() or ord(c) < 32 for c in self.base_url)
		):
			raise ValueError('Use an HTTP(S) server URL without credentials, query or fragment.')
		if url.port is not None and not 1 <= url.port <= 65535:
			raise ValueError('Invalid server port')
		if not self.model:
			raise ValueError('Choose a model')
		return self


def current(base_url: str, model: str) -> dict:
	with _LOCK:
		if SETTINGS_PATH.exists():
			config = Connection.model_validate_json(SETTINGS_PATH.read_text()).model_dump()
			source = 'saved'
		else:
			config = {'base_url': base_url.rstrip('/'), 'model': model}
			source = 'launcher'
	return {
		**config,
		'source': source,
		'tunnel_host': os.getenv('GRANTBRIDGE_TUNNEL_HOST')
		if config['base_url'] == base_url.rstrip('/')
		else None,
	}


def save(connection: Connection) -> None:
	with _LOCK:
		SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
		temp = SETTINGS_PATH.with_suffix('.tmp')
		temp.write_text(json.dumps(connection.model_dump()))
		temp.replace(SETTINGS_PATH)


def reset() -> None:
	with _LOCK:
		SETTINGS_PATH.unlink(missing_ok=True)
