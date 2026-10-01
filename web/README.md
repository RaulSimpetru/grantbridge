# GrantBridge web

The browser UI for GrantBridge: React 19, TypeScript and Vite. It is a client of the local
FastAPI server (`src/grant_recommender/api.py`), and every request goes through `src/api.ts`.
Product overview and setup: [../README.md](../README.md). Needs Node 22.12 or later.

## Run it against the API

From the repository root, start the API with the watch loop, one tick every 60 s. It listens on
127.0.0.1:8010.

```sh
uv sync
uv run python scripts/serve_grantbridge.py --watch 60
```

In a second terminal:

```sh
cd web
npm ci
npm run dev
```

Open http://127.0.0.1:5173. The dev server always binds 127.0.0.1:5173 and exits if the port
is busy instead of moving to another one: the API's origin check admits the UI only from port
5173.

The UI calls `http://127.0.0.1:8010` unless `VITE_API_BASE_URL` is set when the dev server (or
the build) starts, for example for an API started with `--port 8099`:

```sh
VITE_API_BASE_URL=http://127.0.0.1:8099 npm run dev
```

If the API runs on another machine, forward its port and keep the defaults:

```sh
ssh -N -L 8010:127.0.0.1:8010 user@host
```

Browsing works without a model. Generation needs Ollama, and any endpoint works: start the API
with `--ollama-url URL`, or with `--ollama-ssh user@host` to have the launcher open an
SSH tunnel. **Connection settings** in the app tests and switches the endpoint and model while
the API runs.

## Build, test, lint

```sh
npm run build    # type-check (tsc -b), then bundle into dist/
npm run preview  # serve dist/ on 127.0.0.1:5173, the origin the API accepts
npm test         # node:test unit tests in tests/, no browser needed
npm run lint     # oxlint
npm run format   # prettier, configured under "prettier" in package.json
```

Backend tests run from the repository root with `uv run pytest -q`.
