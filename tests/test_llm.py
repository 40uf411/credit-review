"""Both providers, tested against fake SDK clients: request shape, reply parsing,
fallbacks, cache and offline mode. No network, no API key."""

import json
from types import SimpleNamespace as NS

import pytest

from credit_review.llm import (
    AnthropicProvider,
    CacheMiss,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMRequest,
    OpenAICompatProvider,
    parse_json_text,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 8

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"],
          "additionalProperties": False}


def request(images=()):
    return LLMRequest(purpose="extract", system="sys", prompt="read this", schema=SCHEMA,
                      schema_name="extraction", images=list(images), meta={"doc": "a.pdf"})


class BadRequest(Exception):
    status_code = 400


class FakeAnthropic:
    """Behaves like the real API: a large strict schema can be refused, forced tool choice is
    refused (as on the 5.5 models), prompted replies come from a queue."""

    def __init__(self, reject_structured=False, prompted=('{"x": "prompted"}',)):
        self.calls, self.reject, self.prompted = [], reject_structured, list(prompted)
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=11, output_tokens=7)
        if kw.get("tool_choice", {}).get("type") in ("tool", "any"):
            raise BadRequest('tool_choice: type "tool" and "any" are not supported for this model.')
        if "output_config" in kw:
            if self.reject:
                raise BadRequest("Schema is too complex for compilation")
            return NS(content=[NS(type="text", text='{"x": "structured"}')], stop_reason="end_turn", usage=usage)
        return NS(content=[NS(type="text", text=self.prompted.pop(0))], stop_reason="end_turn", usage=usage)


class FakeOpenAI:
    def __init__(self, reply='{"x": "ok"}', reject_schema=False):
        self.calls, self.reply, self.reject = [], reply, reject_schema
        self.chat = NS(completions=NS(create=self.create))
        self.models = NS(list=lambda: NS(data=[NS(id="qwen2.5-vl-7b-instruct")]))

    def create(self, **kw):
        self.calls.append(kw)
        if self.reject and "response_format" in kw:
            raise BadRequest("response_format not supported")
        return NS(choices=[NS(message=NS(content=self.reply), finish_reason="stop")],
                  usage=NS(prompt_tokens=20, completion_tokens=5))


# --- Anthropic -------------------------------------------------------------------

def test_anthropic_uses_structured_output_and_sends_images_first():
    fake = FakeAnthropic()
    data, usage, mode, _ = AnthropicProvider(LLMConfig(), client=fake).complete(
        request(images=[PNG]), "claude-sonnet-5-5", 1000)
    call = fake.calls[0]
    assert data == {"x": "structured"} and mode == "json_schema"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[-1] == {"type": "text", "text": "read this"}
    assert "temperature" not in call                      # not accepted by current Claude models
    assert usage == {"input_tokens": 11, "output_tokens": 7}


def test_anthropic_falls_back_to_prompted_json_when_schema_is_refused():
    fake = FakeAnthropic(reject_structured=True, prompted=['{"x": "a"}', '{"x": "b"}'])
    provider = AnthropicProvider(LLMConfig(), client=fake)
    data, _, mode, _ = provider.complete(request(), "claude-sonnet-5-5", 1000)
    assert data == {"x": "a"} and mode == "prompt"
    second = fake.calls[1]
    assert "output_config" not in second and "tool_choice" not in second and "tools" not in second
    assert "JSON Schema" in second["messages"][0]["content"][-1]["text"]
    provider.complete(request(), "claude-sonnet-5-5", 1000)       # remembered for this schema
    assert len(fake.calls) == 3 and "output_config" not in fake.calls[2]


def test_fallback_is_per_schema_so_small_schemas_stay_strict():
    fake = FakeAnthropic(reject_structured=True, prompted=['{"x": "a"}'])
    provider = AnthropicProvider(LLMConfig(), client=fake)
    provider.complete(request(), "m", 100)
    fake.reject = False
    small = request()
    small.schema = {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"],
                    "additionalProperties": False}
    provider.complete(small, "m", 100)
    assert "output_config" in fake.calls[-1]


def test_a_reply_missing_required_keys_gets_one_repair_request():
    fake = FakeAnthropic(reject_structured=True, prompted=['{"y": 1}', '{"x": "fixed"}'])
    data, usage, mode, _ = AnthropicProvider(LLMConfig(), client=fake).complete(request(), "m", 100)
    assert data == {"x": "fixed"} and mode == "prompt+repair"
    repair = fake.calls[2]["messages"]
    assert repair[1] == {"role": "assistant", "content": '{"y": 1}'}
    assert "$.x: missing" in repair[2]["content"]
    assert usage == {"input_tokens": 22, "output_tokens": 14}


def test_a_reply_that_is_never_json_is_an_error():
    fake = FakeAnthropic(reject_structured=True, prompted=["Sorry.", "Still sorry."])
    with pytest.raises(LLMError, match="not valid JSON"):
        AnthropicProvider(LLMConfig(), client=fake).complete(request(), "m", 100)


