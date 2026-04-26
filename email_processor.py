import base64
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTask:
    email_id: str
    thread_id: str
    sender: str
    sender_email: str
    subject: str
    received_at: str
    raw_text: str
    task_type: str   # bug_report | feature_request | feedback | question | unknown
    urgency: str     # high | medium | low
    extracted_requirements: list[str]
    context: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return {
            "email_id": self.email_id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "received_at": self.received_at,
            "raw_text": self.raw_text,
            "task_type": self.task_type,
            "urgency": self.urgency,
            "extracted_requirements": self.extracted_requirements,
            "context": self.context,
            "handoff_instructions": self._handoff_instructions(),
        }

    def _handoff_instructions(self) -> str:
        reqs = "\n".join(f"  - {r}" for r in self.extracted_requirements) or "  (none extracted — parse raw_text)"
        return (
            f"CLIENT EMAIL TASK [{self.task_type.upper()}] — urgency: {self.urgency}\n"
            f"From: {self.sender} <{self.sender_email}>\n"
            f"Subject: {self.subject}\n"
            f"Received: {self.received_at}\n\n"
            f"=== RAW CLIENT MESSAGE ===\n{self.raw_text}\n\n"
            f"=== EXTRACTED REQUIREMENTS ===\n{reqs}\n\n"
            f"=== AGENT INSTRUCTIONS ===\n"
            f"1. If requirements are clear → implement the requested changes.\n"
            f"2. If requirements are vague → call send_email(to='{self.sender_email}', ...) to ask for clarification.\n"
            f"3. When done → call send_email to notify the client, then mark_task_complete(email_id='{self.email_id}').\n"
        )


_URGENCY_KEYWORDS: dict[str, list[str]] = {
    "high": ["urgent", "asap", "immediately", "critical", "emergency", "blocker", "broken", "down", "production"],
    "low": ["whenever", "no rush", "low priority", "minor", "nice to have", "eventually", "someday"],
}

_TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "bug_report": ["bug", "error", "broken", "crash", "fail", "not working", "issue", "exception", "traceback"],
    "feature_request": ["feature", "add", "implement", "new", "could you", "would be nice", "can we", "request"],
    "feedback": ["feedback", "review", "looks like", "noticed", "suggestion", "improve", "looks good"],
    "question": ["question", "how do", "why does", "what is", "when will", "can you explain"],
}


class EmailProcessor:
    def to_agent_task(self, message: dict) -> AgentTask | None:
        """Convert a raw Gmail message dict into an AgentTask. Returns None on parse failure."""
        try:
            headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
            subject = headers.get("Subject", "(no subject)")
            from_header = headers.get("From", "unknown")
            date_header = headers.get("Date", "")

            m = re.match(r'^"?(.+?)"?\s*<(.+?)>$', from_header)
            sender_name = m.group(1).strip() if m else from_header
            sender_email = m.group(2) if m else from_header

            body = self._extract_body(message["payload"])
            if not body.strip():
                return None

            combined = subject + " " + body
            return AgentTask(
                email_id=message["id"],
                thread_id=message.get("threadId", message["id"]),
                sender=sender_name,
                sender_email=sender_email,
                subject=subject,
                received_at=date_header,
                raw_text=body,
                task_type=self._classify_task_type(combined),
                urgency=self._classify_urgency(combined),
                extracted_requirements=self._extract_requirements(body),
                context={"gmail_message_id": message["id"]},
            )
        except (KeyError, AttributeError, TypeError):
            return None

    def _extract_body(self, payload: dict) -> str:
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        for part in payload.get("parts", []):
            result = self._extract_body(part)
            if result:
                return result
        return ""

    def _classify_task_type(self, text: str) -> str:
        t = text.lower()
        for task_type, keywords in _TASK_TYPE_KEYWORDS.items():
            if any(kw in t for kw in keywords):
                return task_type
        return "unknown"

    def _classify_urgency(self, text: str) -> str:
        t = text.lower()
        if any(kw in t for kw in _URGENCY_KEYWORDS["high"]):
            return "high"
        if any(kw in t for kw in _URGENCY_KEYWORDS["low"]):
            return "low"
        return "medium"

    def _extract_requirements(self, text: str) -> list[str]:
        reqs: list[str] = []

        # Numbered or bulleted list items
        for m in re.finditer(r"^[ \t]*(?:[-•*]|\d+[.)]) +(.+)$", text, re.MULTILINE):
            item = m.group(1).strip()
            if len(item) > 10:
                reqs.append(item)

        # Fall back to sentences containing action verbs
        if not reqs:
            action_verbs = ["fix", "add", "change", "update", "remove", "create",
                            "implement", "modify", "make", "ensure", "refactor", "delete"]
            for sentence in re.split(r"[.!?\n]+", text):
                s = sentence.strip()
                if len(s) > 20 and any(v in s.lower() for v in action_verbs):
                    reqs.append(s)

        return reqs[:10]
