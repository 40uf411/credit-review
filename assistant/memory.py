"""Conversation memory: the turns of each conversation, bound to the user who started it.

Azure Table Storage in the service (partition = user, row = conversation), so another user's conversation id is
simply not found. Conversations expire after TTL; a 6th question is refused by the guards, not here.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from assistant.models import Conversation, Turn

TTL = timedelta(hours=24)
USER_ID = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")       # also safe as a Table Storage partition key


class InvalidUser(ValueError):
    pass


def _check_user(user_id: str) -> str:
    if not USER_ID.match(user_id or ""):
        raise InvalidUser("user id must be 1-128 characters: letters, digits, . _ @ -")
    return user_id


class ConversationStore(Protocol):
    def create(self, user_id: str) -> Conversation: ...
    def get(self, conversation_id: str, user_id: str) -> Conversation | None: ...
    def save(self, conversation: Conversation) -> None: ...


def _new(user_id: str) -> Conversation:
    return Conversation(id=secrets.token_hex(12), user_id=_check_user(user_id))


class InMemoryStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], tuple[datetime, list[Turn]]] = {}

    def create(self, user_id: str) -> Conversation:
        conversation = _new(user_id)
        self._items[(user_id, conversation.id)] = (datetime.now(UTC), [])
        return conversation

    def get(self, conversation_id: str, user_id: str) -> Conversation | None:
        item = self._items.get((user_id, conversation_id))
        if item is None or datetime.now(UTC) - item[0] > TTL:
            return None
        return Conversation(id=conversation_id, user_id=user_id, turns=list(item[1]))

    def save(self, conversation: Conversation) -> None:
        created, _ = self._items[(conversation.user_id, conversation.id)]
        self._items[(conversation.user_id, conversation.id)] = (created, list(conversation.turns))


class TableStore:
    def __init__(self, endpoint: str, table: str = "conversations", client: Any = None):
        if client is None:
            from azure.data.tables import TableClient
            from azure.identity import DefaultAzureCredential
            client = TableClient(endpoint, table, credential=DefaultAzureCredential())
        self.client = client

    def create(self, user_id: str) -> Conversation:
        conversation = _new(user_id)
        self.client.create_entity({"PartitionKey": user_id, "RowKey": conversation.id, "turns": "[]",
                                   "created_at": datetime.now(UTC).isoformat()})
        return conversation

    def get(self, conversation_id: str, user_id: str) -> Conversation | None:
        from azure.core.exceptions import ResourceNotFoundError
        if not USER_ID.match(user_id or "") or not re.fullmatch(r"[0-9a-f]{24}", conversation_id or ""):
            return None
        try:
            entity = self.client.get_entity(partition_key=user_id, row_key=conversation_id)
        except ResourceNotFoundError:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(entity["created_at"]) > TTL:
            return None
        turns = [Turn(role=t["role"], text=t["text"]) for t in json.loads(entity["turns"])]
        return Conversation(id=conversation_id, user_id=user_id, turns=turns)

    def save(self, conversation: Conversation) -> None:
        turns = json.dumps([{"role": t.role, "text": t.text} for t in conversation.turns], ensure_ascii=False)
        self.client.update_entity({"PartitionKey": conversation.user_id, "RowKey": conversation.id,
                                   "turns": turns}, mode="merge")
