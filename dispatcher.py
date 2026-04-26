"""Real-time Devin worker dispatcher.

Watches the MongoDB `tasks` collection via a change stream. When a task
appears with `status="pending"`, atomically claims a per-project lock
and spawns one Devin session as the worker for that project. A
reconciliation loop periodically scans for pending tasks to recover from
missed events while the dispatcher was offline.

Run side-by-side with `server.py`:

    uv run dispatcher.py

Required env vars:
- MONGODB_URI, MONGODB_DB, MONGODB_TASKS_COLLECTION
- DEVIN_API_KEY
- MCP_PUBLIC_URL  (used in worker prompts so Devin knows where to call)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from uuid import uuid4

from dotenv import load_dotenv
from pymongo.errors import PyMongoError

from devin_client import DevinClient
from task_store import MongoTaskStore

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("dispatcher")

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "clanker_mcp")
MONGODB_TASKS_COLLECTION = os.getenv("MONGODB_TASKS_COLLECTION", "tasks")
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY", "")
DEVIN_API_URL = os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1")
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").strip()
WORKER_LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "1800"))
RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "60"))

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is required for dispatcher.py")
if not DEVIN_API_KEY:
    raise RuntimeError("DEVIN_API_KEY is required for dispatcher.py")
if not MCP_PUBLIC_URL:
    raise RuntimeError(
        "MCP_PUBLIC_URL is required so Devin sessions know where to reach the MCP."
    )


def build_worker_prompt(*, project_id: str, worker_id: str, mcp_url: str) -> str:
    """Build the Devin session prompt that drives the worker loop."""
    return (
        "You are an autonomous coding worker for the Infinite Productivity Machine.\n"
        f"Your worker_id is: {worker_id}\n"
        f"Your project_id is: {project_id}\n"
        f"Your MCP server endpoint is: {mcp_url}\n\n"
        "Use the connected `gmail-agent-bridge` MCP tools to drive your loop:\n"
        "1. Call `get_next_task(worker_id=\"" + worker_id + "\")` to atomically claim a task.\n"
        "2. If it returns null, wait 30-60s and retry; if still null after a few tries, exit.\n"
        "3. If a task is returned, switch to the matching repo "
        "(see `project_id`, which is typically `<owner>/<repo>` on GitHub) and read "
        "`handoff_instructions` carefully.\n"
        "4. Make the requested change. If requirements are unclear, call "
        "`send_email(to=<sender_email>, reply_to_thread_id=<thread_id>, ...)` to ask, "
        "then call `release_task(email_id, worker_id=\"" + worker_id + "\")` and exit.\n"
        "5. When done, open a PR and call "
        "`mark_task_complete(email_id, worker_id=\"" + worker_id + "\", "
        "resolution_note=<concise summary + PR link>)`.\n"
        "Only ever work on tasks you have successfully claimed via `get_next_task`. "
        "Never operate on `get_pending_tasks` results directly — that tool is read-only."
    )


async def maybe_spawn_worker(
    store: MongoTaskStore,
    devin: DevinClient,
    project_id: str,
) -> None:
    """Acquire the per-project lock and spawn a Devin session if not held."""
    session_id = f"clanker-{project_id.replace('/', '_')}-{uuid4().hex[:8]}"
    acquired = await store.try_acquire_worker_lock(
        project_id=project_id,
        session_id=session_id,
        lease_seconds=WORKER_LEASE_SECONDS,
    )
    if not acquired:
        logger.debug("Worker lock already held for project_id=%s; skipping spawn", project_id)
        return

    logger.info("Acquired worker lock for project_id=%s session_id=%s", project_id, session_id)
    prompt = build_worker_prompt(
        project_id=project_id,
        worker_id=session_id,
        mcp_url=MCP_PUBLIC_URL,
    )
    try:
        await devin.create_session(
            prompt=prompt,
            idempotent=True,
            title=f"clanker:{project_id}",
        )
    except Exception as exc:
        logger.exception("Failed to create Devin session for %s: %s", project_id, exc)
        await store.release_worker_lock(project_id)


async def watch_loop(store: MongoTaskStore, devin: DevinClient) -> None:
    """Tail the tasks change stream and spawn workers on pending events."""
    pipeline = [
        {
            "$match": {
                "$or": [
                    {"operationType": "insert", "fullDocument.status": "pending"},
                    {
                        "operationType": "update",
                        "updateDescription.updatedFields.status": "pending",
                    },
                ]
            }
        }
    ]

    while True:
        try:
            kwargs: dict[str, object] = {"full_document": "updateLookup"}
            token = await store.get_resume_token()
            if token:
                kwargs["resume_after"] = token
                logger.info("Resuming change stream from saved token")
            else:
                logger.info("Starting change stream without resume token")

            async with store.tasks.watch(pipeline, **kwargs) as stream:
                async for change in stream:
                    try:
                        await store.save_resume_token(change["_id"])
                        doc = change.get("fullDocument") or {}
                        project_id = doc.get("project_id")
                        if not isinstance(project_id, str) or not project_id:
                            logger.debug(
                                "Skipping change with no project_id (operationType=%s)",
                                change.get("operationType"),
                            )
                            continue
                        await maybe_spawn_worker(store, devin, project_id)
                    except Exception as exc:
                        logger.exception("Error handling change event: %s", exc)
        except PyMongoError as exc:
            logger.warning("Change stream error, reconnecting in 2s: %s", exc)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Unexpected watch_loop error, retrying in 5s: %s", exc)
            await asyncio.sleep(5)


async def reconcile_loop(store: MongoTaskStore, devin: DevinClient) -> None:
    """Periodically heal any pending projects missed by the change stream."""
    while True:
        try:
            project_ids = await store.list_pending_project_ids()
            for project_id in project_ids:
                await maybe_spawn_worker(store, devin, project_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("reconcile_loop error: %s", exc)
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


async def main() -> None:
    store = MongoTaskStore(
        uri=MONGODB_URI,
        db_name=MONGODB_DB,
        collection_name=MONGODB_TASKS_COLLECTION,
        default_lease_seconds=WORKER_LEASE_SECONDS,
    )
    await store.init_indexes()
    devin = DevinClient(api_key=DEVIN_API_KEY, base_url=DEVIN_API_URL)

    stop_event = asyncio.Event()

    def _signal_stop() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except NotImplementedError:
            pass  # Windows / non-main thread fallback

    logger.info(
        "Dispatcher starting (mcp_url=%s, lease=%ss, reconcile=%ss)",
        MCP_PUBLIC_URL,
        WORKER_LEASE_SECONDS,
        RECONCILE_INTERVAL_SECONDS,
    )

    watcher = asyncio.create_task(watch_loop(store, devin), name="watch_loop")
    reconciler = asyncio.create_task(reconcile_loop(store, devin), name="reconcile_loop")

    await stop_event.wait()
    for task in (watcher, reconciler):
        task.cancel()
    await asyncio.gather(watcher, reconciler, return_exceptions=True)
    store.close()
    logger.info("Dispatcher stopped")


if __name__ == "__main__":
    asyncio.run(main())
