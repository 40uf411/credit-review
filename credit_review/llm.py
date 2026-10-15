"""One interface, two backends: Anthropic API and any OpenAI-compatible server (LM Studio).

Every call returns JSON matching a schema, and every result is cached on disk,
keyed by everything that influences it (provider, model, prompts, schema, image
hashes). Re-running a dossier therefore costs nothing and gives identical output,
and `offline=True` guarantees no network call is made: the demo is reproducible.

Configuration (environment variables or a `.env` file in the working directory):

    CREDIT_REVIEW_LLM_PROVIDER   anthropic | lmstudio | openai          (default: anthropic)
    CREDIT_REVIEW_LLM_MODEL      model id; for LM Studio the loaded model's id
                          (if empty, the first model the server lists is used)
    CREDIT_REVIEW_LLM_VISION_MODEL  optional separate model for scanned documents
    CREDIT_REVIEW_LLM_BASE_URL   default http://localhost:1234/v1 for lmstudio
    CREDIT_REVIEW_LLM_API_KEY    falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY
    CREDIT_REVIEW_LLM_OFFLINE    1 = cache only, never call a model
    CREDIT_REVIEW_CACHE_DIR      default .credit_review_cache
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_LMSTUDIO_URL = "http://localhost:1234/v1"


class LLMError(RuntimeError):
    pass


class CacheMiss(LLMError):
    pass


# ------------------------------------------------------------------------ config

class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai"] = "anthropic"
    model: str = ""
    vision_model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.0           # OpenAI-compatible only; current Claude models fix sampling
    max_tokens: int = 16000           # room for models that think before answering
    timeout: float = 300.0
    cache_dir: Path = Path(".credit_review_cache")
    offline: bool = False
    structured: Literal["auto", "json_schema", "prompt"] = "auto"
    label: str = ""                    # how the provider is shown to users

    @classmethod
    def from_env(cls, env_file: str | Path = ".env", **overrides: Any) -> "LLMConfig":
        env = dict(_read_env_file(Path(env_file)))
        env.update(os.environ)
        provider = env.get("CREDIT_REVIEW_LLM_PROVIDER", "anthropic").strip().lower()
        label = provider
        if provider == "lmstudio":
            provider = "openai"
        if provider not in ("anthropic", "openai"):
            raise LLMError(f"unknown CREDIT_REVIEW_LLM_PROVIDER {provider!r}")
        if provider == "anthropic":
            api_key = env.get("CREDIT_REVIEW_LLM_API_KEY") or env.get("ANTHROPIC_API_KEY", "")
            base_url = env.get("CREDIT_REVIEW_LLM_BASE_URL", "")
            model = env.get("CREDIT_REVIEW_LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL
        else:
            api_key = env.get("CREDIT_REVIEW_LLM_API_KEY") or env.get("OPENAI_API_KEY") or "lm-studio"
            base_url = env.get("CREDIT_REVIEW_LLM_BASE_URL") or DEFAULT_LMSTUDIO_URL
            model = env.get("CREDIT_REVIEW_LLM_MODEL", "")
        values: dict[str, Any] = dict(
            provider=provider, label=label, model=model, base_url=base_url, api_key=api_key,
            vision_model=env.get("CREDIT_REVIEW_LLM_VISION_MODEL", ""),
            offline=env.get("CREDIT_REVIEW_LLM_OFFLINE", "0").strip().lower() in ("1", "true", "yes"),
            cache_dir=Path(env.get("CREDIT_REVIEW_CACHE_DIR", ".credit_review_cache")),
        )
        values.update(overrides)
        return cls(**values)


def _read_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            yield key.strip(), value.strip().strip('"').strip("'")


# ------------------------------------------------------------------- request/result

@dataclass
class LLMRequest:
    purpose: str                       # classify | extract | judge
    system: str
    prompt: str
    schema: dict[str, Any]
    schema_name: str
    images: list[bytes] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)   # logged, never sent to the model


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    mode: str
    cache_key: str
    cached: bool
    usage: dict[str, Any]
    seconds: float


class Provider(Protocol):
    name: str

    def complete(self, req: LLMRequest, model: str, max_tokens: int) -> tuple[dict, dict, str, str]:
        """Return (data, usage, mode, raw_text)."""


# ------------------------------------------------------------------------ helpers

def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def media_type(img: bytes) -> str:
    if img[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    raise LLMError("images must be JPEG or PNG")


def parse_json_text(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply (handles <think> blocks and code fences)."""
    if not text:
        raise LLMError("empty reply from the model")
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    cleaned = re.sub(r"```(?:json)?", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise LLMError(f"no JSON object in reply: {text[:200]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise LLMError(f"reply is not valid JSON ({exc}): {text[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise LLMError("reply JSON is not an object")
    return obj


def _schema_instruction(req: LLMRequest) -> str:
    return (
        "\n\nRespond with a single JSON object only — no prose, no code fences — "
        f"that validates against this JSON Schema:\n{json.dumps(req.schema, ensure_ascii=False)}"
    )


def schema_problems(data: Any, schema: dict[str, Any], path: str = "$", limit: int = 15) -> list[str]:
    """Structural check of a reply: containers and required keys. Scalar types are coerced
    later by the extractor, so only problems that would lose data are reported."""
    problems: list[str] = []

    def walk(node: Any, sch: dict[str, Any], at: str) -> None:
        if len(problems) >= limit:
            return
        kind = sch.get("type")
        if kind == "object":
            if not isinstance(node, dict):
                problems.append(f"{at}: expected an object")
                return
            for key in sch.get("required", []):
                if key not in node:
                    problems.append(f"{at}.{key}: missing")
            for key, sub in sch.get("properties", {}).items():
                if key in node:
                    walk(node[key], sub, f"{at}.{key}")
        elif kind == "array":
            if not isinstance(node, list):
                problems.append(f"{at}: expected an array")
                return
            for i, item in enumerate(node):
                walk(item, sch.get("items", {}), f"{at}[{i}]")

    walk(data, schema, path)
    return problems


def _schema_id(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


class _JSONProvider:
    """Strict schema-constrained decoding when the server accepts the schema; otherwise
    the schema goes in the prompt, the reply is checked, and the model gets one chance
    to repair it. The fallback is remembered per schema, so small schemas stay strict."""

    name = "base"
    strict_enabled = True

    def __init__(self) -> None:
        self.prompt_only: dict[str, str] = {}      # schema id -> why strict decoding was refused

    def complete(self, req: LLMRequest, model: str, max_tokens: int):
        sid = _schema_id(req.schema)
        if self.strict_enabled and sid not in self.prompt_only:
            try:
                text, usage = self._send(req, model, max_tokens, strict=True)
                return parse_json_text(text), usage, "json_schema", text
            except Exception as exc:
                if not self._strict_refused(exc):
                    raise
                self.prompt_only[sid] = str(exc)[:300]

        text, usage = self._send(req, model, max_tokens, strict=False)
        data, problems = self._read(text, req.schema)
        if not problems:
            return data, usage, "prompt", text
        fix = ("Your reply does not match the JSON Schema:\n- " + "\n- ".join(problems)
               + "\nReply again with the complete, corrected JSON object only.")
        text2, usage2 = self._send(req, model, max_tokens, strict=False, repair=(text, fix))
        data2, problems2 = self._read(text2, req.schema)
        if data2 is None:
            raise LLMError("reply was not valid JSON, also after a repair request: " + "; ".join(problems2[:3]))
        usage = {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in ("input_tokens", "output_tokens")}
        return data2, usage, "prompt+repair", text2

    @staticmethod
    def _read(text: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            data = parse_json_text(text)
        except LLMError as exc:
            return None, [str(exc)]
        return data, schema_problems(data, schema)

    def _strict_refused(self, exc: Exception) -> bool:
        raise NotImplementedError

    def _send(self, req: LLMRequest, model: str, max_tokens: int, strict: bool,
              repair: tuple[str, str] | None = None) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


# --------------------------------------------------------------------- Anthropic

class AnthropicProvider(_JSONProvider):
    name = "anthropic"

    def __init__(self, config: LLMConfig, client: Any = None):
        super().__init__()
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMError("pip install anthropic") from exc
            kwargs: dict[str, Any] = {"timeout": config.timeout, "max_retries": 2}
            if config.api_key:
                kwargs["api_key"] = config.api_key
            if config.base_url:
                kwargs["base_url"] = config.base_url
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def _strict_refused(self, exc: Exception) -> bool:
        # 400: schema too complex to compile / unsupported; TypeError: SDK without output_config
        return isinstance(exc, TypeError) or _status(exc) == 400

    def _send(self, req, model, max_tokens, strict, repair=None):
        content: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type(img),
                                         "data": base64.b64encode(img).decode("ascii")}}
            for img in req.images
        ]
        content.append({"type": "text", "text": req.prompt if strict else req.prompt + _schema_instruction(req)})
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        if repair:
            messages += [{"role": "assistant", "content": repair[0] or "{}"},
                         {"role": "user", "content": repair[1]}]
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=req.system, messages=messages)
        if strict:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": req.schema}}
        resp = self.client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise LLMError("reply was cut off at max_tokens; raise LLMConfig.max_tokens")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        u = getattr(resp, "usage", None)
        return text, {"input_tokens": getattr(u, "input_tokens", None),
                      "output_tokens": getattr(u, "output_tokens", None)}


# ------------------------------------------------------- OpenAI-compatible (LM Studio)

class OpenAICompatProvider(_JSONProvider):
    name = "openai"

    def __init__(self, config: LLMConfig, client: Any = None):
        super().__init__()
        if client is None:
            try:
                import openai
            except ImportError as exc:
                raise LLMError("pip install openai") from exc
            client = openai.OpenAI(base_url=config.base_url or None, api_key=config.api_key or "lm-studio",
                                   timeout=config.timeout, max_retries=1)
        self.client = client
        self.temperature = config.temperature
        self.strict_enabled = config.structured in ("auto", "json_schema")
        self.allow_fallback = config.structured == "auto"

    def list_models(self) -> list[str]:
        return [m.id for m in self.client.models.list().data]

    def _strict_refused(self, exc: Exception) -> bool:
        return self.allow_fallback and _status(exc) in (400, 422)

    def _send(self, req, model, max_tokens, strict, repair=None):
        prompt = req.prompt if strict else req.prompt + _schema_instruction(req)
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        parts += [{"type": "image_url",
                   "image_url": {"url": f"data:{media_type(img)};base64," + base64.b64encode(img).decode("ascii")}}
                  for img in req.images]
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system},
                                          {"role": "user", "content": parts}]
        if repair:
            messages += [{"role": "assistant", "content": repair[0] or "{}"},
                         {"role": "user", "content": repair[1]}]
        kwargs: dict[str, Any] = dict(model=model, temperature=self.temperature, max_tokens=max_tokens,
                                      messages=messages)
        if strict:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": req.schema_name, "strict": True, "schema": req.schema}}
        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError("reply was cut off at max_tokens; raise max_tokens or the server context length")
        u = getattr(resp, "usage", None)
        return choice.message.content or "", {"input_tokens": getattr(u, "prompt_tokens", None),
                                              "output_tokens": getattr(u, "completion_tokens", None)}


