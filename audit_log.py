from __future__ import annotations

import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger("audit_log")


class AuditLogger:
    """Writes Negotiator decisions to the audit_log MongoDB collection.

    Injected with a Motor collection so it shares the server's existing connection.
    """

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def record(
        self,
        decision_type: str,
        task_id: str,
        reasoning: str,
        candidate_message: str,
        definition_of_ready: str,
        conflicts_with: list[str],
    ) -> str:
        """Persist a Negotiator decision. Returns str(inserted_id) as audit_link.

        decision_type values: NEW | REDUNDANT | CONFLICT | READY | NEEDS_CLARIFICATION
        """
        doc = {
            "task_id": task_id,
            "decision_type": decision_type,
            "reasoning": reasoning,
            "candidate_message": candidate_message,
            "definition_of_ready": definition_of_ready,
            "conflicts_with": conflicts_with,
            "recorded_at": datetime.now(timezone.utc),
        }
        try:
            result = await self._col.insert_one(doc)
            audit_link = str(result.inserted_id)
            logger.info("Audit: %s task_id=%s audit_link=%s", decision_type, task_id, audit_link)
            return audit_link
        except Exception as e:
            logger.warning("AuditLogger failed to write for task_id=%s: %s", task_id, e)
            return ""
