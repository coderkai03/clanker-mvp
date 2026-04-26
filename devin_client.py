"""Thin async wrapper around the Devin REST API.

Used by `dispatcher.py` to spawn one Devin session per project that has
pending tasks. Each session is a worker that loops `get_next_task` /
`mark_task_complete` against the MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("devin-client")


class DevinClient:
    """Async client for `POST {base_url}/sessions` and friends."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.devin.ai/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("DEVIN_API_KEY is required to construct DevinClient")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def create_session(
        self,
        *,
        prompt: str,
        idempotent: bool = True,
        playbook_id: str | None = None,
        snapshot_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Devin session. Returns the API response body."""
        body: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent}
        if playbook_id:
            body["playbook_id"] = playbook_id
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        if title:
            body["title"] = title

        url = f"{self._base_url}/sessions"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=self._headers, json=body)
        if resp.status_code >= 400:
            logger.error(
                "Devin create_session failed: status=%s body=%s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        logger.info(
            "Devin session created: session_id=%s is_new=%s",
            data.get("session_id"),
            data.get("is_new_session"),
        )
        return data
