"""Provider layer, tested offline with mock HTTP transports (no keys, no network, no spend)."""

import json

import anthropic
import httpx
import httpx2
import pytest

from ci_healer.llm import LLMAnalyzer
from ci_healer.logs import extract
from ci_healer.models import Category, Classification, RunContext
from ci_healer.providers import (AnthropicBackend, ConfigError, LLMError, OpenAICompatBackend,
                                 inline_refs, make_backend, resolve)

GOOD = {"category": "flaky_test", "confidence": 1.3, "summary": "timeout", "evidence": ["E x"],
        "failing_tests": ["t::x"], "implicated_files": [], "trivial_fix_possible": False}
CTX = RunContext("o/r", 1, 1, "a" * 40, "main", "CI", "push", "failure", "")
EXCERPT = extract("##[group]Run pytest\nE TimeoutError\n##[error]Process completed with exit code 1.")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)


# ------------------------------------------------------------------------ config ----
def test_resolution_rules(monkeypatch):
    assert resolve().label == "anthropic/claude-opus-5-5"                 # backwards-compatible default

    with pytest.raises(ConfigError, match="LLM_MODEL is required"):
        resolve("gemini", api_key="k")
    with pytest.raises(ConfigError, match="no API key.*GROQ_API_KEY"):
        resolve("groq", "llama-x")
    with pytest.raises(ConfigError, match="unknown LLM_PROVIDER"):
        resolve("watson", "m", "k")
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        resolve("openai_compatible", "m")

    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    cfg = resolve("Groq", "llama-x")                                         # case-insensitive
    assert cfg.api_key == "from-env" and cfg.base_url == "https://api.groq.com/openai/v1"
    assert resolve("groq", "m", api_key="explicit").api_key == "explicit"   # LLM_API_KEY wins
    assert resolve("ollama", "qwen").api_key == ""                           # local, keyless
    assert "explicit" not in repr(resolve("groq", "m", api_key="explicit"))  # never printed


def test_inline_refs_removes_refs_and_adds_strictness():
    s = inline_refs(Classification.model_json_schema(), strict=True)
    blob = json.dumps(s)
    assert "$ref" not in blob and "$defs" not in blob
    assert s["properties"]["category"]["enum"][0] == "infra"
    assert s["additionalProperties"] is False