def test_anthropic_truncated_reply_is_an_error():
    fake = FakeAnthropic()
    fake.messages = NS(create=lambda **kw: NS(content=[NS(type="text", text='{"x":')],
                                              stop_reason="max_tokens", usage=None))
    with pytest.raises(LLMError, match="max_tokens"):
        AnthropicProvider(LLMConfig(), client=fake).complete(request(), "m", 10)


# --- OpenAI-compatible / LM Studio ------------------------------------------------

def test_lmstudio_sends_json_schema_and_data_url_images():
    fake = FakeOpenAI()
    data, _, mode, _ = OpenAICompatProvider(LLMConfig(provider="openai"), client=fake).complete(
        request(images=[JPEG]), "qwen2.5-vl-7b-instruct", 1000)
    call = fake.calls[0]
    assert data == {"x": "ok"} and mode == "json_schema"
    assert call["response_format"]["json_schema"]["schema"] == SCHEMA
    assert call["temperature"] == 0.0
    assert call["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_lmstudio_falls_back_to_prompted_json():
    fake = FakeOpenAI(reject_schema=True)
    data, _, mode, _ = OpenAICompatProvider(LLMConfig(provider="openai"), client=fake).complete(
        request(), "some-model", 1000)
    assert data == {"x": "ok"} and mode == "prompt"
    assert "JSON Schema" in fake.calls[1]["messages"][1]["content"][0]["text"]


def test_reply_cleanup_handles_reasoning_and_fences():
    reply = '<think>let me look</think>\n```json\n{"x": "clean"}\n```'
    assert parse_json_text(reply) == {"x": "clean"}
    with pytest.raises(LLMError):
        parse_json_text("I cannot read this document.")


def test_lmstudio_uses_first_loaded_model_when_none_configured(tmp_path):
    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", cache_dir=tmp_path)
    client = LLMClient(config, provider=OpenAICompatProvider(config, client=fake))
    result = client.complete(request())
    assert result.model == "qwen2.5-vl-7b-instruct"


# --- cache & offline --------------------------------------------------------------

def test_second_call_is_served_from_cache(tmp_path):
    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", model="m", cache_dir=tmp_path)
    client = LLMClient(config, provider=OpenAICompatProvider(config, client=fake))
    first, second = client.complete(request()), client.complete(request())
    assert not first.cached and second.cached and second.data == first.data
    assert len(fake.calls) == 1
    entry = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert entry["model"] == "m" and entry["meta"] == {"doc": "a.pdf"}


def test_any_change_to_the_request_misses_the_cache(tmp_path):
    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", model="m", cache_dir=tmp_path)
    client = LLMClient(config, provider=OpenAICompatProvider(config, client=fake))
    client.complete(request())
    changed = request()
    changed.prompt = "read this carefully"
    client.complete(changed)
    assert len(fake.calls) == 2


def test_offline_mode_never_calls_a_model(tmp_path):
    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", model="m", cache_dir=tmp_path, offline=True)
    client = LLMClient(config, provider=OpenAICompatProvider(config, client=fake))
    with pytest.raises(CacheMiss):
        client.complete(request())
    assert fake.calls == []


def test_config_from_env(tmp_path, monkeypatch):
    for k in ("CREDIT_REVIEW_LLM_PROVIDER", "CREDIT_REVIEW_LLM_MODEL", "CREDIT_REVIEW_LLM_BASE_URL", "CREDIT_REVIEW_LLM_API_KEY",
              "OPENAI_API_KEY", "CREDIT_REVIEW_LLM_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("CREDIT_REVIEW_LLM_PROVIDER=lmstudio\nCREDIT_REVIEW_LLM_MODEL=qwen2.5-vl-7b-instruct\n")
    config = LLMConfig.from_env(env)
    assert config.provider == "openai" and config.label == "lmstudio"
    assert config.base_url == "http://localhost:1234/v1" and config.api_key == "lm-studio"
    monkeypatch.setenv("CREDIT_REVIEW_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("CREDIT_REVIEW_LLM_MODEL", raising=False)
    assert LLMConfig.from_env(tmp_path / "missing.env").model == "claude-sonnet-5-5"


def test_anthropic_falls_back_on_an_sdk_without_output_config():
    fake = FakeAnthropic()
    original = fake.create
    def old_sdk(**kw):
        if "output_config" in kw:
            raise TypeError("create() got an unexpected keyword argument 'output_config'")
        return original(**kw)
    fake.messages = NS(create=old_sdk)
    data, _, mode, _ = AnthropicProvider(LLMConfig(), client=fake).complete(request(), "m", 100)
    assert data == {"x": "prompted"} and mode == "prompt"


def test_sdk_errors_become_per_document_errors(tmp_path):
    class Boom:
        name = "anthropic"
        def complete(self, req, model, max_tokens):
            raise BadRequest("Error code: 400 - invalid_request_error")
    client = LLMClient(LLMConfig(model="m", cache_dir=tmp_path), provider=Boom())
    with pytest.raises(LLMError, match="anthropic call failed"):
        client.complete(request())
