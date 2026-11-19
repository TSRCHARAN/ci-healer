"""LLM providers: one interface, two wire protocols.

Configure with three variables (plus an optional base URL):

    LLM_PROVIDER = anthropic | gemini | groq | openai | openrouter | mistral | deepseek |
                   together | ollama | openai_compatible
    LLM_MODEL    = the provider's model id   (required except for anthropic)
    LLM_API_KEY  = the key                   (falls back to the provider's usual env var)
    LLM_BASE_URL = only for openai_compatible, or to point at a proxy

Why only two adapters?
Nearly every vendor now serves an *OpenAI-compatible* Chat Completions endpoint (Gemini, Groq,
OpenRouter, Mistral, DeepSeek, Together, Ollama, vLLM, LM Studio...). So:

- `AnthropicBackend` uses the official Anthropic SDK natively. That keeps what the native API
  gives us: schema-enforced structured outputs, the `effort` control, and refusal fallbacks.
- `OpenAICompatBackend` speaks plain HTTP to `{base_url}/chat/completions` with httpx. No
  vendor SDKs to install, and adding a provider is one line in PROVIDERS.

The real difference between providers is **how hard they guarantee the JSON shape**:
some enforce a JSON Schema, some only promise "valid JSON", some promise nothing. So the
compat backend degrades gracefully — json_schema → json_object → plain prompt — and *always*
validates the reply with Pydantic, with one repair round-trip if validation fails. Downstream
code never sees unvalidated model output, whichever provider produced it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger("ci_healer.providers")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


# ============================================================================ registry ====
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    protocol: Literal["anthropic", "openai"]
    base_url: str | None
    key_envs: tuple[str, ...] = ()
    default_model: str | None = None
    key_required: bool = True
    strict_schema: bool = False          # provider enforces strict JSON Schema (OpenAI)
    max_tokens_param: str = "max_tokens"  # OpenAI's newer models want max_completion_tokens


PROVIDERS: dict[str, ProviderSpec] = {s.name: s for s in [
    ProviderSpec("anthropic", "anthropic", None, ("ANTHROPIC_API_KEY",), "claude-opus-5-5",
                 key_required=False),  # the SDK can also resolve credentials itself
    ProviderSpec("gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
                 ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ProviderSpec("groq", "openai", "https://api.groq.com/openai/v1", ("GROQ_API_KEY",)),
    ProviderSpec("openai", "openai", "https://api.openai.com/v1", ("OPENAI_API_KEY",),
                 strict_schema=True, max_tokens_param="max_completion_tokens"),
    ProviderSpec("openrouter", "openai", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",)),
    ProviderSpec("mistral", "openai", "https://api.mistral.ai/v1", ("MISTRAL_API_KEY",)),
    ProviderSpec("deepseek", "openai", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",)),
    ProviderSpec("together", "openai", "https://api.together.xyz/v1", ("TOGETHER_API_KEY",)),
    ProviderSpec("ollama", "openai", "http://localhost:11434/v1", (), key_required=False),
    ProviderSpec("openai_compatible", "openai", None, (), key_required=False),
]}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str = field(default="", repr=False)   # never let a key end up in a log line
    base_url: str | None = None
    max_tokens: int = 8192

    @property
    def spec(self) -> ProviderSpec:
        return PROVIDERS[self.provider]

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def resolve(provider: str = "", model: str = "", api_key: str = "", base_url: str = "",
            max_tokens: int = 8192, *, need_model: bool = True) -> LLMConfig:
    """Validate the user's LLM settings and fill in per-provider defaults."""
    name = (provider or "anthropic").strip().lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ConfigError(f"unknown LLM_PROVIDER {name!r}; choose one of: {', '.join(PROVIDERS)}")

    model = (model or spec.default_model or "").strip()
    if need_model and not model:
        raise ConfigError(
            f"LLM_MODEL is required for provider '{name}'. Model ids change often, so there is "
            f"no baked-in default — run `ci-healer models` to list what your key can use.")

    key = api_key or next((os.environ[e] for e in spec.key_envs if os.environ.get(e)), "")
    if spec.key_required and not key:
        envs = " or ".join(("LLM_API_KEY",) + spec.key_envs)
        raise ConfigError(f"no API key for provider '{name}': set {envs}")

    base = (base_url or spec.base_url or "").rstrip("/") or None
    if spec.protocol == "openai" and not base:
        raise ConfigError(f"provider '{name}' needs LLM_BASE_URL (e.g. http://localhost:8000/v1)")
    return LLMConfig(name, model, key, base, max_tokens)


