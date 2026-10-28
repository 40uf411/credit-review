"""Azure AI Content Safety Prompt Shields: detects prompt attacks in a user's message (direct injection, jailbreak)
and in documents the model will read (indirect injection).

Signs in with Entra ID (the Content Safety resource has local auth disabled). Any failure raises ShieldsError; the
guard that calls this treats a failure as a refusal, never as "no attack".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

API_VERSION = "2024-09-01"
SCOPE = "https://cognitiveservices.azure.com/.default"
MAX_CHARS = 10_000          # the service's limit for the user prompt and for all documents together
MAX_DOCUMENTS = 5           # the service's limit on documents per call


class ShieldsError(Exception):
    """Prompt Shields could not give an answer (network, auth, quota, malformed reply)."""


@dataclass(frozen=True)
class ShieldResult:
    user_attack: bool
    document_attacks: tuple[bool, ...] = ()

    @property
    def attack(self) -> bool:
        return self.user_attack or any(self.document_attacks)


def _azure_error(response: httpx.Response) -> str:
    """Azure's own error code and message (never the text that was sent), for the logs."""
    try:
        error = response.json().get("error", {})
        return f": {error.get('code', '')} {error.get('message', '')}".rstrip()[:200]
    except Exception:
        return ""


class PromptShields:
    def __init__(self, endpoint: str, token: Callable[[], str] | None = None, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        if token is None:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            token = get_bearer_token_provider(DefaultAzureCredential(), SCOPE)
        self.url = endpoint.rstrip("/") + "/contentsafety/text:shieldPrompt"
        self.token = token
        self.client = httpx.Client(timeout=timeout, transport=transport)

    def analyze(self, user_prompt: str, documents: Sequence[str] = ()) -> ShieldResult:
        if len(user_prompt) > MAX_CHARS or sum(len(d) for d in documents) > MAX_CHARS:
            raise ShieldsError("text longer than Prompt Shields accepts")
        if len(documents) > MAX_DOCUMENTS:
            raise ShieldsError(f"more than {MAX_DOCUMENTS} documents in one Prompt Shields call")
        try:
            response = self.client.post(
                self.url, params={"api-version": API_VERSION},
                headers={"Authorization": f"Bearer {self.token()}"},
                json={"userPrompt": user_prompt, "documents": list(documents)})
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            user = body["userPromptAnalysis"]["attackDetected"]
            docs = tuple(d["attackDetected"] for d in body.get("documentsAnalysis", []))
        except ShieldsError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ShieldsError(f"Prompt Shields failed (HTTP {exc.response.status_code}{_azure_error(exc.response)})") from exc
        except Exception as exc:     # anything else: no answer, so no pass
            raise ShieldsError(f"Prompt Shields failed ({type(exc).__name__})") from exc
        if not isinstance(user, bool) or not all(isinstance(d, bool) for d in docs) or len(docs) != len(documents):
            raise ShieldsError("Prompt Shields returned an unexpected reply")
        return ShieldResult(user_attack=user, document_attacks=docs)
