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
    assert usage == {"input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 0}


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
    assert usage == {"input_tokens": 22, "output_tokens": 14, "cached_input_tokens": 0}


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


# --- Azure OpenAI -----------------------------------------------------------------

def _clear_llm_env(monkeypatch):
    for k in ("CREDIT_REVIEW_LLM_PROVIDER", "CREDIT_REVIEW_LLM_MODEL", "CREDIT_REVIEW_LLM_BASE_URL",
              "CREDIT_REVIEW_LLM_API_KEY", "CREDIT_REVIEW_LLM_TEMPERATURE", "OPENAI_API_KEY", "CREDIT_REVIEW_LLM_OFFLINE",
              "CREDIT_REVIEW_LLM_REASONING_EFFORT"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("endpoint", ["https://res.openai.azure.com", "https://res.openai.azure.com/",
                                      "https://res.openai.azure.com/openai/v1/"])
def test_azure_config_uses_the_v1_endpoint_and_entra_id(tmp_path, monkeypatch, endpoint):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("CREDIT_REVIEW_LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("CREDIT_REVIEW_LLM_BASE_URL", endpoint)
    monkeypatch.setenv("CREDIT_REVIEW_LLM_MODEL", "my-deployment")
    config = LLMConfig.from_env(tmp_path / "missing.env")
    assert config.provider == "openai" and config.label == "azure_openai"
    assert config.base_url == "https://res.openai.azure.com/openai/v1/"
    assert (config.auth, config.api_key, config.token_param) == ("entra", "", "max_completion_tokens")
    assert config.temperature is None and config.model == "my-deployment"


def test_azure_config_needs_a_deployment_name(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("CREDIT_REVIEW_LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("CREDIT_REVIEW_LLM_BASE_URL", "https://res.openai.azure.com")
    with pytest.raises(LLMError, match="deployment name"):
        LLMConfig.from_env(tmp_path / "missing.env")


def test_azure_requests_send_max_completion_tokens_and_no_temperature():
    """Newer OpenAI models reject max_tokens and any temperature but the default with a 400, which the
    structured-output fallback would otherwise misread as 'schema refused'."""
    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", auth="entra", token_param="max_completion_tokens", temperature=None)
    data, _, mode, _ = OpenAICompatProvider(config, client=fake).complete(request(), "my-deployment", 1000)
    call = fake.calls[0]
    assert data == {"x": "ok"} and mode == "json_schema"
    assert call["max_completion_tokens"] == 1000
    assert "max_tokens" not in call and "temperature" not in call


def test_azure_signs_in_with_a_token_provider_not_a_key(monkeypatch):
    import azure.identity
    import openai

    seen = {}
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: "credential")
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider",
                        lambda cred, scope: seen.update(cred=cred, scope=scope) or (lambda: "token"))
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: seen.update(client=kw) or NS())
    OpenAICompatProvider(LLMConfig(provider="openai", auth="entra", base_url="https://res.openai.azure.com/openai/v1/"))
    assert seen["cred"] == "credential" and seen["scope"] == "https://cognitiveservices.azure.com/.default"
    api_key = seen["client"]["api_key"]
    assert callable(api_key) and api_key() == "token"


def test_cost_comes_from_usage_and_is_kept_for_cached_results(tmp_path, monkeypatch):
    prices = tmp_path / "prices.yaml"
    prices.write_text("models:\n  m: {input: 2.0, output: 10.0}\n")
    monkeypatch.setenv("CREDIT_REVIEW_PRICES_FILE", str(prices))
    fake = FakeOpenAI()                                    # usage: 20 input, 5 output tokens
    config = LLMConfig(provider="openai", model="m", cache_dir=tmp_path / "cache")
    client = LLMClient(config, provider=OpenAICompatProvider(config, client=fake))
    first, again = client.complete(request()), client.complete(request())
    assert first.cost == again.cost == pytest.approx((20 * 2.0 + 5 * 10.0) / 1_000_000)
    assert not first.cached and again.cached               # cost of the call when made; nothing paid the second time

    config_unpriced = LLMConfig(provider="openai", model="unpriced", cache_dir=tmp_path / "cache2")
    unpriced = LLMClient(config_unpriced, provider=OpenAICompatProvider(config_unpriced, client=FakeOpenAI()))
    assert unpriced.complete(request()).cost is None   # unknown price is "n/a", never 0


def test_prompt_cached_input_is_recorded_and_priced_lower(tmp_path, monkeypatch):
    prices = tmp_path / "prices.yaml"
    prices.write_text("models:\n  m: {input: 2.0, output: 10.0, cached_input: 0.2}\n  n: {input: 2.0, output: 10.0}\n")
    monkeypatch.setenv("CREDIT_REVIEW_PRICES_FILE", str(prices))

    class Cached(FakeOpenAI):                              # 2,000 input tokens, 1,500 of them from the prompt cache
        def create(self, **kw):
            reply = super().create(**kw)
            reply.usage = NS(prompt_tokens=2000, completion_tokens=5, prompt_tokens_details=NS(cached_tokens=1500))
            return reply

    for model, expected in (("m", 500 * 2.0 + 1500 * 0.2 + 5 * 10.0), ("n", 2000 * 2.0 + 5 * 10.0)):
        config = LLMConfig(provider="openai", model=model, cache_dir=tmp_path / model)
        result = LLMClient(config, provider=OpenAICompatProvider(config, client=Cached())).complete(request())
        assert result.usage["cached_input_tokens"] == 1500 and result.usage["input_tokens"] == 2000
        assert result.cost == pytest.approx(expected / 1_000_000)     # without a cached price: priced as input


def test_a_reply_without_cache_details_counts_no_cached_tokens(tmp_path):
    config = LLMConfig(provider="openai", model="m", cache_dir=tmp_path)
    usage = LLMClient(config, provider=OpenAICompatProvider(config, client=FakeOpenAI())).complete(request()).usage
    assert usage["cached_input_tokens"] == 0


def test_reasoning_effort_is_sent_and_keys_the_cache_only_when_set(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("CREDIT_REVIEW_LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("CREDIT_REVIEW_LLM_BASE_URL", "https://res.openai.azure.com")
    monkeypatch.setenv("CREDIT_REVIEW_LLM_MODEL", "my-deployment")
    monkeypatch.setenv("CREDIT_REVIEW_LLM_REASONING_EFFORT", "low")
    assert LLMConfig.from_env(tmp_path / "missing.env").reasoning_effort == "low"

    fake = FakeOpenAI()
    config = LLMConfig(provider="openai", model="m", reasoning_effort="low", cache_dir=tmp_path)
    OpenAICompatProvider(config, client=fake).complete(request(), "m", 1000)
    assert fake.calls[0]["reasoning_effort"] == "low"
    OpenAICompatProvider(LLMConfig(provider="openai"), client=fake).complete(request(), "m", 1000)
    assert "reasoning_effort" not in fake.calls[1]

    def key(effort):
        return LLMClient(LLMConfig(provider="openai", model="m", reasoning_effort=effort)).cache_key(request(), "m")
    assert key("low") != key("medium") != key(None)
    assert key(None) == key("")                             # unset: the key every earlier cache entry has
