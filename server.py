import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from datetime import datetime, timezone

from email_processor import EmailProcessor
from audit_log import AuditLogger
from gatekeeper import Gatekeeper
from gmail_client import GmailClient
from negotiator import Negotiator
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

GMAIL_PUBSUB_TOPIC = os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()
GMAIL_WEBHOOK_TOKEN = os.getenv("GMAIL_WEBHOOK_TOKEN", "").strip()
GMAIL_WEBHOOK_PATH = os.getenv("GMAIL_WEBHOOK_PATH", "/gmail/webhook").strip() or "/gmail/webhook"
GMAIL_WATCH_RENEW_HOURS = float(os.getenv("GMAIL_WATCH_RENEW_HOURS", "24"))

if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is required. Set it in .env or your deployment environment."
    )

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is required for triage_inbox and validate_and_negotiate tools."
    )

gmail = GmailClient()
processor = EmailProcessor()
gatekeeper = Gatekeeper()
negotiator = Negotiator()
task_store = MongoTaskStore(
    uri=MONGODB_URI,
    db_name=MONGODB_DB,
    collection_name=MONGODB_TASKS_COLLECTION,
    default_lease_seconds=CLAIM_LEASE_SECONDS,
)
audit_logger = AuditLogger(task_store.audit_log)


def _resolve_project_id(client: dict[str, Any]) -> str | None:
    """Return the project identifier from known client document shapes."""
    project_id = client.get("project_id")
    if isinstance(project_id, str) and project_id.strip():
        return project_id.strip()

    github_repo = client.get("githubRepo")
    if isinstance(github_repo, str) and github_repo.strip():
        return github_repo.strip()

    return None


async def _ingest_emails() -> int:
    """Pull unread inbox messages and persist new AgentTasks to MongoDB."""
    messages = gmail.get_unread_messages(max_results=MAX_EMAILS_PER_RUN)
    existing_ids = await task_store.existing_email_ids()
    added = 0
    for msg in messages:
        task = processor.to_agent_task(msg)
        if task is None or task.email_id in existing_ids:
            continue
        client = await task_store.lookup_client(task.sender_email)
        if client is None:
            logger.debug("Skipping email from unregistered sender: %s", task.sender_email)
            continue
        project_id = _resolve_project_id(client)
        if project_id is None:
            logger.warning(
                "Skipping email from %s: client has no project_id/githubRepo",
                task.sender_email,
            )
            continue
        inserted = await task_store.insert_pending(task, project_id=project_id)
        if not inserted:
            continue
        existing_ids.add(task.email_id)
        gmail.mark_as_read(msg["id"])
        added += 1
    if added:
        size = await task_store.queue_size()
        logger.info(f"Ingested {added} tasks (queue: {size})")
    return added


def _start_gmail_watch() -> None:
    """Start (or refresh) the Gmail users.watch registration. Best-effort."""
    if not GMAIL_PUBSUB_TOPIC:
        return
    try:
        resp = gmail.start_watch(GMAIL_PUBSUB_TOPIC)
        logger.info(
            "Gmail watch active: historyId=%s expiration=%s",
            resp.get("historyId"),
            resp.get("expiration"),
        )
    except Exception as exc:  # pragma: no cover - network/auth errors
        logger.warning("Failed to start Gmail watch: %s", exc)


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
    if GMAIL_PUBSUB_TOPIC:
        _start_gmail_watch()
        scheduler.add_job(
            _start_gmail_watch,
            "interval",
            hours=GMAIL_WATCH_RENEW_HOURS,
            id="gmail_watch_renew",
        )
    else:
        logger.info(
            "GMAIL_PUBSUB_TOPIC unset; webhook ingestion disabled (cron polling only)"
        )
    scheduler.start()
    await _ingest_emails()  # Immediate run on startup
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if GMAIL_PUBSUB_TOPIC:
            try:
                gmail.stop_watch()
            except Exception as exc:  # pragma: no cover
                logger.debug("gmail.stop_watch on shutdown failed: %s", exc)
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


@mcp.custom_route(GMAIL_WEBHOOK_PATH, methods=["POST"], include_in_schema=False)
async def gmail_webhook(request) -> JSONResponse:
    """Pub/Sub push endpoint for Gmail change notifications.

    Verifies a shared-secret token in the URL query, then schedules an
    `_ingest_emails()` run in the background. Returns 200 immediately so
    Pub/Sub does not retry on slow ingests.
    """
    if not GMAIL_WEBHOOK_TOKEN:
        return JSONResponse({"error": "webhook_disabled"}, status_code=503)

    token = request.query_params.get("token", "")
    if not hmac.compare_digest(token, GMAIL_WEBHOOK_TOKEN):
        logger.warning("gmail_webhook: rejected request with invalid token")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    data_b64 = message.get("data", "") if isinstance(message, dict) else ""
    if data_b64:
        try:
            decoded = base64.b64decode(data_b64).decode("utf-8", errors="replace")
            event = json.loads(decoded)
            logger.info(
                "gmail_webhook: event email=%s historyId=%s",
                event.get("emailAddress"),
                event.get("historyId"),
            )
        except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
            logger.debug("gmail_webhook: could not decode Pub/Sub data: %s", exc)

    asyncio.create_task(_ingest_emails())
    return JSONResponse({"status": "accepted"})


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
async def register_client(email: str, project_id: str) -> dict[str, Any]:
    """
    Register or update a client email → project mapping.

    Upserts into the clients collection. After registration, emails from
    this sender are accepted by triage_inbox and tagged with project_id.
    Subsequent calls with the same email update the project_id.

    Args:
        email: Client's email address.
        project_id: Project identifier to associate with this client.

    Returns:
        The stored client document: {email, project_id, created_at, updated_at}
    """
    doc = await task_store.register_client(email=email, project_id=project_id)
    logger.info("Registered client %s → project %s", email, project_id)
    return doc


