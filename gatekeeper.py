import logging
import os
import uuid

import anthropic
from anthropic import AsyncAnthropic

logger = logging.getLogger("gatekeeper")

TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "claude-haiku-4-5-20251001")

_TRIAGE_TOOL: dict = {
    "name": "classify_messages",
    "description": "Classify a batch of messages for actionability, category, and priority.",
    "input_schema": {
        "type": "object",
        "required": ["classifications"],
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["index", "category", "priority", "is_actionable", "reasoning"],
                    "properties": {
                        "index":         {"type": "integer"},
                        "category":      {"type": "string", "enum": ["bug_report", "feature_request", "feedback", "question", "unknown"]},
                        "priority":      {"type": "string", "enum": ["high", "medium", "low"]},
                        "is_actionable": {"type": "boolean"},
                        "reasoning":     {"type": "string"},
                    },
                },
            }
        },
    },
}

_SYSTEM_PROMPT = """\
You are a technical task classifier for a software engineering team.
For each message you receive, determine:
1. Is this an ACTIONABLE technical request requiring engineering work?
   Actionable: bug reports, feature requests, technical questions requiring investigation.
   NOT actionable: social messages ("Thanks!", "Sounds good"), scheduling ("Let's meet"), vague praise.
2. If actionable, assign the most specific category and priority.

Priority rules:
- high: explicit urgency, production impact, data loss risk, or security concern.
- medium: clear request with no stated urgency.
- low: nice-to-have, "whenever you get a chance", minor cosmetic.

You MUST call the classify_messages tool with exactly one entry per input message, preserving the original index.\
"""

_FALLBACK_ENTRY = {
    "category": "unknown",
    "priority": "low",
    "is_actionable": False,
}


class Gatekeeper:
    def __init__(self) -> None:
        self._client = AsyncAnthropic()

    async def triage(self, messages: list[str]) -> list[dict]:
        if not messages:
            return []

        numbered = "\n".join(f"[{i}] {msg}" for i, msg in enumerate(messages))
        try:
            response = await self._client.messages.create(
                model=TRIAGE_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=[_TRIAGE_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": numbered}],
            )
        except anthropic.APIError as e:
            logger.warning("Gatekeeper API error during triage: %s", e)
            return [
                {
                    "task_id": str(uuid.uuid4()),
                    "original_message": msg,
                    "classification": dict(_FALLBACK_ENTRY),
                }
                for msg in messages
            ]

        tool_block = next(
            (b for b in response.content if b.type == "tool_use"),
            None,
        )
        if tool_block is None:
            logger.warning("Gatekeeper: no tool_use block in response; falling back")
            return [
                {
                    "task_id": str(uuid.uuid4()),
                    "original_message": msg,
                    "classification": dict(_FALLBACK_ENTRY),
                }
                for msg in messages
            ]

        by_index: dict[int, dict] = {
            entry["index"]: entry
            for entry in tool_block.input.get("classifications", [])
        }

        results = []
        for i, msg in enumerate(messages):
            entry = by_index.get(i, {})
            results.append(
                {
                    "task_id": str(uuid.uuid4()),
                    "original_message": msg,
                    "classification": {
                        "category": entry.get("category", _FALLBACK_ENTRY["category"]),
                        "priority": entry.get("priority", _FALLBACK_ENTRY["priority"]),
                        "is_actionable": entry.get("is_actionable", _FALLBACK_ENTRY["is_actionable"]),
                    },
                }
            )
            if "reasoning" in entry:
                logger.debug("triage[%d] reasoning: %s", i, entry["reasoning"])

        return results
