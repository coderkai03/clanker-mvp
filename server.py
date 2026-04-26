import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastmcp import FastMCP

from email_processor import AgentTask, EmailProcessor
from gmail_client import GmailClient

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("gmail-agent-bridge")

INGEST_INTERVAL_HOURS = float(os.getenv("INGEST_INTERVAL_HOURS", "1"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "20"))

# In-process task buffer. An AI agent reads from here, acts, then marks complete.
_pending_tasks: list[AgentTask] = []

gmail = GmailClient()
processor = EmailProcessor()


async def _ingest_emails() -> int:
    """Pull unread inbox messages and append new AgentTasks to the buffer."""
    messages = gmail.get_unread_messages(max_results=MAX_EMAILS_PER_RUN)
    existing_ids = {t.email_id for t in _pending_tasks}
    added = 0
    for msg in messages:
        task = processor.to_agent_task(msg)
        if task is None or task.email_id in existing_ids:
            continue
        _pending_tasks.append(task)
        existing_ids.add(task.email_id)
        gmail.mark_as_read(msg["id"])
        added += 1
    if added:
        logger.info(f"Ingested {added} tasks (queue: {len(_pending_tasks)})")
    return added


@asynccontextmanager
async def _lifespan(app: FastMCP):
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


mcp = FastMCP(
    name="gmail-agent-bridge",
    lifespan=_lifespan,
    instructions=(
        "Gmail-to-agent bridge for the Infinite Productivity Machine. "
        "Workflow: call get_pending_tasks() → pick a task → implement or clarify via send_email() → mark_task_complete()."
    ),
)


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
def get_pending_tasks(
    urgency: str | None = None,
    task_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return all pending client email tasks awaiting agent action.

    Each task includes:
    - email_id / thread_id: Use thread_id when calling send_email to reply.
    - sender / sender_email: Client contact info.
    - subject / raw_text: Original email content.
    - task_type: bug_report | feature_request | feedback | question | unknown
    - urgency: high | medium | low
    - extracted_requirements: Parsed actionable items.
    - handoff_instructions: Step-by-step guide for the agent.

    Args:
        urgency: Filter by urgency level (high | medium | low).
        task_type: Filter by task type.
    """
    tasks = _pending_tasks
    if urgency:
        tasks = [t for t in tasks if t.urgency == urgency]
    if task_type:
        tasks = [t for t in tasks if t.task_type == task_type]
    return [t.model_dump() for t in tasks]


@mcp.tool()
def mark_task_complete(email_id: str, resolution_note: str = "") -> dict[str, Any]:
    """
    Remove a resolved task from the pending queue.

    Call this after implementing the client's changes or after sending a
    clarification request (the client's reply will create a new task on
    the next ingest cycle).

    Args:
        email_id: The email_id from the AgentTask.
        resolution_note: Optional summary of what was done (logged only).
    """
    global _pending_tasks
    before = len(_pending_tasks)
    _pending_tasks = [t for t in _pending_tasks if t.email_id != email_id]
    removed = before - len(_pending_tasks)
    logger.info(f"Completed task {email_id}: {resolution_note or '(no note)'}")
    return {"removed": removed, "email_id": email_id, "queue_size": len(_pending_tasks)}


@mcp.tool()
async def trigger_ingest() -> dict[str, Any]:
    """
    Manually trigger an email ingestion run without waiting for the cron schedule.
    Useful for testing or when you know new mail has arrived.
    """
    added = await _ingest_emails()
    return {"added": added, "queue_size": len(_pending_tasks)}


if __name__ == "__main__":
    mcp.run()
