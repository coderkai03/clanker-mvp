# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gmail-to-agent MCP bridge for the **Infinite Productivity Machine** hackathon project. The server polls Gmail on a cron schedule, converts unread client emails into structured `AgentTask` payloads, persists them in MongoDB Atlas, and exposes MCP tools so multiple concurrent AI coding agent sessions can atomically claim, work, and complete tasks without collisions.

## Setup

```bash
uv sync                  # install dependencies
cp .env.example .env     # fill in paths and MONGODB_URI
# Put credentials.json from Google Cloud Console in the project root
uv run server.py         # default stdio mode; first run opens browser for OAuth
```

`MONGODB_URI` is required — the server refuses to start without it.

Add to Claude Desktop `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "gmail-agent-bridge": {
      "command": "uv",
      "args": ["--directory", "/path/to/clanker-mcp", "run", "server.py"]
    }
  }
}
```

## Running modes

This server supports two transports via `MCP_TRANSPORT`:

- `stdio` (default): local MCP clients that launch the process directly (Claude Desktop/Cursor)
- `streamable-http`: hosted MCP endpoint for remote clients such as Devin

Run in HTTP mode:
```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8000 uv run server.py
```

Health endpoint (HTTP mode): `GET /health`
MCP endpoint path defaults to `/mcp` (configurable with `MCP_HTTP_PATH`).

## Devin integration (remote HTTP)

1. Deploy this app on a host that exposes port `MCP_PORT`.
2. Set env vars at minimum:
   - `MCP_TRANSPORT=streamable-http`
   - `MCP_HOST=0.0.0.0`
   - `MCP_PORT=<platform_port_or_8000>`
3. Ensure `credentials.json` and `token.json` are available to the runtime.
   - For headless deploys, create `token.json` locally first, then provide it as a secret/file in your host.
4. In Devin → MCP Marketplace → Add Your Own:
   - Transport: HTTP
   - URL: `https://<your-domain>/mcp`
   - Optional auth header if you put auth in front of the service.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_pending_tasks(urgency?, task_type?)` | Read-only view of claimable tasks (pending or lease-expired). Does not claim. |
| `get_next_task(worker_id, lease_seconds?, urgency?, task_type?)` | Atomically claim the next task for this worker; returns `null` if none. |
| `mark_task_complete(email_id, worker_id, resolution_note?)` | Mark claimed task complete. Only the owning worker can complete. |
| `release_task(email_id, worker_id)` | Voluntarily release a claim back to pending. |
| `send_email(to, subject, body, reply_to_thread_id?)` | Sends email; pass `thread_id` from AgentTask to reply in-thread. |
| `trigger_ingest()` | Manual on-demand ingest; bypasses cron schedule. |

### Multi-session usage

- Every agent session must pass a stable `worker_id` (e.g. Devin session id).
- The expected loop is: `get_next_task(worker_id)` → work → `mark_task_complete(email_id, worker_id)`.
- If a session crashes or disconnects, its claim auto-expires after `CLAIM_LEASE_SECONDS` and the task becomes claimable by another worker.
- `get_pending_tasks` is for diagnostics/dashboards — it does NOT claim, so two agents reading the list will not coordinate. Use `get_next_task` to actually take work.

## Architecture

```
server.py           FastMCP server, cron scheduler, MCP tools (HTTP + stdio), wires task_store
task_store.py       MongoTaskStore: atomic claim/release/complete via MongoDB find_one_and_update
gmail_client.py     Google OAuth flow + Gmail API (fetch unread, mark read, send)
email_processor.py  Parses raw Gmail message dict → AgentTask dataclass with urgency/type classification
```

**Data flow:** cron fires `_ingest_emails()` → `GmailClient.get_unread_messages()` → `EmailProcessor.to_agent_task()` → `task_store.insert_pending()` (idempotent on `email_id`) → email marked read. Agent session calls `get_next_task(worker_id)` to atomically claim, acts, calls `mark_task_complete(email_id, worker_id)` when done.

**`AgentTask.handoff_instructions`** — pre-formatted string telling the agent exactly what to do, generated from the task at dump time and stored on the Mongo doc.

### Real-time ingestion (Gmail webhook)

When `GMAIL_PUBSUB_TOPIC` and `GMAIL_WEBHOOK_TOKEN` are set, the server registers a Gmail `users.watch` against a Cloud Pub/Sub topic on startup and exposes a push endpoint at `GMAIL_WEBHOOK_PATH` (default `/gmail/webhook`). Pub/Sub POSTs notifications to `https://<host><GMAIL_WEBHOOK_PATH>?token=<GMAIL_WEBHOOK_TOKEN>`; the handler verifies the shared secret with `hmac.compare_digest`, schedules `_ingest_emails()` in the background, and returns `200` immediately. A scheduled `gmail_watch_renew` job re-registers the watch every `GMAIL_WATCH_RENEW_HOURS` (default 24h) because Gmail watches expire after 7 days.

