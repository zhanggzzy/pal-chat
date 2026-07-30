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

This repository now includes the phase 1 experiment skeleton under `apps/server`:

- FastAPI app with `/health/live`, `/health/ready`, `/api/v1/bootstrap`,
  credential, profile-template, and conversation lifecycle endpoints.
- SQLAlchemy catalog schema plus Alembic revision `20260729_0001` for
  profile templates, credential metadata, and conversation catalog records.
- `catalog.sqlite` plus per-experiment archive layout with `manifest.json`,
  `observations.ndjson`, and `transcript.sqlite` bootstrap.
- Static module registry, compatibility validation, credential masking,
  virtual clock support, and a scripted model adapter for deterministic tests.

## Local setup

### Stage 7 reproducible runtime

Version locks:

- Python `3.12` via `.python-version`
- Node `22` via `.nvmrc`

Preferred stage-7 entry points:

```bash
./scripts/bootstrap.sh
./scripts/start.sh
./scripts/health.sh
./scripts/stop.sh
```

PowerShell equivalents are also shipped:

- `./scripts/bootstrap.ps1`
- `./scripts/start.ps1`
- `./scripts/health.ps1`
- `./scripts/stop.ps1`

The stage-7 scripts use a fresh runtime root at `var/runtime-data/current`.
Older local roots such as `var/data` or `${XDG_DATA_HOME:-~/.local/share}/pal-chat`
are kept for inspection/rollback only; this repo does not migrate old data.

### Legacy debug startup

The older debug helper remains available for local fallback only:

```bash
./scripts/dev.sh
```

It still uses the older `var/data` convention and should not be treated as the
primary stage-7 cutover path.

Useful local entry points after `./scripts/start.sh`:

- Frontend debug console: `http://127.0.0.1:5173`
- Backend live health: `http://127.0.0.1:8000/health/live`
- Backend ready health: `http://127.0.0.1:8000/health/ready`
- OpenAPI docs: `http://127.0.0.1:8000/docs`

The frontend is intentionally minimal: it verifies that the browser can reach
the backend, create a conversation, send a message, and inspect the resulting
records.

### Persistent WSL handoff

For a handoff where the services should keep running after the shell exits:

```bash
./scripts/serve-wsl.sh
```

The script will:

- bind the frontend and backend to `0.0.0.0` inside WSL so Windows can reach
  them through a dedicated `wsl.exe` supervisor process;
- write PID files under `.dev/wsl-service/`;
- write persistent logs to `var/log/api.log` and `var/log/web.log`.

Stop the services with:

```bash
./scripts/stop-wsl.sh
```

The intended Windows entry points are:

- Frontend: `http://localhost:5173`
- Backend health: `http://localhost:8000/health/ready`
- OpenAPI: `http://localhost:8000/docs`

### Manual setup

1. Install dependencies:

```bash
uv sync --group dev
npm --prefix apps/web install
```

2. Copy `.env.example` to `.env` and set values if needed. The stage-7 scripts
   default to `var/runtime-data/current`; older local roots stay read-only.

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

## Cutover and rollback

- Runtime root/cutover guide: [docs/cutover-and-rollback.md](docs/cutover-and-rollback.md)
- Gate baseline and remaining stage-7 gaps: [docs/stage7-gate-matrix.md](docs/stage7-gate-matrix.md)
