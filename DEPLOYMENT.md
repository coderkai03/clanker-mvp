# Deployment Guide

This project can run either as a local `stdio` MCP server or a hosted Streamable HTTP MCP server. Both modes share the same MongoDB Atlas-backed task store, so multiple agent sessions can coordinate safely.

## Prerequisites

- A MongoDB Atlas cluster (free M0 is fine for the hackathon).
- `credentials.json` from Google Cloud Console (Desktop OAuth client).
- A pre-generated `token.json` for headless deployments (see below).

## Local (Claude Desktop / Cursor)

```bash
uv sync
cp .env.example .env
# Set MONGODB_URI in .env
uv run server.py
```

Default mode is `MCP_TRANSPORT=stdio`.

## Hosted (Devin-compatible HTTP MCP)

Set environment variables:

```bash
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8000
MCP_HTTP_PATH=/mcp
MCP_STATELESS_HTTP=true

MONGODB_URI=mongodb+srv://<user>:<pass>@<cluster>/?retryWrites=true&w=majority
MONGODB_DB=clanker_mcp
MONGODB_TASKS_COLLECTION=tasks
CLAIM_LEASE_SECONDS=1800
```

Then run:

```bash
uv run server.py
```

### Endpoints

- MCP endpoint: `http(s)://<host>:<port>/mcp`
- Healthcheck: `http(s)://<host>:<port>/health` — returns `{ "status": "ok", "queue_size": <int> }`
- Gmail webhook: `http(s)://<host>:<port><GMAIL_WEBHOOK_PATH>?token=<GMAIL_WEBHOOK_TOKEN>` (POST; only enabled when both env vars are set)

## MongoDB Atlas setup

