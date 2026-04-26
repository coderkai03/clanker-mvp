import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from email_processor import EmailProcessor
from gmail_client import GmailClient
from task_store import MongoTaskStore

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("gmail-agent-bridge")

INGEST_INTERVAL_HOURS = float(os.getenv("INGEST_INTERVAL_HOURS", "1"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "20"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp")
MCP_STATELESS_HTTP = os.getenv("MCP_STATELESS_HTTP", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "clanker_mcp")
MONGODB_TASKS_COLLECTION = os.getenv("MONGODB_TASKS_COLLECTION", "tasks")
CLAIM_LEASE_SECONDS = int(os.getenv("CLAIM_LEASE_SECONDS", "1800"))

if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is required. Set it in .env or your deployment environment."
    )

gmail = GmailClient()
processor = EmailProcessor()
task_store = MongoTaskStore(
    uri=MONGODB_URI,
    db_name=MONGODB_DB,
    collection_name=MONGODB_TASKS_COLLECTION,
    default_lease_seconds=CLAIM_LEASE_SECONDS,
)


async def _ingest_emails() -> int:
    """Pull unread inbox messages and persist new AgentTasks to MongoDB."""
    messages = gmail.get_unread_messages(max_results=MAX_EMAILS_PER_RUN)
    existing_ids = await task_store.existing_email_ids()
    added = 0
    for msg in messages:
        task = processor.to_agent_task(msg)
        if task is None or task.email_id in existing_ids:
            continue
        inserted = await task_store.insert_pending(task)
        if not inserted:
            continue
        existing_ids.add(task.email_id)
        gmail.mark_as_read(msg["id"])
        added += 1
    if added:
        size = await task_store.queue_size()
        logger.info(f"Ingested {added} tasks (queue: {size})")
    return added


@asynccontextmanager
async def _lifespan(app: FastMCP):
    await task_store.init_indexes()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: asyncio.create_task(_ingest_emails()),
        "interval",
        hours=INGEST_INTERVAL_HOURS,
        id="email_ingest",
    )
    scheduler.start()
    await _ingest_emails()  # Immediate run on startup
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        task_store.close()


mcp = FastMCP(
    name="gmail-agent-bridge",
    lifespan=_lifespan,
    instructions=(
        "Gmail-to-agent bridge for the Infinite Productivity Machine. "
        "Multi-session workflow: each agent session uses a stable worker_id. "
        "Call get_next_task(worker_id) to atomically claim a task, work it, "
        "then mark_task_complete(email_id, worker_id). "
        "Use release_task(email_id, worker_id) to abort a claim. "
        "get_pending_tasks() is read-only — it does not claim."
    ),
)


# ---------------------------------------------------------------------------
# Healthcheck endpoint (HTTP transport only)
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def healthcheck(_request) -> JSONResponse:
    size = await task_store.queue_size()
    return JSONResponse({"status": "ok", "queue_size": size})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    reply_to_thread_id: str | None = None,
) -> dict[str, Any]:
    """
    Send an email to a client.

    Use to request clarification when requirements are vague, or to notify
    the client when their requested change is complete.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text body content.
        reply_to_thread_id: Gmail thread ID (AgentTask.thread_id) to reply within.
    """
    result = gmail.send_message(to=to, subject=subject, body=body, thread_id=reply_to_thread_id)
    logger.info(f"Sent email → {to}: {subject}")
    return result


