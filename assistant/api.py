"""The assistant over HTTP.

    POST /conversations                      start one (header X-User-Id) → {id, remaining_messages}
    POST /conversations/{id}/messages        ask → {answer, citations, refused, code, remaining_messages}
    GET  /conversations/{id}                 the turns so far
    GET  /healthz, /readyz

    APP_MODULE=assistant.api:app uvicorn ...      (the same image as the credit-review API)

Every route but the health checks needs X-API-Key. X-User-Id names the end user on whose behalf a trusted client
calls; a conversation is bound to it, so another user's id gets 404. In production the user comes from the caller's
Entra ID token instead (docs/adr).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from credit_review import telemetry

telemetry.setup("assistant")

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from assistant.chat import Chat  # noqa: E402
from assistant.config import Settings, load_topic  # noqa: E402
from assistant.guards import MAX_CHARS, Guards, RateLimiter  # noqa: E402
from assistant.memory import ConversationStore, InvalidUser, TableStore  # noqa: E402
from assistant.retriever import AzureEmbedder, AzureSearchRetriever  # noqa: E402
from assistant.shields import PromptShields  # noqa: E402
from credit_review.llm import LLMClient, LLMConfig  # noqa: E402
from credit_review.security import require_key  # noqa: E402
from credit_review.storage import store_from_env  # noqa: E402

app = FastAPI(title="Regulation assistant", version="0.1.0",
              description="Answers questions on EU, Belgian, French and Dutch mortgage-lending rules, with citations "
                          "to the sources. At most 5 questions per conversation.")


@lru_cache(maxsize=1)
def get_store() -> ConversationStore:
    return TableStore(Settings.from_env().table_endpoint)


@lru_cache(maxsize=1)
def get_chat() -> Chat:
    settings = Settings.from_env()
    if settings.missing():
        raise RuntimeError("missing settings: " + ", ".join(settings.missing()))
    config = LLMConfig.from_env()
    llm = LLMClient(config, store=store_from_env("llm-cache", config.cache_dir))
    guards = Guards(PromptShields(settings.shields_endpoint), llm, load_topic(), RateLimiter())
    retriever = AzureSearchRetriever(settings.search_endpoint, settings.search_index,
                                     AzureEmbedder(settings.openai_endpoint, settings.embedding_deployment))
    return Chat(guards, retriever, llm, get_store())


UserId = Annotated[str, Header(alias="X-User-Id", description="The end user the conversation belongs to.")]


class Message(BaseModel):
    text: str = Field(max_length=MAX_CHARS * 2)    # the guard gives the friendly refusal; this stops huge bodies


def _conversation(store: ConversationStore, conversation_id: str, user_id: str):
    conversation = store.get(conversation_id, user_id)
    if conversation is None:                 # missing, expired, or someone else's: the same answer for all three
        raise HTTPException(404, "no such conversation")
    return conversation


@app.post("/conversations", status_code=201, dependencies=[Depends(require_key)], summary="Start a conversation")
def create_conversation(user_id: UserId, store: Annotated[ConversationStore, Depends(get_store)],
                        chat: Annotated[Chat, Depends(get_chat)]) -> dict[str, Any]:
    try:
        conversation = store.create(user_id)
    except InvalidUser as exc:
        raise HTTPException(422, str(exc)) from None
    return {"id": conversation.id, "remaining_messages": chat.remaining(conversation)}


@app.post("/conversations/{conversation_id}/messages", dependencies=[Depends(require_key)], summary="Ask a question")
def ask(conversation_id: str, message: Message, user_id: UserId,
        store: Annotated[ConversationStore, Depends(get_store)],
        chat: Annotated[Chat, Depends(get_chat)]) -> dict[str, Any]:
    reply = chat.ask(_conversation(store, conversation_id, user_id), message.text)
    return {"answer": reply.text, "refused": reply.refused, "code": reply.code,
            "citations": [{"chunk_id": c.chunk_id, "quote": c.quote} for c in reply.citations],
            "remaining_messages": reply.remaining_messages}


@app.get("/conversations/{conversation_id}", dependencies=[Depends(require_key)], summary="The conversation so far")
def get_conversation(conversation_id: str, user_id: UserId,
                     store: Annotated[ConversationStore, Depends(get_store)],
                     chat: Annotated[Chat, Depends(get_chat)]) -> dict[str, Any]:
    conversation = _conversation(store, conversation_id, user_id)
    return {"id": conversation.id, "turns": [{"role": t.role, "text": t.text} for t in conversation.turns],
            "remaining_messages": chat.remaining(conversation)}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    missing = Settings.from_env().missing()
    return JSONResponse({"status": "not ready" if missing else "ready"}, status_code=503 if missing else 200)