1. Create a free **M0** cluster in [MongoDB Atlas](https://cloud.mongodb.com/).
2. **Database Access** → add a user with read/write on `clanker_mcp`. Note the password.
3. **Network Access** → IP allow list:
   - Production: add the hosted server's public IP (e.g. Vultr VM IP).
   - Hackathon demo: temporarily allow `0.0.0.0/0` if you can't pin an IP. Tighten before any real use.
4. **Cluster → Connect → Drivers** → copy the `mongodb+srv://...` URI. Replace `<password>` with the user password and set it as `MONGODB_URI`.
5. On first startup the server creates the required indexes (`email_id` unique, claim-sort compound, lease-expiry compound). No schema migration is needed.

## Gmail OAuth in deployed environments

`gmail_client.py` uses the desktop OAuth flow for first authentication. In headless deployments:

1. Run locally once to generate `token.json`.
2. Provide both `credentials.json` and `token.json` to the deployed runtime.
3. Keep paths aligned with:
   - `GMAIL_CREDENTIALS_PATH`
   - `GMAIL_TOKEN_PATH`

If `token.json` contains a refresh token, the server can refresh access automatically.

## Devin setup

In Devin MCP Marketplace ("Add Your Own"):

- Transport: **HTTP**
- URL: `https://<your-domain>/mcp`
- Optional auth header if your host/reverse proxy enforces auth.

### Multi-session contract

Every Devin session must call MCP tools with a stable `worker_id` (the Devin session id is a good choice).

Recommended loop per session:

1. `get_next_task(worker_id="<session-id>")` — atomically claims the highest-priority task for this worker. Returns `null` when nothing is claimable.
2. Work the task (read `handoff_instructions`, write code, optionally `send_email` for clarification).
3. `mark_task_complete(email_id, worker_id="<session-id>", resolution_note="...")`.
4. If the agent decides not to finish: `release_task(email_id, worker_id="<session-id>")`. The task returns to `pending` for another session.

Failure modes handled automatically:
- Session crash mid-task: claim's `lease_expires_at` elapses (default `CLAIM_LEASE_SECONDS=1800`), the task becomes claimable again on the next `get_next_task`.
- Two sessions racing on the same task: MongoDB `findOneAndUpdate` is atomic, so exactly one wins; the other gets either a different task or `null`.
- Reading without claiming: `get_pending_tasks` is read-only and safe for dashboards.

## Gmail webhook (Pub/Sub push) setup

Real-time ingestion requires GCP-side configuration (Pub/Sub topic + push subscription) plus the env vars below. See the README/CLAUDE.md for the GCP walkthrough; the MCP server-side requirements are:

```bash
GMAIL_PUBSUB_TOPIC=projects/<gcp-project-id>/topics/<topic>
GMAIL_WEBHOOK_TOKEN=<random-shared-secret>
GMAIL_WEBHOOK_PATH=/gmail/webhook        # default
GMAIL_WATCH_RENEW_HOURS=24               # default
```

On startup the server calls `users.watch` against the topic and re-registers it every `GMAIL_WATCH_RENEW_HOURS` (Gmail expires watches after 7 days). If `GMAIL_PUBSUB_TOPIC` is unset, webhook ingestion is disabled and only cron polling runs.

### Operational notes for the Gmail webhook

- **Push endpoint URL**: configure the Pub/Sub push subscription to POST to `https://<host><GMAIL_WEBHOOK_PATH>?token=<GMAIL_WEBHOOK_TOKEN>`. The handler rejects requests with a missing or mismatched token (401).
- **Pub/Sub IAM**: the service account `gmail-api-push@system.gserviceaccount.com` must have the **Pub/Sub Publisher** role on the topic, otherwise `users.watch` fails.
- **ngrok / dynamic hosts**: every time the public URL changes (e.g. ngrok restart), update the Pub/Sub push subscription's endpoint to match. The `?token=` query string is part of the configured endpoint, not the request body, so it must be set on the subscription itself.
- **Retries**: the handler returns `200 {"status":"accepted"}` immediately and runs `_ingest_emails()` in the background, so Pub/Sub will not retry on slow ingests. Errors during ingest are logged but not surfaced to Pub/Sub.
- **Backup polling**: cron ingest (`INGEST_INTERVAL_HOURS`) keeps running, so missed notifications are eventually picked up via the unread-inbox query.

## Run the dispatcher (Devin worker spawning)

`dispatcher.py` is a second long-running process that watches the `tasks` collection via a Mongo change stream and spawns one Devin session per project that has pending work. It must run alongside the MCP server.

Required env vars (in addition to the MCP server vars):

```bash
DEVIN_API_KEY=<bearer token from Devin>
DEVIN_API_URL=https://api.devin.ai/v1                # default
MCP_PUBLIC_URL=https://<your-public-host>/mcp        # used in worker prompts
WORKER_LEASE_SECONDS=1800                            # per-project lock TTL
RECONCILE_INTERVAL_SECONDS=60                        # heal missed events
```

Run it:

```bash
uv run dispatcher.py
```

For local end-to-end testing the typical layout is three terminals: `server.py`, `dispatcher.py`, and `ngrok http 8000`. In production, run them as two separate services (e.g. two systemd units or two containers) sharing the same `.env` and pointing at the same Atlas cluster.

### Operational notes for the dispatcher

- **Only one dispatcher**: run a single dispatcher instance per Mongo cluster. Multiple dispatchers will not spawn duplicate workers (the per-project lock is atomic), but they will both consume the change stream and waste API calls.
- **Mongo Atlas required**: change streams need a replica set. Atlas always provides one; standalone Mongo deployments must be configured as a single-node replica set.
- **Idempotent spawns**: every spawn acquires `worker_sessions.{project_id}` with a TTL. Locks auto-expire after `WORKER_LEASE_SECONDS`, so a crashed Devin session does not block the project forever.
- **Resume tokens**: persisted in `dispatcher_state` after every event so restarts pick up where they left off. If the resume token ages past Mongo's oplog window, the dispatcher logs an error and the reconciliation loop catches up by scanning for `pending` tasks.
- **Cost control**: lower `WORKER_LEASE_SECONDS` to release locks faster, or raise `RECONCILE_INTERVAL_SECONDS` to reduce reconcile-driven spawns.

## Operational notes

- Run a single MCP server instance against the Atlas cluster. Replicas are fine for tool reads but the cron ingest should only fire on one node — otherwise the same email gets ingested multiple times before the unique-index dedupes.
- Watch the Atlas metrics dashboard during demos: judges can see live `find_one_and_update` ops as Devin sessions claim tasks.
- To reset the queue: drop the `tasks` collection in Atlas; indexes are recreated on the next server startup.
