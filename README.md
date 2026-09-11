# rag-codebase-chat

Chat with your GitHub codebase. Connect a repository through a GitHub App,
it gets indexed (AST chunking → embeddings → Qdrant), and then you can ask
questions in natural language and get answers backed by links to the
actual files and lines.

Architecture and the step-by-step development plan — see [`PLAN.md`](PLAN.md).
Code conventions — see [`CLAUDE.md`](CLAUDE.md).

## Stack and models used

| Purpose | Service/model |
|---|---|
| API | FastAPI, Python 3.12 |
| Frontend | React + Vite + TypeScript + Tailwind (SPA) |
| Metadata | PostgreSQL 16 |
| Vector store | Qdrant |
| Indexing queue | Redis + RQ |
| Code embeddings | Voyage AI, model `voyage-code-3` |
| Reranking | Voyage AI, model `rerank-2` |
| Answer generation | OpenAI or Anthropic — configurable, see below |
| Code access | GitHub App (Tarball API + Compare API), no `git clone` |

Answer generation is pluggable (`api/services/answer.py`): `ANSWER_PROVIDER`
picks `openai` (default, `gpt-4o`) or `anthropic` (`claude-sonnet-5`),
`ANSWER_MODEL` overrides the model for whichever provider is active. Only
the active provider's API key is required.

## Before you install