Cron polling (`INGEST_INTERVAL_HOURS`) keeps running as a backup so missed notifications are eventually picked up. If `GMAIL_PUBSUB_TOPIC` is unset, the watch and webhook are disabled and only cron runs.

### Task document schema (`tasks` collection)

| Field | Type | Notes |
|-------|------|-------|
| `email_id` | string (unique) | Gmail message id; idempotency key. |
| `thread_id`, `sender`, `sender_email`, `subject`, `received_at`, `raw_text` | string | Email payload. |
| `task_type` | string | `bug_report` \| `feature_request` \| `feedback` \| `question` \| `unknown` |
| `urgency` | string | `high` \| `medium` \| `low` |
| `urgency_rank` | int | `0`/`1`/`2` for sort priority on claim. |
| `extracted_requirements` | string[] | Parsed actionable items. |
| `context` | dict | Misc metadata. |
| `handoff_instructions` | string | Agent-facing instructions. |
| `status` | string | `pending` \| `in_progress` \| `completed` |
| `claimed_by`, `claimed_at`, `lease_expires_at` | string/datetime | Claim coordination. |
| `created_at`, `completed_at`, `resolution_note` | string/datetime/string | Audit trail. |

Indexes (created automatically on startup):
- Unique on `email_id`.
- Compound `(status, urgency_rank, received_at)` to back the claim sort.
- Compound `(status, lease_expires_at)` to find lease-expired claims fast.

## Auth

Uses standard Google OAuth 2.0 desktop flow. On first run, opens browser → saves `token.json`. Subsequent runs refresh automatically. Required scopes: `gmail.readonly`, `gmail.send`, `gmail.modify`.

To reset auth: delete `token.json`.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `GMAIL_CREDENTIALS_PATH` | `credentials.json` | Download from GCP Console |
| `GMAIL_TOKEN_PATH` | `token.json` | Auto-created |
| `INGEST_INTERVAL_HOURS` | `1` | Cron frequency |
| `MAX_EMAILS_PER_RUN` | `20` | Cap per ingest cycle |
| `LOG_LEVEL` | `INFO` | DEBUG for verbose |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind host |
| `MCP_PORT` | `8000` | HTTP bind port |
| `MCP_HTTP_PATH` | `/mcp` | Streamable HTTP endpoint path |
| `MCP_STATELESS_HTTP` | `true` | FastMCP stateless HTTP mode |
| `MONGODB_URI` | _(required)_ | Atlas connection string (`mongodb+srv://...`) |
| `MONGODB_DB` | `clanker_mcp` | Database name |
| `MONGODB_TASKS_COLLECTION` | `tasks` | Collection name |
| `CLAIM_LEASE_SECONDS` | `1800` | Lease length on a claim before it auto-expires |
| `GMAIL_PUBSUB_TOPIC` | _(empty)_ | Full topic name `projects/<gcp-project>/topics/<topic>`; empty disables webhook |
| `GMAIL_WEBHOOK_TOKEN` | _(empty)_ | Shared secret required as `?token=` on the push endpoint |
| `GMAIL_WEBHOOK_PATH` | `/gmail/webhook` | Path Pub/Sub posts to |
| `GMAIL_WATCH_RENEW_HOURS` | `24` | Watch renewal cadence (Gmail expires watches after 7 days) |

## MongoDB Atlas setup

1. Create a free M0 cluster in MongoDB Atlas.
2. Database Access → add a user with read/write on `clanker_mcp`.
3. Network Access → add the hosted server's public IP (e.g. Vultr VM IP). For hackathon demos you can temporarily allow `0.0.0.0/0`.
4. Cluster → Connect → Drivers → copy the `mongodb+srv://...` URI and set it as `MONGODB_URI` in `.env`.
5. The server creates required indexes on first startup; no manual schema setup is needed.

## Key constraints

- The cron job marks emails as read immediately on successful insert into the task store. If `insert_pending` fails (e.g. duplicate `email_id`), the email stays unread for the next pass.
- `EmailProcessor.to_agent_task()` returns `None` for emails with empty bodies — these are silently skipped.
- A claim is only safe for `CLAIM_LEASE_SECONDS`. Long-running agent sessions should either (a) pick a lease longer than the expected work duration, or (b) re-claim if the work outlives the lease.
- This server is designed to run as a single instance backed by Atlas. Running multiple replicas without further coordination is fine for reads but the cron ingest should only run on one node.
