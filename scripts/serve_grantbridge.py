"""Run the GrantBridge API on 127.0.0.1, optionally with the watch loop.

    uv run python scripts/serve_grantbridge.py --watch 60

--watch SECONDS runs the autonomous funding watch loop alongside the API.
--ollama-ssh user@host reaches Ollama on another machine through an SSH tunnel.
"""

import argparse
import os
import subprocess
import time

import requests
import uvicorn


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--ollama-ssh', default=os.getenv('GRANTBRIDGE_OLLAMA_SSH'))
	parser.add_argument(
		'--ssh-config',
		help='Optional SSH config file; use /dev/null for a direct host without inherited forwards.',
	)
	parser.add_argument(
		'--ollama-url', default=os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
	)
	# No default here: concepts.py picks it per platform, and importing concepts now
	# would read OLLAMA_* before they are set below.
	parser.add_argument(
		'--model',
		help='Ollama model tag (default: $OLLAMA_MODEL, else gemma4:12b-mlx on Apple silicon, '
		'qwen3:4b-instruct-2507-q4_K_M elsewhere).',
	)
	parser.add_argument('--port', type=int, default=8010)
	parser.add_argument(
		'--watch',
		type=float,
		default=float(os.getenv('GRANTBRIDGE_WATCH_INTERVAL') or 0),
		metavar='SECONDS',
		help='Run the autonomous watch loop every SECONDS (0 = off, the default).',
	)
	parser.add_argument('--tunnel-port', type=int, default=11435)
	args = parser.parse_args()
	tunnel = None
	try:
		if args.ollama_ssh:
			if args.ollama_ssh.startswith('-'):
				parser.error('SSH destination cannot start with a dash')
			tunnel = subprocess.Popen(
				[
					'ssh',
					*(['-F', args.ssh_config] if args.ssh_config else []),
					'-N',
					'-T',
					'-o',
					'BatchMode=yes',
					'-o',
					'ExitOnForwardFailure=yes',
					'-o',
					'ConnectTimeout=12',
					'-o',
					'ServerAliveInterval=15',
					'-o',
					'ServerAliveCountMax=3',
					'-L',
					f'127.0.0.1:{args.tunnel_port}:127.0.0.1:11434',
					args.ollama_ssh,
				]
			)
			args.ollama_url = f'http://127.0.0.1:{args.tunnel_port}'
			os.environ['GRANTBRIDGE_TUNNEL_HOST'] = args.ollama_ssh
			for _ in range(60):
				if tunnel.poll() is not None:
					raise SystemExit('SSH tunnel failed. Check the host and SSH access.')
				try:
					response = requests.get(f'{args.ollama_url}/api/tags', timeout=1)
					response.raise_for_status()
					break
				except requests.RequestException:
					time.sleep(0.25)
			else:
				raise SystemExit('Tunnel opened but the remote Ollama did not respond.')
		os.environ['OLLAMA_BASE_URL'] = args.ollama_url
		if args.model:
			os.environ['OLLAMA_MODEL'] = args.model
		os.environ['GRANTBRIDGE_WATCH_INTERVAL'] = str(args.watch)
		uvicorn.run('grant_recommender.api:app', host='127.0.0.1', port=args.port)
	finally:
		if tunnel:
			tunnel.terminate()
			tunnel.wait(timeout=5)


if __name__ == '__main__':
	main()
