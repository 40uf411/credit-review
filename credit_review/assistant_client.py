"""The analyst page's client for the regulation assistant: its HTTP API, nothing else.

The page is a client of `ca-assistant`, the service that holds the assistant: the guards, retrieval, memory, rate
limit and telemetry stay there. The key is the server's own setting and goes only into a request header, so the
browser never holds it.
"""

from __future__ import annotations

from typing import Any

import httpx

TRANSPORT: httpx.BaseTransport | None = None     # tests put an httpx.MockTransport here; None = the network


class AssistantError(RuntimeError):
    """Something the analyst can act on, in words fit to show them."""


class ConversationExpired(AssistantError):
    pass


class AssistantClient:
    def __init__(self, url: str, key: str, user: str, timeout: float = 60.0):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, transport=TRANSPORT,
                                 headers={"X-API-Key": key, "X-User-Id": user})

    def start(self) -> dict[str, Any]:
        """A new conversation: {id, remaining_messages}."""
        return self._call("POST", "/conversations")

    def ask(self, conversation_id: str, text: str) -> dict[str, Any]:
        """{answer, refused, code, citations: [{chunk_id, quote, source, title}], remaining_messages}."""
        return self._call("POST", f"/conversations/{conversation_id}/messages", json={"text": text})

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise AssistantError("The assistant did not answer. Try again in a moment.") from None
        if response.status_code == 401:
            raise AssistantError("The assistant did not accept this page's key.")
        if response.status_code == 404:
            raise ConversationExpired("This conversation has expired. Start a new one.")
        if response.status_code >= 400:
            raise AssistantError("The assistant did not answer. Try again in a moment.")
        return response.json()