@mcp.tool()
async def triage_inbox(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Classify messages into Candidate Tasks, enforcing client registration.

    Each item must include "message" (str) and "sender_email" (str).
    Senders not registered in the clients collection receive
    status="NOT_REGISTERED" and are skipped by the LLM classifier.

    Pass actionable results to validate_and_negotiate to check for
    redundancy and conflicts before agents claim work.

    Args:
        messages: List of {"message": str, "sender_email": str} dicts.

    Returns:
        One result per input item:
        - Registered:   {task_id, original_message, sender_email, project_id, classification}
        - Unregistered: {task_id: null, original_message, sender_email,
                         status: "NOT_REGISTERED", classification: null}
    """
    results: list[dict[str, Any] | None] = [None] * len(messages)
    registered_indices: list[int] = []
    registered_texts: list[str] = []
    client_map: dict[int, dict] = {}

    for i, item in enumerate(messages):
        sender_email = item.get("sender_email", "")
        message = item.get("message", "")
        client = await task_store.lookup_client(sender_email)
        if client is None:
            logger.info("triage_inbox: unregistered sender %s", sender_email)
            results[i] = {
                "task_id": None,
                "original_message": message,
                "sender_email": sender_email,
                "status": "NOT_REGISTERED",
                "classification": None,
            }
        else:
            registered_indices.append(i)
            registered_texts.append(message)
            client_map[i] = client

    if registered_texts:
        classified = await gatekeeper.triage(registered_texts)
        for idx, original_i in enumerate(registered_indices):
            candidate = classified[idx]
            candidate["sender_email"] = messages[original_i].get("sender_email", "")
            candidate_project_id = _resolve_project_id(client_map[original_i])
            if candidate_project_id is None:
                logger.warning(
                    "triage_inbox: registered sender without project_id/githubRepo: %s",
                    candidate["sender_email"],
                )
                candidate_project_id = "unknown_project"
            candidate["project_id"] = candidate_project_id
            results[original_i] = candidate

    return [r for r in results if r is not None]


@mcp.tool()
async def validate_and_negotiate(candidate_task: dict[str, Any]) -> dict[str, Any]:
    """
    Validate a Candidate Task against the active MongoDB task queue.

    Fetches the 15 most recent pending/in_progress tasks (sliding window),
    then uses an LLM to assess redundancy, conflicts, and Definition of Ready.

    Every decision is written to the audit_log collection. Major conflicts
    are also persisted to the escalations collection and return
    error_code="NEEDS_HUMAN_INTERVENTION".

    If the Anthropic API is unavailable, fails open with
    definition_of_ready="NEW" and metadata.warning="anthropic_api_unavailable".

    Args:
        candidate_task: A Candidate Task dict from triage_inbox output.

    Returns:
        {
          "task_id": "...",
          "classification": { "category": "...", "priority": "...", "is_actionable": true },
          "state_analysis": {
            "is_redundant": false,
            "conflicts_with": [],
            "dependencies": [],
            "definition_of_ready": "READY|NOT_READY|NEEDS_CLARIFICATION|NEW"
          },
          "agent_instructions": { "suggested_files": [], "constraints": [] },
          "metadata": {
            "is_blocked": false,
            "requires_intervention": false,
            "audit_link": "<mongo_id>"
          },
          "error_code": null | "NEEDS_HUMAN_INTERVENTION"
        }
    """
    project_id = candidate_task.get("project_id")
    existing_tasks = await task_store.list_for_negotiation(project_id=project_id)
    result = await negotiator.validate(candidate_task, existing_tasks)

    reasoning = result.pop("reasoning", "")
    state = result.get("state_analysis", {})
    dor = state.get("definition_of_ready", "")
    is_redundant = state.get("is_redundant", False)
    conflicts_with = state.get("conflicts_with", [])
    is_major_conflict = result.get("error_code") == "NEEDS_HUMAN_INTERVENTION"

    if dor == "NEW":
        decision_type = "NEW"
    elif is_major_conflict:
        decision_type = "CONFLICT"
    elif is_redundant:
        decision_type = "REDUNDANT"
    else:
        decision_type = dor  # "READY" or "NEEDS_CLARIFICATION"

    audit_link = await audit_logger.record(
        decision_type=decision_type,
        task_id=candidate_task.get("task_id", "unknown"),
        reasoning=reasoning,
        candidate_message=candidate_task.get("original_message", ""),
        definition_of_ready=dor,
        conflicts_with=conflicts_with,
    )

    if is_major_conflict:
        await task_store.insert_escalation({
            "candidate_task_id": candidate_task.get("task_id"),
            "candidate_message": candidate_task.get("original_message", ""),
            "conflicts_with": conflicts_with,
            "conflict_severity": "major",
            "reasoning": reasoning,
            "audit_link": audit_link,
            "created_at": datetime.now(timezone.utc),
        })

    if "metadata" not in result:
        result["metadata"] = {
            "is_blocked": is_major_conflict,
            "requires_intervention": is_major_conflict,
            "audit_link": audit_link,
        }
    else:
        result["metadata"]["audit_link"] = audit_link

    return result


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
