"""MongoDB-backed task store for multi-session safe task coordination.

Replaces the in-process `_pending_tasks` list with an atomic claim model
so multiple Devin sessions can call MCP tools concurrently without
collisions and without losing tasks across server restarts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ReturnDocument

from email_processor import AgentTask

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

_URGENCY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _urgency_rank(urgency: str) -> int:
    return _URGENCY_RANK.get(urgency, 1)


def _task_to_doc(task: AgentTask, project_id: str | None = None) -> dict[str, Any]:
    """Project an AgentTask into a Mongo document with coordination fields."""
    now = _now()
    return {
        "email_id": task.email_id,
        "thread_id": task.thread_id,
        "sender": task.sender,
        "sender_email": task.sender_email,
        "subject": task.subject,
        "received_at": task.received_at,
        "raw_text": task.raw_text,
        "task_type": task.task_type,
        "urgency": task.urgency,
        "urgency_rank": _urgency_rank(task.urgency),
        "extracted_requirements": list(task.extracted_requirements),
        "context": dict(task.context),
        "handoff_instructions": task._handoff_instructions(),
        "status": STATUS_PENDING,
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "created_at": now,
        "completed_at": None,
        "project_id": project_id,
        "resolution_note": None,
        "metadata": {
            "is_blocked": False,
            "requires_intervention": False,
            "audit_link": None,
        },
    }


def _strip_internal(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop Mongo-internal fields before returning a doc to MCP clients."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("created_at", "claimed_at", "lease_expires_at", "completed_at"):
        val = out.get(key)
        if isinstance(val, datetime):
            out[key] = val.astimezone(timezone.utc).isoformat()
    return out


class MongoTaskStore:
    """Async MongoDB-backed store for AgentTask coordination across sessions."""

    def __init__(
        self,
        uri: str,
        db_name: str,
        collection_name: str,
        default_lease_seconds: int = 1800,
    ) -> None:
        self._client = AsyncIOMotorClient(uri)
        self._db = self._client[db_name]
        self.tasks: AsyncIOMotorCollection = self._db[collection_name]
        self.clients: AsyncIOMotorCollection = self._db["clients"]
        self.escalations: AsyncIOMotorCollection = self._db["escalations"]
        self.audit_log: AsyncIOMotorCollection = self._db["audit_log"]
        self.default_lease_seconds = default_lease_seconds

    async def init_indexes(self) -> None:
        """Create indexes required for atomic claims and idempotent inserts."""
        await self.clients.create_index("email", unique=True)
        await self.tasks.create_index("email_id", unique=True)
        await self.tasks.create_index(
            [("status", 1), ("urgency_rank", 1), ("received_at", 1)]
        )
        await self.tasks.create_index([("status", 1), ("lease_expires_at", 1)])
        await self.escalations.create_index("candidate_task_id")
        await self.escalations.create_index("created_at")
        await self.audit_log.create_index("task_id")
        await self.audit_log.create_index("recorded_at")

    async def insert_pending(self, task: AgentTask, project_id: str | None = None) -> bool:
        """Insert a new pending task. Returns False if email_id already exists."""
        try:
            await self.tasks.insert_one(_task_to_doc(task, project_id=project_id))
            return True
        except Exception as e:
            if "duplicate key" in str(e).lower() or "E11000" in str(e):
                return False
            raise

    async def list_pending(
        self,
        urgency: str | None = None,
        task_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return tasks claimable right now (pending OR lease-expired)."""
        now = _now()
        claimable = {
            "$or": [
                {"status": STATUS_PENDING},
                {"status": STATUS_IN_PROGRESS, "lease_expires_at": {"$lt": now}},
            ]
        }
        filters: list[dict[str, Any]] = [claimable]
        if urgency:
            filters.append({"urgency": urgency})
        if task_type:
            filters.append({"task_type": task_type})
        query = {"$and": filters} if len(filters) > 1 else filters[0]

        cursor = self.tasks.find(query).sort(
            [("urgency_rank", 1), ("received_at", 1)]
        )
        return [_strip_internal(doc) async for doc in cursor]

    async def claim_next(
        self,
        worker_id: str,
        lease_seconds: int | None = None,
        urgency: str | None = None,
        task_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one task. Returns None if no claimable task exists."""
        lease = lease_seconds or self.default_lease_seconds
        now = _now()
        expires = now + timedelta(seconds=lease)

        claimable = {
            "$or": [
                {"status": STATUS_PENDING},
                {"status": STATUS_IN_PROGRESS, "lease_expires_at": {"$lt": now}},
            ]
        }
        filters: list[dict[str, Any]] = [claimable]
        if urgency:
            filters.append({"urgency": urgency})
        if task_type:
            filters.append({"task_type": task_type})
        query = {"$and": filters} if len(filters) > 1 else filters[0]

        doc = await self.tasks.find_one_and_update(
            query,
            {
                "$set": {
                    "status": STATUS_IN_PROGRESS,
                    "claimed_by": worker_id,
                    "claimed_at": now,
                    "lease_expires_at": expires,
                }
            },
            sort=[("urgency_rank", 1), ("received_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return _strip_internal(doc) if doc else None

    async def complete(
        self,
        email_id: str,
        worker_id: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Mark task completed iff caller currently owns the claim."""
        now = _now()
        doc = await self.tasks.find_one_and_update(
            {
                "email_id": email_id,
                "claimed_by": worker_id,
                "status": STATUS_IN_PROGRESS,
            },
            {
                "$set": {
                    "status": STATUS_COMPLETED,
                    "completed_at": now,
                    "resolution_note": resolution_note,
                    "lease_expires_at": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return _strip_internal(doc) if doc else None

    async def release(
        self,
        email_id: str,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """Voluntarily release a claim back to pending."""
        doc = await self.tasks.find_one_and_update(
            {
                "email_id": email_id,
                "claimed_by": worker_id,
                "status": STATUS_IN_PROGRESS,
            },
            {
                "$set": {
                    "status": STATUS_PENDING,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return _strip_internal(doc) if doc else None

    async def existing_email_ids(self) -> set[str]:
        """Return all email_ids the store already knows about (any status)."""
        cursor = self.tasks.find({}, {"email_id": 1, "_id": 0})
        return {doc["email_id"] async for doc in cursor}

    async def queue_size(self) -> int:
        """Count of tasks in pending or in_progress states."""
        return await self.tasks.count_documents(
            {"status": {"$in": [STATUS_PENDING, STATUS_IN_PROGRESS]}}
        )

    async def list_for_negotiation(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return condensed active tasks for Negotiator LLM context (sliding window).

        Capped at 15 most recent pending/in_progress tasks matching project_id.
        When project_id is provided, tasks from other projects are excluded —
        no cross-project comparisons are ever made.
        """
        query: dict[str, Any] = {
            "status": {
                "$in": [STATUS_PENDING, STATUS_IN_PROGRESS],
                "$ne": STATUS_COMPLETED,
            }
        }
        if project_id is not None:
            query["project_id"] = project_id
        cursor = self.tasks.find(
            query,
            {
                "_id": 0,
                "email_id": 1,
                "task_type": 1,
                "urgency": 1,
                "subject": 1,
                "extracted_requirements": 1,
                "status": 1,
            },
        ).sort([("received_at", -1)]).limit(15)
        return [doc async for doc in cursor]

    async def lookup_client(self, email: str) -> dict[str, Any] | None:
        """Return the client doc for email, or None if not registered."""
        return await self.clients.find_one({"email": email}, {"_id": 0})

    async def register_client(self, email: str, project_id: str) -> dict[str, Any]:
        """Upsert a client email → project_id mapping. Returns the stored doc."""
        now = _now()
        doc = await self.clients.find_one_and_update(
            {"email": email},
            {
                "$set": {"email": email, "project_id": project_id, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return {k: v for k, v in doc.items() if k != "_id"}

    async def insert_escalation(self, doc: dict[str, Any]) -> str:
        """Persist a major-conflict escalation. Returns str(inserted_id)."""
        result = await self.escalations.insert_one(doc)
        return str(result.inserted_id)

    def close(self) -> None:
        self._client.close()
