from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from grant_recommender import concepts
from grant_recommender import generation_settings as settings
from grant_recommender.api import app


@pytest.fixture
def client(monkeypatch, tmp_path):
	monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'connection.json')
	monkeypatch.setattr(concepts, 'OLLAMA_BASE_URL', 'http://127.0.0.1:11435')
	monkeypatch.setattr(concepts, 'OLLAMA_MODEL', 'original')
	monkeypatch.setenv('GRANTBRIDGE_TUNNEL_HOST', 'user@host')
	response = Mock()
	response.json.return_value = {'models': [{'name': 'original'}, {'name': 'replacement'}]}
	monkeypatch.setattr(requests, 'get', Mock(return_value=response))
	return TestClient(app)


def test_test_does_not_save_and_reports_models_and_timing(client):
	before = client.get('/generation/settings').json()
	result = client.post(
		'/generation/test',
		json={
			'base_url': 'https://ollama.example.org/',
			'model': 'replacement',
		},
	).json()
	assert result['ready']
	assert result['base_url'] == 'https://ollama.example.org'
	assert result['models'] == ['original', 'replacement']
	assert result['checked_at'] and result['latency_ms'] >= 0
	assert client.get('/generation/settings').json() == before
	assert not settings.SETTINGS_PATH.exists()


def test_save_persists_and_reset_restores_tunnel(client):
	config = {'base_url': 'https://ollama.example.org', 'model': 'replacement'}
	assert client.put('/generation/settings', json=config).status_code == 200
	assert (
		settings.Connection.model_validate_json(settings.SETTINGS_PATH.read_text()).model_dump()
		== config
	)
	active = client.get('/generation/settings').json()
	assert active == {**config, 'source': 'saved', 'tunnel_host': None}
	assert client.get('/generation/health').json()['model'] == 'replacement'
	reset = client.delete('/generation/settings').json()
	assert reset['model'] == 'original'
	assert reset['tunnel_host'] == 'user@host'
	assert not settings.SETTINGS_PATH.exists()


def test_unavailable_model_does_not_replace_connection(client):
	settings.save(settings.Connection(base_url='https://working.example.org', model='original'))
	response = client.put(
		'/generation/settings',
		json={
			'base_url': 'https://new.example.org',
			'model': 'not-installed',
		},
	)
	assert response.status_code == 503
	assert 'not installed' in response.json()['detail']
	assert client.get('/generation/settings').json()['base_url'] == 'https://working.example.org'


@pytest.mark.parametrize(
	'url',
	[
		'file:///etc/passwd',
		'ftp://host',
		'http://user:secret@host',
		'http://host?key=secret',
		'http://host#fragment',
		'http://host:99999',
		'http://',
		'http://bad host',
	],
)
def test_invalid_server_urls_rejected(client, url):
	assert (
		client.post('/generation/test', json={'base_url': url, 'model': 'original'}).status_code
		== 422
	)
	requests.get.assert_not_called()


def test_foreign_browser_origin_cannot_change_or_probe_server(client):
	headers = {'Origin': 'https://untrusted.example.org'}
	config = {'base_url': 'http://127.0.0.1:11435', 'model': 'original'}
	assert client.post('/generation/test', json=config, headers=headers).status_code == 403
	assert client.put('/generation/settings', json=config, headers=headers).status_code == 403
	assert client.delete('/generation/settings', headers=headers).status_code == 403
	requests.get.assert_not_called()


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_browser_preflight_allows_settings_actions(client, method):
	response = client.options(
		'/generation/settings',
		headers={
			'Origin': 'http://localhost:5173',
			'Access-Control-Request-Method': method,
			'Access-Control-Request-Headers': 'content-type',
		},
	)
	assert response.status_code == 200
	assert method in response.headers['access-control-allow-methods']


@pytest.mark.parametrize(
	'failure, expected',
	[
		(requests.Timeout(), 'timed out'),
		(requests.ConnectionError(), 'Cannot reach'),
		(ValueError(), 'valid Ollama'),
	],
)
def test_check_failures_are_visible_and_do_not_save(client, failure, expected):
	requests.get.side_effect = failure
	result = client.post(
		'/generation/test',
		json={
			'base_url': 'http://127.0.0.1:11435',
			'model': 'original',
		},
	).json()
	assert result['ready'] is False
	assert expected in result['detail']
	assert result['checked_at']
	assert not settings.SETTINGS_PATH.exists()


def test_malformed_model_list_has_actionable_error(client):
	requests.get.return_value.json.return_value = {'models': None}
	result = client.get('/generation/health').json()
	assert not result['ready']
	assert 'valid Ollama' in result['detail']


def test_chat_uses_job_snapshot_not_changed_settings(client, monkeypatch):
	settings.save(settings.Connection(base_url='https://new.example.org', model='replacement'))
	response = Mock()
	response.status_code = 200
	response.json.return_value = {'message': {'content': '{}'}}
	post = Mock(return_value=response)
	monkeypatch.setattr(requests, 'post', post)
	concepts._ollama_chat(
		{
			'_connection': {'base_url': 'http://127.0.0.1:11435', 'model': 'original'},
			'system_prompt': 'test',
			'user_prompt': 'test',
		},
		schema={},
		num_predict=1,
		seed=42,
	)
	assert post.call_args.args[0] == 'http://127.0.0.1:11435/api/chat'
	assert post.call_args.kwargs['json']['model'] == 'original'
