# pal-chat

A multi-agent group chat web project focused on making AI conversations feel
closer to a human group chat.

The current product scope, delivery boundaries, team responsibilities, risks,
and acceptance criteria are recorded in
[docs/project-charter.md](docs/project-charter.md).

The proposed MVP component boundaries, topic and relay mechanics, data and API
contracts, technology decisions, and delivery sequence are recorded in
[docs/architecture.md](docs/architecture.md).

## Development workflow

- Make scoped changes on a task branch and merge them through a pull request.
- Keep each commit focused and include the related issue key in the PR title,
  body, or branch name.
- Never commit API keys, access tokens, model credentials, or user chat data.

## Backend foundation

This repository now includes the initial backend contract and schema layer
under `apps/server`:

- FastAPI app with `/health/live`, `/health/ready`, conversation/message/run
  endpoints, timeline queries, and an SSE replay endpoint.
- SQLAlchemy models plus Alembic schema `20260728_0001` for conversations,
  messages, topics, runs, decisions, snapshots, model calls, events, and
  request idempotency.
- SQLite WAL initialization on startup.
- A programmable fake `ModelGateway` for deterministic tests.

## Local setup

### One-command debug startup

Run:

```bash
./scripts/dev.sh
```

The script will:

- create `.env` from `.env.example` when missing;
- install backend (`uv`) and frontend (`npm`) dependencies;
- run Alembic migrations;
- start the backend at `http://127.0.0.1:8000`;
- start the frontend debug console at `http://127.0.0.1:5173`.

Useful local entry points:

- Frontend debug console: `http://127.0.0.1:5173`
- Backend live health: `http://127.0.0.1:8000/health/live`
- Backend ready health: `http://127.0.0.1:8000/health/ready`
- OpenAPI docs: `http://127.0.0.1:8000/docs`

The frontend is intentionally minimal: it verifies that the browser can reach
the backend, create a conversation, send a message, and inspect the resulting
records.

### Manual setup

1. Install dependencies:

```bash
uv sync --group dev
npm --prefix apps/web install
```

2. Copy `.env.example` to `.env` and set values if needed. By default the app
   uses a local SQLite database under `${XDG_DATA_HOME:-~/.local/share}/pal-chat`.

3. Run migrations:

   ```bash
   uv run alembic upgrade head
   ```

4. Start the API:

   ```bash
   uv run uvicorn pal_chat_server.main:app --app-dir apps/server/src --reload
   ```

5. Start the frontend:

   ```bash
   npm --prefix apps/web run dev -- --host 127.0.0.1 --port 5173
   ```

6. Run checks:

   ```bash
   uv run ruff check .
   uv run mypy apps/server/src tests
   uv run pytest
   npm --prefix apps/web run build
   ```