# ------------------------------------------------------------ OpenAI-compatible ----
class Server:
    """Scripted chat-completions server. Each entry: (status, content) or (status, raw_json)."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content) if request.content else None})
        status, content, *finish = self.replies.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": content}})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": finish[0] if finish else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def compat(provider, server, model="m-1"):
    return make_backend(resolve(provider, model, api_key="sk-test"), transport=httpx.MockTransport(server))


def test_gemini_happy_path_uses_json_schema_and_bearer_key():
    srv = Server((200, json.dumps(GOOD)))
    result, usage = LLMAnalyzer(compat("gemini", srv, "gemini-x")).classify(CTX, "test", EXCERPT, [], {})
    req = srv.requests[0]
    assert req["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["response_format"]["type"] == "json_schema"
    assert "$ref" not in json.dumps(req["body"]["response_format"])
    assert req["body"]["max_tokens"] == 8192
    assert result.category == Category.FLAKY_TEST and result.confidence == 1.0   # clamped
    assert usage == {"llm": "gemini/gemini-x", "input_tokens": 100, "output_tokens": 20}


def test_openai_gets_strict_schema_and_max_completion_tokens():
    srv = Server((200, json.dumps(GOOD)))
    compat("openai", srv).complete_json("s", "u", Classification, "medium")
    body = srv.requests[0]["body"]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "max_completion_tokens" in body and "max_tokens" not in body


def test_degrades_when_provider_rejects_json_schema_and_remembers():
    srv = Server((400, "response_format json_schema not supported"),
                 (200, json.dumps(GOOD)), (200, json.dumps(GOOD)))
    backend = compat("groq", srv)
    backend.complete_json("s", "u", Classification, "medium")
    backend.complete_json("s", "u", Classification, "medium")
    modes = [r["body"].get("response_format", {}).get("type") for r in srv.requests]
    assert modes == ["json_schema", "json_object", "json_object"]
    assert "JSON Schema" in srv.requests[1]["body"]["messages"][0]["content"]   # schema in prompt


def test_tolerates_think_blocks_and_code_fences():
    srv = Server((200, "<think>hmm, timeouts...</think>\n```json\n" + json.dumps(GOOD) + "\n```"))
    result, _ = compat("groq", srv).complete_json("s", "u", Classification, "medium")
    assert result.summary == "timeout"


def test_invalid_reply_gets_one_repair_round_trip():
    bad = {k: v for k, v in GOOD.items() if k != "evidence"}
    srv = Server((200, json.dumps(bad)), (200, json.dumps(GOOD)))
    result, usage = compat("openrouter", srv).complete_json("s", "u", Classification, "medium")
    second = srv.requests[1]["body"]["messages"]
    assert second[-2]["role"] == "assistant" and "did not match the schema" in second[-1]["content"]
    assert usage["input_tokens"] == 200                                      # both calls counted

    srv = Server((200, "not json at all"), (200, "still not json"))
    with pytest.raises(LLMError, match="invalid JSON twice"):
        compat("openrouter", srv).complete_json("s", "u", Classification, "medium")


def test_auth_and_truncation_errors_are_clear_and_keyless():
    with pytest.raises(LLMError, match="rejected the API key") as e:
        compat("groq", Server((401, "bad key"))).complete_json("s", "u", Classification, "medium")
    assert "sk-test" not in str(e.value)
    with pytest.raises(LLMError, match="LLM_MAX_TOKENS"):
        compat("groq", Server((200, '{"category":', "length"))).complete_json("s", "u", Classification, "m")


def no_sleep(backend, sleeps):
    for b in (getattr(backend, "primary", backend), getattr(backend, "secondary", None)):
        if b is not None:
            b._sleep = sleeps.append
    return backend


def test_overloaded_primary_falls_back_to_second_model():
    """Live-demo regression: Gemini answered 503 'high demand'."""
    srv = Server(*[(503, "This model is currently experiencing high demand")] * 4,
                 (200, json.dumps(GOOD)))
    sleeps = []
    backend = no_sleep(make_backend(resolve("gemini", "m-1", api_key="k", fallback_model="m-2"),
                                    transport=httpx.MockTransport(srv)), sleeps)
    result, usage = backend.complete_json("s", "u", Classification, "medium")
    assert [r["body"]["model"] for r in srv.requests] == ["m-1"] * 4 + ["m-2"]
    assert sleeps == [5, 15, 30]                       # patient backoff before giving up
    assert usage["llm"] == "gemini/m-2" and "fallback m-2" in backend.label


def test_overload_without_fallback_is_a_transient_error_and_honours_retry_after():
    from ci_healer.providers import TransientLLMError

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "slow down"})

    sleeps = []
    backend = no_sleep(compat("groq", handler), sleeps)
    with pytest.raises(TransientLLMError, match="unavailable after retries"):
        backend.complete_json("s", "u", Classification, "medium")
    assert sleeps == [2, 2, 2]


def test_time_budget_bounds_retries_but_each_model_still_gets_one_try():
    """Live-demo regression: a 10-minute Action job was killed mid-retry and reported nothing."""
    from ci_healer.providers import TransientLLMError
    srv = Server((503, "busy"), (503, "busy"))
    sleeps = []
    backend = no_sleep(make_backend(resolve("gemini", "m-1", api_key="k", fallback_model="m-2",
                                            time_budget=0), transport=httpx.MockTransport(srv)), sleeps)
    with pytest.raises(TransientLLMError, match="time budget exhausted"):
        backend.complete_json("s", "u", Classification, "medium")
    assert [r["body"]["model"] for r in srv.requests] == ["m-1", "m-2"]   # one try each, no waiting
    assert sleeps == []

    srv = Server((503, "busy"), (200, json.dumps(GOOD)))
    backend = no_sleep(make_backend(resolve("gemini", "m-1", api_key="k", fallback_model="m-2",
                                            time_budget=0), transport=httpx.MockTransport(srv)), sleeps)
    result, usage = backend.complete_json("s", "u", Classification, "medium")
    assert usage["llm"] == "gemini/m-2" and result.category == Category.FLAKY_TEST


def test_permanent_errors_do_not_fall_back():
    srv = Server((401, "bad key"))
    backend = make_backend(resolve("gemini", "m-1", api_key="k", fallback_model="m-2"),
                           transport=httpx.MockTransport(srv))
    with pytest.raises(LLMError, match="rejected the API key"):
        backend.complete_json("s", "u", Classification, "medium")
    assert len(srv.requests) == 1


def test_list_models_strips_gemini_prefix():
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [
        {"id": "models/gemini-b"}, {"id": "models/gemini-a"}]}))
    assert make_backend(resolve("gemini", api_key="k", need_model=False), transport=t).list_models() \
        == ["gemini-a", "gemini-b"]


# --------------------------------------------------------------------- Anthropic ----
def anthropic_backend(model):
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": json.dumps(GOOD)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})

    client = anthropic.Anthropic(api_key="test", http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(handler)))
    return AnthropicBackend(resolve("anthropic", model), client=client), seen


def test_anthropic_sends_effort_and_fallbacks_for_current_models():
    backend, seen = anthropic_backend("claude-opus-5-5")
    result, usage = backend.complete_json("s", "u", Classification, "high")
    assert seen["body"]["output_config"]["effort"] == "high"
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert seen["body"]["fallbacks"] == "default" and seen["beta"] == AnthropicBackend.FALLBACK_BETA
    assert result.category == Category.FLAKY_TEST and usage["llm"] == "anthropic/claude-opus-5-5"


def test_anthropic_omits_params_older_models_reject():
    backend, seen = anthropic_backend("claude-haiku-4-5")
    backend.complete_json("s", "u", Classification, "high")
    assert "fallbacks" not in seen["body"] and "effort" not in seen["body"].get("output_config", {})