# ============================================================================ backends ====
class Backend(Protocol):
    label: str

    def complete_json(self, system: str, user: str, schema: type[T], effort: str) -> tuple[T, dict]: ...

    def list_models(self) -> list[str]: ...


def make_backend(cfg: LLMConfig, *, transport: httpx.BaseTransport | None = None) -> Backend:
    if cfg.spec.protocol == "anthropic":
        return AnthropicBackend(cfg)
    return OpenAICompatBackend(cfg, transport=transport)


# --------------------------------------------------------------------- Anthropic ----------
class AnthropicBackend:
    """Native Messages API via the official SDK.

    - `messages.parse(output_format=Model)`: the API constrains output to the schema.
    - `effort` trades depth for cost/latency (sent only to models that accept it).
    - `fallbacks="default"` (beta): if a safety classifier declines, the API retries on a
      fallback model in the same call. Sent only to the model families that support it.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    _FALLBACK_FAMILIES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5-5")
    _NO_EFFORT = ("claude-3", "claude-haiku-4", "claude-sonnet-4-5", "claude-opus-4-1", "claude-opus-4-0")

    def __init__(self, cfg: LLMConfig, client: anthropic.Anthropic | None = None):
        self.cfg, self.label = cfg, cfg.label
        self.client = client or anthropic.Anthropic(api_key=cfg.api_key or None,
                                                    base_url=cfg.base_url or None)

    def complete_json(self, system: str, user: str, schema: type[T], effort: str) -> tuple[T, dict]:
        model = self.cfg.model
        extra: dict = {}
        if model.startswith(self._FALLBACK_FAMILIES):
            extra.update(betas=[self.FALLBACK_BETA], fallbacks="default")
        if not model.startswith(self._NO_EFFORT):
            extra["output_config"] = {"effort": effort}
        try:
            resp = self.client.beta.messages.parse(
                model=model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}], output_format=schema, **extra)
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed — check LLM_API_KEY") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"network error talking to Anthropic: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited by Anthropic (SDK retries exhausted)") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e

        usage = {"llm": f"anthropic/{resp.model}", "input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens, "request_id": resp._request_id}
        if resp.stop_reason == "refusal":
            raise LLMError(f"model declined (category={getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise LLMError(f"no parseable output (stop_reason={resp.stop_reason})")
        return resp.parsed_output, usage

    def list_models(self) -> list[str]:
        return [m.id for m in self.client.models.list()]


# ------------------------------------------------------------- OpenAI-compatible ----------
THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
MODES = ("json_schema", "json_object", "prompt_only")


def inline_refs(schema: dict, *, strict: bool = False) -> dict:
    """Pydantic emits `$defs` + `$ref`; several providers' schema dialects reject refs.
    Inline them (our schemas aren't recursive). For strict providers, also forbid extra keys."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v) for k, v in node.items() if k != "$defs"}
            if strict and out.get("type") == "object":
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def parse_json_reply(text: str, schema: type[T]) -> T:
    """Tolerate the common wrappers (<think> blocks, ``` fences, a sentence before the JSON),
    then validate strictly against the schema."""
    s = THINK.sub("", text or "").strip()
    s = FENCE.sub("", s).strip()
    if not s.startswith("{"):
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("reply contains no JSON object")
        s = s[start:end + 1]
    return schema.model_validate(json.loads(s))


class OpenAICompatBackend:
    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.cfg, self.label = cfg, cfg.label
        headers = {"Content-Type": "application/json", "User-Agent": "ci-healer"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._http = httpx.Client(base_url=cfg.base_url + "/", headers=headers, timeout=180,
                                  transport=transport)
        self._mode = 0   # index into MODES; remembered per process once a provider says no

    # --- HTTP with small, honest retry policy ---------------------------------------------
    def _post(self, body: dict) -> httpx.Response:
        for attempt in range(3):
            try:
                r = self._http.post("chat/completions", json=body)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise LLMError(f"network error talking to {self.cfg.provider}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(min(float(r.headers.get("retry-after", 2 ** (attempt + 1))), 30))
                continue
            return r
        return r  # pragma: no cover

    def _request(self, messages: list[dict], schema: type[BaseModel]) -> dict:
        while True:
            mode = MODES[self._mode]
            body: dict = {"model": self.cfg.model, "messages": messages,
                          self.cfg.spec.max_tokens_param: self.cfg.max_tokens}
            if mode == "json_schema":
                js = {"name": schema.__name__,
                      "schema": inline_refs(schema.model_json_schema(), strict=self.cfg.spec.strict_schema)}
                if self.cfg.spec.strict_schema:
                    js["strict"] = True
                body["response_format"] = {"type": "json_schema", "json_schema": js}
            elif mode == "json_object":
                body["response_format"] = {"type": "json_object"}

            r = self._post(body)
            if r.status_code in (401, 403):
                raise LLMError(f"{self.cfg.provider} rejected the API key (HTTP {r.status_code}) — check LLM_API_KEY")
            if r.status_code in (400, 422) and mode != "prompt_only":
                log.warning("%s refused response_format=%s (%s); degrading", self.label, mode, r.text[:200])
                self._mode += 1
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.cfg.provider} error {r.status_code}: {r.text[:300]}")
            return r.json()

    def complete_json(self, system: str, user: str, schema: type[T], effort: str) -> tuple[T, dict]:
        # The schema is ALSO spelled out in the prompt: providers that only do json_object (or
        # nothing) need it, and it doesn't hurt the ones that enforce it.
        sys_prompt = (f"{system}\n\nRespond with ONLY a JSON object (no prose, no code fences) "
                      f"that conforms to this JSON Schema:\n"
                      f"{json.dumps(inline_refs(schema.model_json_schema()))}")
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        usage = {"llm": self.label, "input_tokens": 0, "output_tokens": 0}

        for attempt in range(2):            # first try + one repair round-trip
            data = self._request(messages, schema)
            if data.get("model"):   # aliases like "gemini-flash-latest" → record what really ran
                usage["llm"] = f"{self.cfg.provider}/{str(data['model']).removeprefix('models/')}"
            u = data.get("usage") or {}
            usage["input_tokens"] += u.get("prompt_tokens", 0)
            usage["output_tokens"] += u.get("completion_tokens", 0)
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise LLMError("reply truncated (finish_reason=length) — raise LLM_MAX_TOKENS")
            text = (choice.get("message") or {}).get("content") or ""
            try:
                return parse_json_reply(text, schema), usage
            except (ValueError, ValidationError) as e:
                if attempt == 1:
                    raise LLMError(f"{self.label} returned invalid JSON twice: {str(e)[:200]}") from e
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"That reply did not match the schema: "
                                                         f"{str(e)[:500]}\nReply with ONLY the corrected JSON object."}]
        raise LLMError("unreachable")  # pragma: no cover

    def list_models(self) -> list[str]:
        r = self._http.get("models")
        if r.status_code >= 400:
            raise LLMError(f"{self.cfg.provider} /models -> {r.status_code}: {r.text[:200]}")
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        return sorted(i.removeprefix("models/") for i in ids if i)