# ------------------------------------------------------------------------- client

class LLMClient:
    """Cache-first facade. Providers are injectable for tests and replay."""

    def __init__(self, config: LLMConfig | None = None, provider: Provider | None = None):
        self.config = config or LLMConfig.from_env()
        self._provider = provider

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            cls = AnthropicProvider if self.config.provider == "anthropic" else OpenAICompatProvider
            self._provider = cls(self.config)
        return self._provider

    def describe(self) -> str:
        c = self.config
        name = c.label or c.provider
        model = c.model or "(first loaded model)"
        vision = f", vision: {c.vision_model}" if c.vision_model else ""
        return f"{name} · {model}{vision}" + (" · offline (cache only)" if c.offline else "")

    def resolve_model(self, needs_vision: bool) -> str:
        model = (self.config.vision_model if needs_vision and self.config.vision_model else self.config.model)
        if not model and isinstance(self.provider, OpenAICompatProvider):
            models = self.provider.list_models()
            if not models:
                raise LLMError("the OpenAI-compatible server lists no models; load one in LM Studio")
            model = models[0]
            self.config.model = model
        if not model:
            raise LLMError("no model configured (CREDIT_REVIEW_LLM_MODEL)")
        return model

    def cache_key(self, req: LLMRequest, model: str) -> str:
        payload = {
            "provider": self.config.provider, "base_url": self.config.base_url if self.config.provider == "openai" else "",
            "model": model, "system": req.system, "prompt": req.prompt, "schema": req.schema,
            "images": [hashlib.sha256(i).hexdigest() for i in req.images],
        }   # max_tokens is deliberately not part of the key: it never changes an answer, only truncates it
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.config.cache_dir / "llm" / key[:2] / f"{key}.json"

    def complete(self, req: LLMRequest) -> LLMResult:
        offline = self.config.offline
        model = self.config.vision_model if (req.images and self.config.vision_model) else self.config.model
        if not model and offline:
            raise CacheMiss("offline mode needs CREDIT_REVIEW_LLM_MODEL to find cached results")
        if not model:
            model = self.resolve_model(bool(req.images))
        key = self.cache_key(req, model)
        path = self._cache_path(key)
        if path.exists():
            entry = json.loads(path.read_text(encoding="utf-8"))
            return LLMResult(data=entry["data"], provider=entry["provider"], model=entry["model"],
                             mode=entry["mode"], cache_key=key, cached=True,
                             usage=entry.get("usage", {}), seconds=0.0)
        if offline:
            raise CacheMiss(f"no cached {req.purpose} result for {req.meta.get('doc', '?')} "
                            f"with {self.config.provider}/{model}; run once online to fill the cache")
        started = time.monotonic()
        try:
            data, usage, mode, raw = self.provider.complete(req, model, self.config.max_tokens)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{self.config.provider} call failed ({type(exc).__name__}): {str(exc)[:400]}") from exc
        seconds = round(time.monotonic() - started, 2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "cache_key": key, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider": self.config.provider, "model": model, "mode": mode, "purpose": req.purpose,
            "meta": req.meta, "usage": usage, "seconds": seconds, "data": data, "raw_text": raw,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return LLMResult(data=data, provider=self.config.provider, model=model, mode=mode,
                         cache_key=key, cached=False, usage=usage, seconds=seconds)