1. **Docker Desktop** (or a compatible docker + docker compose setup).
2. **Your own GitHub App** — this project talks to a GitHub App you
   register yourself, not a shared one: https://github.com/settings/apps/new
   (or under an organization). When creating it, set:
   - **Callback URL** (for OAuth login): `http://localhost:8000/api/v1/auth/callback`
   - **Setup URL** (under "Post installation" / "Identifying and
     authorizing users"): `http://localhost:8000/api/v1/github/callback`,
     with **"Redirect on update" checked**. This one is **required**,
     not optional — without it, GitHub doesn't redirect anywhere after
     install and just shows its own confirmation page; our backend never
     learns the `installation_id`, so nothing gets indexed. (`GET
     /api/v1/github/install` only handles the first half — sending the
     browser *to* GitHub's install screen; the Setup URL is what brings
     it *back*.)
   - **Webhook URL**: `http://localhost:8000/api/v1/webhooks/github`
     (for local development without a public address, proxy it through
     [smee.io](https://smee.io) or `ngrok`)
   - **Webhook secret** — make one up and save it, you'll need it in `.env`
   - **Permissions**: Repository → Contents: Read-only, Metadata: Read-only
   - **Permissions**: Account → Email addresses: Read-only — **required**
     for login: the app reads the user's primary email via `/user/emails`
     when it isn't public on their profile. Without this permission the
     callback fails with a 403 from GitHub (`github_email_permission_missing`).
   - **Subscribe to events**: Push
   - **Identifying and authorizing users** → enable "Request user
     authorization (OAuth) during installation" — login won't work without it.
     Note: GitHub ignores any OAuth `scope` for GitHub Apps — access is
     entirely determined by the Permissions configured here, not by scopes
     requested at login time.
   - After creating the app, generate a **private key** (downloads a
     `.pem` file) and note down the **App ID**, **Client ID**,
     **Client secret**, and the app's **slug** (the last part of
     `github.com/apps/<slug>`)
3. **API keys**:
   - [Anthropic API key](https://console.anthropic.com/) — answer generation
   - [Voyage AI API key](https://dashboard.voyageai.com/) — embeddings and reranking

## Install and run

```bash
cp .env.example .env
```

Fill in `.env` with real values (see the variables table below).

The GitHub App private key (downloaded when you generated it) needs to
land inside the `api` and `worker` containers at the path
`GITHUB_APP_PRIVATE_KEY_PATH` points to — by default
`/run/secrets/github_app_private_key.pem`. `docker-compose.override.yml`
(git-ignored, already in the repo) mounts it there for you:

```bash
mkdir -p secrets
mv ~/Downloads/your-app.*.private-key.pem secrets/github_app_private_key.pem
```

That's it — no changes needed to `.env` or the override file, since the
mounted path already matches the default `GITHUB_APP_PRIVATE_KEY_PATH`.
If you name or place the file differently, edit the path in
`docker-compose.override.yml` to match.

```bash
docker compose up --build
```

The API comes up on `http://localhost:8000`, Swagger docs at
`http://localhost:8000/docs`, and the web UI at `http://localhost:3000`.
Postgres is published on host port **5433** (not 5432 — to avoid
clashing with a locally installed Postgres), Qdrant on 6333/6334, Redis
on 6379.

Postgres/Qdrant/Redis data lives in `./data/` (bind-mounted, git-ignored)
rather than a Docker-managed named volume — it survives `docker compose
down` and rebuilds, and its path doesn't depend on which context/shell
`docker compose` gets invoked from (named volumes are keyed by compose
project name, which can silently drift). To fully reset local state:
`docker compose down && rm -rf data/*/`.

Check everything is up:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

## Environment variables

| Variable | Where to get it | Required |
|---|---|---|
| `GITHUB_APP_ID` | GitHub App settings page | yes |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the downloaded App `.pem` key | yes |
| `GITHUB_APP_WEBHOOK_SECRET` | You make it up when creating the App | yes |
| `GITHUB_APP_SLUG` | Part of the app's URL (`github.com/apps/<slug>`) | yes |
| `GITHUB_APP_CLIENT_ID` | GitHub App settings, OAuth credentials | yes |
| `GITHUB_APP_CLIENT_SECRET` | GitHub App settings, OAuth credentials | yes |
| `SESSION_SECRET_KEY` | Any random string (`openssl rand -hex 32`) | yes |
| `FRONTEND_URL` | Where to redirect the browser after login/install | yes |
| `ANSWER_PROVIDER` | `openai` (default) or `anthropic` | no |
| `ANSWER_MODEL` | Overrides the default model for the active provider | no |
| `OPENAI_API_KEY` | platform.openai.com — only if `ANSWER_PROVIDER=openai` | yes* |
| `ANTHROPIC_API_KEY` | console.anthropic.com — only if `ANSWER_PROVIDER=anthropic` | yes* |
| `VOYAGE_API_KEY` | dashboard.voyageai.com — embeddings/reranking, always used | yes |
| `VAULT_ADDR`, `VAULT_TOKEN` | Only needed if `APP_ENV != local` | no |
| `DATABASE_URL`, `QDRANT_URL`, `REDIS_URL` | Overridden automatically in docker-compose | no |
| `APP_ENV` | `local` — secrets are read from `.env`; otherwise — from Vault at the address above | yes |

*Only the active `ANSWER_PROVIDER`'s key is actually required — the other
one can be left as a placeholder.

With `APP_ENV=local`, secrets (`GITHUB_APP_CLIENT_SECRET`, `SESSION_SECRET_KEY`,
`VOYAGE_API_KEY`, `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, the App private key)
are read directly from the environment/file. In production they go through
Vault (`api/services/secrets.py`) — `.env` is not used for secrets there.

## How to use it

Open `http://localhost:3000` and:

1. **Log in** with the "Войти через GitHub" button (GitHub OAuth, sets a
   session cookie).
2. **Connect a repository** with "+ Подключить репозиторий" — opens the
   GitHub App installation flow; after picking repositories it comes
   back to the dashboard and indexing starts in the background. Status
   and progress (files done/total) update automatically while indexing.

   **If nothing happens after clicking Install on GitHub's side**, your
   App's **Setup URL** is probably missing (see the app setup step
   above) — GitHub has no way to tell us the installation happened
   without it. As a fallback that doesn't depend on that redirect at
   all, click **"Синхронизировать"** on the dashboard: it looks up the
   app's installations for your GitHub account directly
   (`GET /app/installations`) and lists the repos in it, so you can pick
   one to connect explicitly, without ever needing GitHub to call us
   back.
3. **Chat** — click "Чат" on a repository once its status is "Готов"
   (ready). Type a question; the answer streams in token by token, with
   the source files/lines shown above it once retrieval finishes. Rate
   answers with 👍/👎.
4. **No webhook configured?** Each repo row has a "Переиндексировать"
   button — does a full reindex the first time, an incremental one
   (same diff-based logic as the push webhook) after that.

### Using the API directly

Everything above is also a plain JSON/SSE API, useful for scripting —
the session cookie from the browser login works here too:

```bash
# List your repos
curl --cookie "session=<cookie value from login>" http://localhost:8000/api/v1/repos

# Ask a question — SSE stream: `sources` event, then `token` events
# (the answer, piece by piece), then `done` with a query_id
curl -N -X POST http://localhost:8000/api/v1/repos/<repo_id>/ask \
  -H "Content-Type: application/json" \
  --cookie "session=<cookie value from login>" \
  -d '{"question": "where is retry logic handled in the HTTP client?"}'

# Rate an answer
curl -X POST http://localhost:8000/api/v1/queries/<query_id>/feedback \
  -H "Content-Type: application/json" \
  --cookie "session=<...>" \
  -d '{"rating": 1, "comment": "pinpointed the right file"}'

# Manually trigger a (re)index instead of waiting for the push webhook
curl -X POST http://localhost:8000/api/v1/repos/<repo_id>/reindex \
  --cookie "session=<...>"
```

`/ask` is limited to 20 questions per minute per repository. If a
webhook URL is configured on the GitHub App, pushes to the default
branch trigger the same incremental reindex automatically.

## Database migrations

Schema changes go through Alembic (`alembic/versions/`). The `migrate`
service in `docker-compose.yml` runs `alembic upgrade head` automatically
before `api`/`worker` start, so a plain `docker compose up` always leaves
the schema current.

To add a new migration:

```bash
docker compose exec api alembic revision --autogenerate -m "add foo column"
```

Always review the generated file by hand — autogenerate doesn't reliably
detect column renames, enum changes, or check constraints.

## Tests

```bash
docker compose exec api pytest
```

All external APIs (GitHub, Voyage, Anthropic) are mocked in tests via
`respx`/`unittest.mock`. Postgres, Qdrant, and Redis run as real
instances from `docker-compose` (local infrastructure, not an external
service, so they aren't mocked).

## Project layout

```
api/            — FastAPI app (routers/, services/, db/)
worker/         — RQ indexing worker
frontend/       — React + Vite web UI
alembic/        — DB migrations (Alembic), applied automatically by the
                  `migrate` service on `docker compose up`
.claude/tasks/  — detailed task specs for each piece of functionality
```
