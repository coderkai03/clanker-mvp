# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gmail-to-agent MCP bridge for the **Infinite Productivity Machine** hackathon project. The server polls Gmail on a cron schedule, converts unread client emails into structured `AgentTask` payloads, and exposes four MCP tools so an AI coding agent can read tasks, communicate with clients, and mark work complete.

## Setup

```bash
uv sync                  # install dependencies
cp .env.example .env     # fill in paths
# Put credentials.json from Google Cloud Console in the project root
uv run server.py         # first run opens browser for OAuth; token.json saved for subsequent runs
```

Add to Claude Desktop `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "gmail-agent-bridge": {
      "command": "uv",
      "args": ["run", "/path/to/clanker-mcp/server.py"]
    }
  }
}
```

## MCP Tools

| Tool | Purpose |
|------|---------|
| `get_pending_tasks(urgency?, task_type?)` | Returns buffered AgentTask list; supports urgency/type filters |
| `send_email(to, subject, body, reply_to_thread_id?)` | Sends email; pass `thread_id` from AgentTask to reply in-thread |
| `mark_task_complete(email_id, resolution_note?)` | Removes task from buffer after resolution |
| `trigger_ingest()` | Manual on-demand ingest; bypasses cron schedule |

## Architecture

```
server.py           FastMCP server, cron scheduler, all four tools, in-process task buffer (_pending_tasks)
gmail_client.py     Google OAuth flow + Gmail API (fetch unread, mark read, send)
email_processor.py  Parses raw Gmail message dict → AgentTask dataclass with urgency/type classification
```

**Data flow:** cron fires `_ingest_emails()` → `GmailClient.get_unread_messages()` → `EmailProcessor.to_agent_task()` → appended to `_pending_tasks` → email marked read. Agent calls `get_pending_tasks()` to read buffer, acts, calls `mark_task_complete()` when done.

**`AgentTask.handoff_instructions`** — pre-formatted string telling the agent exactly what to do, generated from the task at dump time.

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

## Key constraints

- `_pending_tasks` is in-process memory — tasks are lost on restart. For persistence, replace with SQLite or a file-backed store.
- The cron job marks emails as read immediately on ingest to avoid re-processing. If a task is dropped (parse failure), the email stays read.
- `EmailProcessor.to_agent_task()` returns `None` for emails with empty bodies — these are silently skipped.
