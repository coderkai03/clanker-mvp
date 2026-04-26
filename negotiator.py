import logging
import os

import anthropic
from anthropic import AsyncAnthropic

logger = logging.getLogger("negotiator")

NEGOTIATION_MODEL = os.getenv("NEGOTIATION_MODEL", "claude-haiku-4-5-20251001")

_ASSESS_TOOL: dict = {
    "name": "assess_candidate",
    "description": "Assess a candidate task against the existing work queue.",
    "input_schema": {
        "type": "object",
        "required": [
            "is_redundant", "conflicts_with", "dependencies",
            "definition_of_ready", "conflict_severity",
            "suggested_files", "constraints", "reasoning",
        ],
        "properties": {
            "is_redundant":        {"type": "boolean"},
            "conflicts_with":      {"type": "array", "items": {"type": "string"}, "description": "email_ids of conflicting existing tasks"},
            "dependencies":        {"type": "array", "items": {"type": "string"}, "description": "email_ids the candidate depends on"},
            "definition_of_ready": {"type": "string", "enum": ["READY", "NOT_READY", "NEEDS_CLARIFICATION"]},
            "conflict_severity":   {"type": "string", "enum": ["none", "minor", "major"]},
            "suggested_files":     {"type": "array", "items": {"type": "string"}},
            "constraints":         {"type": "array", "items": {"type": "string"}},
            "reasoning":           {"type": "string"},
        },
    },
}

_SYSTEM_PROMPT = """\
You are a senior engineering lead reviewing a candidate task against an active backlog.

Your job:
1. REDUNDANCY: Is this candidate substantially the same ask as an existing task?
   - If yes: is_redundant=true, definition_of_ready=NOT_READY.
2. CONFLICT severity:
   - none: no conflict.
   - minor: overlapping scope or ambiguous ordering.
   - major: directly opposing goals on the same feature/resource, security regression, or data-loss risk.
   - If major: definition_of_ready=NOT_READY.
3. DEPENDENCIES: Does this candidate require another task to complete first?
4. DEFINITION OF READY:
   - READY: clear requirements, no blocking conflict, not redundant.
   - NOT_READY: redundant with or majorly conflicts with existing work.
   - NEEDS_CLARIFICATION: requirements are too vague to implement safely.
5. AGENT INSTRUCTIONS: Suggest likely files to touch and implementation constraints from the task description.

Be conservative: prefer NEEDS_CLARIFICATION over READY when requirements are genuinely ambiguous.
Call the assess_candidate tool with your assessment.\
"""

_READY_SHORTCIRCUIT = {
    "is_redundant": False,
    "conflicts_with": [],
    "dependencies": [],
    "definition_of_ready": "READY",
    "conflict_severity": "none",
    "suggested_files": [],
    "constraints": [],
}


def _summarise_existing(tasks: list[dict]) -> str:
    lines = []
    for t in tasks:
        reqs = t.get("extracted_requirements", [])[:3]
        reqs_str = "; ".join(reqs)[:150]
        lines.append(
            f"- email_id={t.get('email_id')} type={t.get('task_type')} "
            f"urgency={t.get('urgency')} status={t.get('status')}\n"
            f"  subject: {t.get('subject', '')}\n"
            f"  requirements: {reqs_str}"
        )
    return "\n".join(lines)


class Negotiator:
    def __init__(self) -> None:
        self._client = AsyncAnthropic()

    async def validate(self, candidate: dict, existing_tasks: list[dict]) -> dict:
        task_id = candidate.get("task_id", "unknown")
        classification = candidate.get("classification", {})

        if not existing_tasks:
            logger.debug("No existing tasks; short-circuiting negotiation for %s", task_id)
            return {
                "task_id": task_id,
                "classification": classification,
                "state_analysis": {
                    "is_redundant": False,
                    "conflicts_with": [],
                    "dependencies": [],
                    "definition_of_ready": "READY",
                },
                "agent_instructions": {
                    "suggested_files": [],
                    "constraints": [],
                },
                "error_code": None,
                "reasoning": "No existing tasks — short-circuited.",
            }

        user_msg = (
            f"CANDIDATE TASK\n"
            f"task_id: {task_id}\n"
            f"message: {candidate.get('original_message', '')}\n"
            f"category: {classification.get('category')}, "
            f"priority: {classification.get('priority')}, "
            f"is_actionable: {classification.get('is_actionable')}\n\n"
            f"EXISTING ACTIVE TASKS ({len(existing_tasks)} tasks)\n"
            f"{_summarise_existing(existing_tasks)}"
        )

        try:
            response = await self._client.messages.create(
                model=NEGOTIATION_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=[_ASSESS_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": user_msg}],
            )
        except anthropic.APIError as e:
            logger.warning("Negotiator API error for %s: %s", task_id, e)
            return {
                "task_id": task_id,
                "classification": classification,
                "state_analysis": {
                    "is_redundant": False,
                    "conflicts_with": [],
                    "dependencies": [],
                    "definition_of_ready": "NEW",
                },
                "agent_instructions": {
                    "suggested_files": [],
                    "constraints": [],
                },
                "metadata": {
                    "is_blocked": False,
                    "requires_intervention": False,
                    "audit_link": None,
                    "warning": "anthropic_api_unavailable",
                },
                "error_code": None,
                "reasoning": f"API error — fail-open: {e}",
            }

        tool_block = next(
            (b for b in response.content if b.type == "tool_use"),
            None,
        )
        if tool_block is None:
            logger.warning("Negotiator: no tool_use block for %s; defaulting NEEDS_CLARIFICATION", task_id)
            result = _READY_SHORTCIRCUIT.copy()
            result["definition_of_ready"] = "NEEDS_CLARIFICATION"
            result["reasoning"] = ""
        else:
            result = tool_block.input
            logger.debug("Negotiator reasoning for %s: %s", task_id, result.get("reasoning"))

        error_code = "NEEDS_HUMAN_INTERVENTION" if result.get("conflict_severity") == "major" else None

        return {
            "task_id": task_id,
            "classification": classification,
            "state_analysis": {
                "is_redundant": result.get("is_redundant", False),
                "conflicts_with": result.get("conflicts_with", []),
                "dependencies": result.get("dependencies", []),
                "definition_of_ready": result.get("definition_of_ready", "NEEDS_CLARIFICATION"),
            },
            "agent_instructions": {
                "suggested_files": result.get("suggested_files", []),
                "constraints": result.get("constraints", []),
            },
            "error_code": error_code,
            "reasoning": result.get("reasoning", ""),
        }