@mcp.tool()
async def get_pending_tasks(
    urgency: str | None = None,
    task_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Read-only view of tasks currently claimable by any worker.

    Includes both `pending` tasks and `in_progress` tasks whose lease has
    expired. This tool does NOT claim tasks — it is safe for diagnostics
    and dashboards. To actually work a task, call `get_next_task`.

    Each task includes:
    - email_id / thread_id: Use thread_id when calling send_email to reply.
    - sender / sender_email: Client contact info.
    - subject / raw_text: Original email content.
    - task_type: bug_report | feature_request | feedback | question | unknown
    - urgency: high | medium | low
    - status: pending | in_progress | completed
    - claimed_by / claimed_at / lease_expires_at: Coordination metadata.
    - extracted_requirements: Parsed actionable items.
    - handoff_instructions: Step-by-step guide for the agent.

    Args:
        urgency: Filter by urgency level (high | medium | low).
        task_type: Filter by task type.
    """
    return await task_store.list_pending(urgency=urgency, task_type=task_type)


@mcp.tool()
async def get_next_task(
    worker_id: str,
    lease_seconds: int | None = None,
    urgency: str | None = None,
    task_type: str | None = None,
) -> dict[str, Any] | None:
    """
    Atomically claim the next available task for this worker.

    Use this in multi-session deployments so two agents never work the same
    task. The returned task is moved to `in_progress` with a lease. If you
    do not call `mark_task_complete` or `release_task` before the lease
    expires, the task becomes claimable again automatically.

    Args:
        worker_id: Stable identifier for this agent session (e.g. Devin session id).
        lease_seconds: How long the claim is held before auto-releasing. Defaults to CLAIM_LEASE_SECONDS env var (1800s).
        urgency: Optional filter (high | medium | low).
        task_type: Optional filter (bug_report | feature_request | feedback | question | unknown).

    Returns:
        Claimed task dict, or None if no claimable task exists.
    """
    task = await task_store.claim_next(
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        urgency=urgency,
        task_type=task_type,
    )
    if task is None:
        logger.info(f"No claimable task for worker_id={worker_id}")
        return None
    logger.info(f"Worker {worker_id} claimed task {task['email_id']}")
    return task


@mcp.tool()
async def mark_task_complete(
    email_id: str,
    worker_id: str,
    resolution_note: str = "",
) -> dict[str, Any]:
    """
    Mark a claimed task as completed.

    Only the worker that currently owns the claim can complete the task.
    If `worker_id` does not match the active claim (or the task is not
    in_progress), this returns `{"completed": false, ...}` so the caller
    can decide whether to re-claim or abort.

    Args:
        email_id: The email_id from the AgentTask.
        worker_id: The same worker_id that claimed the task.
        resolution_note: Optional summary of what was done (stored on the doc).
    """
    doc = await task_store.complete(
        email_id=email_id,
        worker_id=worker_id,
        resolution_note=resolution_note,
    )
    queue_size = await task_store.queue_size()
    if doc is None:
        logger.warning(
            f"mark_task_complete failed: worker_id={worker_id} does not own {email_id}"
        )
        return {
            "completed": False,
            "email_id": email_id,
            "worker_id": worker_id,
            "queue_size": queue_size,
            "reason": "claim_not_owned_or_not_in_progress",
        }
    logger.info(
        f"Completed task {email_id} by worker {worker_id}: "
        f"{resolution_note or '(no note)'}"
    )
    return {
        "completed": True,
        "email_id": email_id,
        "worker_id": worker_id,
        "queue_size": queue_size,
    }


@mcp.tool()
async def release_task(email_id: str, worker_id: str) -> dict[str, Any]:
    """
    Voluntarily release a claimed task back to the pending queue.

    Use this when the agent decides it cannot complete the task right now
    (e.g. needs human input). Only the owning worker can release.

    Args:
        email_id: The email_id from the AgentTask.
        worker_id: The same worker_id that claimed the task.
    """
    doc = await task_store.release(email_id=email_id, worker_id=worker_id)
    queue_size = await task_store.queue_size()
    if doc is None:
        return {
            "released": False,
            "email_id": email_id,
            "worker_id": worker_id,
            "queue_size": queue_size,
            "reason": "claim_not_owned_or_not_in_progress",
        }
    logger.info(f"Released task {email_id} from worker {worker_id}")
    return {
        "released": True,
        "email_id": email_id,
        "worker_id": worker_id,
        "queue_size": queue_size,
    }


@mcp.tool()
async def trigger_ingest() -> dict[str, Any]:
    """
    Manually trigger an email ingestion run without waiting for the cron schedule.
    Useful for testing or when you know new mail has arrived.
    """
    added = await _ingest_emails()
    queue_size = await task_store.queue_size()
    return {"added": added, "queue_size": queue_size}


def main() -> None:
    if MCP_TRANSPORT in {"http", "streamable-http"}:
        mcp.run(
            transport="streamable-http",
            host=MCP_HOST,
            port=MCP_PORT,
            path=MCP_HTTP_PATH,
            stateless_http=MCP_STATELESS_HTTP,
        )
        return

    if MCP_TRANSPORT != "stdio":
        raise ValueError(
            "Unsupported MCP_TRANSPORT. Use one of: stdio, http, streamable-http."
        )

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
