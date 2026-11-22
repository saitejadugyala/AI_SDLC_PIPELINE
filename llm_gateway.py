"""Centralized LLM gateway.

Every agent calls generate_json / generate_text / embed here instead of
building its own provider client. That gives one place for:

  - provider routing: config.LLM_PROVIDER_ORDER (default gemini -> openai -> openrouter);
    providers without an API key are skipped
  - retries with backoff for transient failures (timeouts, 429, 5xx) and for
    malformed output (invalid JSON, or JSON that fails the schema)
  - fallback to the next provider when one is exhausted or rejects the request
  - schema validation with jsonschema, whatever the provider claims
  - usage tracking: one llm_calls row per attempt (agent, prompt version,
    provider, model, tokens, latency, cost when the provider reports it,
    thread_id for traceability)
  - LangSmith tracing (when LANGSMITH_TRACING=true): every attempt is an
    "llm" run with prompts, output, token usage and model metadata, nested
    under the LangGraph step that made it

Clients are created lazily on first use, so importing an agent module no
longer requires an API key. This is what makes the app importable in tests.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import time
from dataclasses import dataclass

import jsonschema
import requests
from langsmith import traceable

import config

logger = logging.getLogger("llm_gateway")

# Set by graph nodes so every LLM call can be traced back to its workflow.
current_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_thread_id", default=None)

RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Raised when every configured provider failed. Subclasses RuntimeError
    so existing `except RuntimeError` handlers keep working."""


class _InvalidOutput(Exception):
    """Provider answered, but the content was not valid JSON for the schema."""


class _NonRetryable(Exception):
    """Provider rejected the request (bad key, bad request). Retrying the same
    provider is pointless; fall through to the next one."""


@dataclass
class _Raw:
    text: str | None
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


# ---------------------------------------------------------------- storage

def init_llm_db() -> None:
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            thread_id TEXT,
            agent TEXT NOT NULL,
            prompt_version TEXT,
            provider TEXT NOT NULL,
            model TEXT,
            attempt INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            latency_ms INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_thread ON llm_calls(thread_id)")
    conn.commit()
    conn.close()


def _record(**row) -> None:
    try:
        conn = sqlite3.connect(config.APP_DB_PATH)
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:  # usage tracking must never break a real call
        logger.warning(f"could not record llm call: {e}")


def usage_summary(thread_id: str) -> dict:
    conn = sqlite3.connect(config.APP_DB_PATH)
    rows = conn.execute(
        """SELECT agent, COUNT(*), SUM(COALESCE(input_tokens,0)), SUM(COALESCE(output_tokens,0)),
                  SUM(COALESCE(cost_usd,0)), SUM(latency_ms)
           FROM llm_calls WHERE thread_id = ? GROUP BY agent""",
        (thread_id,),
    ).fetchall()
    conn.close()
    return {
        r[0]: {"calls": r[1], "input_tokens": r[2], "output_tokens": r[3], "cost_usd": round(r[4], 6), "latency_ms": r[5]}
        for r in rows
    }


# ---------------------------------------------------------------- providers

_gemini_client = None


def _get_gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def _gemini_error(e: Exception) -> Exception:
    code = getattr(e, "code", None)
    if isinstance(code, int) and 400 <= code < 500 and code not in RETRYABLE_HTTP:
        return _NonRetryable(f"gemini {code}: {e}")
    return e


def _call_gemini(system: str, contents: str, schema: dict | None, timeout_s: int) -> _Raw:
    from google.genai import types
    kwargs = dict(system_instruction=system, http_options=types.HttpOptions(timeout=timeout_s * 1000))
    if schema is not None:
        kwargs.update(response_mime_type="application/json", response_schema=schema)
    try:
        resp = _get_gemini().models.generate_content(
            model=config.GEMINI_MODEL, contents=contents, config=types.GenerateContentConfig(**kwargs)
        )
    except Exception as e:
        raise _gemini_error(e) from e
    um = getattr(resp, "usage_metadata", None)
    return _Raw(
        text=resp.text,
        model=config.GEMINI_MODEL,
        input_tokens=getattr(um, "prompt_token_count", None),
        output_tokens=getattr(um, "candidates_token_count", None),
    )


def _call_chat_completions(*, name: str, base_url: str, api_key: str, model: str, extra_body: dict,
                           extra_headers: dict, system: str, contents: str, schema: dict | None,
                           timeout_s: int) -> _Raw:
    """Shared client for OpenAI-compatible /chat/completions APIs (OpenAI, OpenRouter)."""
    sys_msg = system
    body = {"model": model, **extra_body}
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            # strict=False: strict mode requires every property to be required and
            # additionalProperties:false, which our schemas don't follow. The
            # gateway validates against the schema itself either way.
            "json_schema": {"name": "response", "strict": False, "schema": schema},
        }
        # Not every model honours response_format; say it in words too.
        sys_msg += "\n\nRespond with ONLY a JSON object matching this JSON Schema:\n" + json.dumps(schema)
    body["messages"] = [{"role": "system", "content": sys_msg}, {"role": "user", "content": contents}]
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", **extra_headers},
        json=body,
        timeout=timeout_s,
    )
    if resp.status_code >= 400:
        msg = f"{name} {resp.status_code}: {resp.text[:300]}"
        if resp.status_code in RETRYABLE_HTTP:
            raise RuntimeError(msg)
        raise _NonRetryable(msg)
    data = resp.json()
    usage = data.get("usage") or {}
    choice = (data.get("choices") or [{}])[0]
    return _Raw(
        text=(choice.get("message") or {}).get("content"),
        model=data.get("model", model),
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cost_usd=usage.get("cost"),  # OpenRouter reports this; OpenAI does not (stays NULL)
    )


def _call_openai(system: str, contents: str, schema: dict | None, timeout_s: int) -> _Raw:
    return _call_chat_completions(
        name="openai", base_url=config.OPENAI_BASE_URL, api_key=config.OPENAI_API_KEY,
        model=config.OPENAI_MODEL, extra_body={}, extra_headers={},
        system=system, contents=contents, schema=schema, timeout_s=timeout_s,
    )


def _call_openrouter(system: str, contents: str, schema: dict | None, timeout_s: int) -> _Raw:
    return _call_chat_completions(
        name="openrouter", base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY,
        model=config.OPENROUTER_MODEL,
        extra_body={"usage": {"include": True}},  # ask OpenRouter to report real cost
        extra_headers={"X-Title": "AI-SDLC-Pipeline"},
        system=system, contents=contents, schema=schema, timeout_s=timeout_s,
    )


PROVIDERS = {"gemini": _call_gemini, "openai": _call_openai, "openrouter": _call_openrouter}


def available_providers() -> list[str]:
    keys = {"gemini": config.GEMINI_API_KEY, "openai": config.OPENAI_API_KEY,
            "openrouter": config.OPENROUTER_API_KEY}
    return [p for p in config.LLM_PROVIDER_ORDER if p in PROVIDERS and keys.get(p)]


# ---------------------------------------------------------------- LangSmith tracing
# A no-op unless LANGSMITH_TRACING=true (the SDK checks the env per call).

def _trace_llm_inputs(inputs: dict) -> dict:
    out = {"messages": [{"role": "system", "content": inputs.get("system")},
                        {"role": "user", "content": inputs.get("contents")}]}
    if inputs.get("schema") is not None:
        out["response_schema"] = inputs["schema"]
    return out


def _usage(input_tokens, output_tokens, cost=None) -> dict | None:
    if input_tokens is None and output_tokens is None:
        return None
    usage = {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0,
             "total_tokens": (input_tokens or 0) + (output_tokens or 0)}
    if cost is not None:
        usage["total_cost"] = cost  # OpenRouter reports real cost; otherwise LangSmith prices by model
    return usage


def _trace_llm_outputs(raw) -> dict:
    if not isinstance(raw, _Raw):
        return {"output": raw}
    out = {"output": raw.text, "model": raw.model}
    usage = _usage(raw.input_tokens, raw.output_tokens, raw.cost_usd)
    if usage:
        out["usage_metadata"] = usage
    return out


@traceable(run_type="llm", name="llm_call", process_inputs=_trace_llm_inputs, process_outputs=_trace_llm_outputs)
def _traced_call(call, system: str, contents: str, schema: dict | None, timeout_s: int) -> "_Raw":
    return call(system, contents, schema, timeout_s)


def _trace_extra(agent: str, prompt_version: str | None, provider: str, model: str | None, attempt: int) -> dict:
    return {
        "name": f"{agent} · {provider}",
        "tags": [f"agent:{agent}", f"provider:{provider}"],
        "metadata": {
            "ls_provider": provider, "ls_model_name": model,  # lets LangSmith price the call
            "agent": agent, "prompt_version": prompt_version, "attempt": attempt,
            "thread_id": current_thread_id.get(),
        },
    }


def _provider_model(provider: str) -> str | None:
    return {"gemini": config.GEMINI_MODEL, "openai": config.OPENAI_MODEL,
            "openrouter": config.OPENROUTER_MODEL}.get(provider)


# ---------------------------------------------------------------- core

def _sleep(seconds: float) -> None:  # patched to a no-op in tests
    time.sleep(seconds)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _parse(raw: _Raw, schema: dict | None):
    if not raw.text:
        raise _InvalidOutput("empty response")
    if schema is None:
        return raw.text
    try:
        data = json.loads(_strip_fences(raw.text))
    except json.JSONDecodeError as e:
        raise _InvalidOutput(f"invalid JSON: {e}") from e
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise _InvalidOutput(f"schema violation at {list(e.absolute_path)}: {e.message}") from e
    return data


def _run(agent: str, prompt_version: str, system: str, contents: str, schema: dict | None, timeout_s: int | None):
    providers = available_providers()
    if not providers:
        raise LLMError(
            "No LLM provider configured. Set at least one of GEMINI_API_KEY, OPENAI_API_KEY, "
            "OPENROUTER_API_KEY in .env "
            f"(provider order: {config.LLM_PROVIDER_ORDER})."
        )
    timeout_s = timeout_s or config.LLM_TIMEOUT_SECONDS
    errors: list[str] = []

    for provider in providers:
        call = PROVIDERS[provider]
        prompt = contents
        for attempt in range(1, config.LLM_MAX_ATTEMPTS_PER_PROVIDER + 1):
            start = time.monotonic()
            raw = None
            try:
                raw = _traced_call(call, system, prompt, schema, timeout_s,
                                   langsmith_extra=_trace_extra(agent, prompt_version, provider,
                                                                _provider_model(provider), attempt))
                result = _parse(raw, schema)
                _record(thread_id=current_thread_id.get(), agent=agent, prompt_version=prompt_version,
                        provider=provider, model=raw.model, attempt=attempt, status="ok", error=None,
                        input_tokens=raw.input_tokens, output_tokens=raw.output_tokens, cost_usd=raw.cost_usd,
                        latency_ms=int((time.monotonic() - start) * 1000))
                return result
            except _NonRetryable as e:
                status, err, retry_same = "rejected", str(e), False
            except _InvalidOutput as e:
                status, err, retry_same = "invalid_output", str(e), True
                prompt = (f"{contents}\n\nYour previous response was rejected ({e}). "
                          f"Return ONLY output that satisfies the required format.")
            except Exception as e:  # network errors, timeouts, 429/5xx
                status, err, retry_same = "error", f"{type(e).__name__}: {e}", True

            _record(thread_id=current_thread_id.get(), agent=agent, prompt_version=prompt_version,
                    provider=provider, model=getattr(raw, "model", None), attempt=attempt, status=status,
                    error=err[:1000], input_tokens=getattr(raw, "input_tokens", None),
                    output_tokens=getattr(raw, "output_tokens", None), cost_usd=getattr(raw, "cost_usd", None),
                    latency_ms=int((time.monotonic() - start) * 1000))
            logger.warning(f"[{agent}] {provider} attempt {attempt} {status}: {err[:200]}")
            errors.append(f"{provider}#{attempt} {status}: {err[:200]}")
            if not retry_same:
                break
            if attempt < config.LLM_MAX_ATTEMPTS_PER_PROVIDER:
                _sleep(min(2 ** (attempt - 1), 8))

    raise LLMError(f"{agent}: all LLM providers failed — " + " | ".join(errors))


def generate_json(*, agent: str, prompt_version: str, system: str, contents: str, schema: dict,
                  timeout_s: int | None = None) -> dict:
    """Structured output, validated against `schema`."""
    return _run(agent, prompt_version, system, contents, schema, timeout_s)


def generate_text(*, agent: str, prompt_version: str, system: str, contents: str,
                  timeout_s: int | None = None) -> str:
    return _run(agent, prompt_version, system, contents, None, timeout_s)


def embedding_model() -> str:
    """The embedding model RAG will use, as "provider:model". Stored with every
    chunk: vectors from different models are not comparable, so retrieval only
    ever compares vectors produced by the same model."""
    provider = config.EMBEDDING_PROVIDER
    if provider == "auto":
        provider = "gemini" if config.GEMINI_API_KEY else "openai" if config.OPENAI_API_KEY else None
    if provider == "gemini" and config.GEMINI_API_KEY:
        return f"gemini:{config.GEMINI_EMBED_MODEL}"
    if provider == "openai" and config.OPENAI_API_KEY:
        return f"openai:{config.OPENAI_EMBED_MODEL}"
    raise LLMError("RAG embeddings need GEMINI_API_KEY or OPENAI_API_KEY "
                   f"(EMBEDDING_PROVIDER={config.EMBEDDING_PROVIDER}).")


def _embed_gemini(model: str, texts: list[str]) -> tuple[list[list[float]], int | None]:
    try:
        result = _get_gemini().models.embed_content(model=model, contents=texts)
    except Exception as e:
        raise _gemini_error(e) from e
    return [e.values for e in result.embeddings], None


def _embed_openai(model: str, texts: list[str]) -> tuple[list[list[float]], int | None]:
    resp = requests.post(
        f"{config.OPENAI_BASE_URL.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={"model": model, "input": texts},
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        msg = f"openai embeddings {resp.status_code}: {resp.text[:300]}"
        if resp.status_code in RETRYABLE_HTTP:
            raise RuntimeError(msg)
        raise _NonRetryable(msg)
    data = resp.json()
    rows = sorted(data["data"], key=lambda d: d["index"])
    return [r["embedding"] for r in rows], (data.get("usage") or {}).get("prompt_tokens")


@traceable(run_type="embedding", name="embedding",
           process_inputs=lambda i: {"model": i.get("model"), "count": len(i.get("texts") or []),
                                     "chars": sum(len(t) for t in i.get("texts") or [])},
           process_outputs=lambda o: {"vectors": len(o[0]), "usage_metadata": _usage(o[1], 0)} if o[1] else {"vectors": len(o[0])})
def _traced_embed(call, model: str, texts: list[str]):
    return call(model, texts)


def embed(texts: list[str], *, agent: str = "rag_embed") -> list[list[float]]:
    """Embeddings with the model from embedding_model(). Retries transient
    errors, but never falls back to a different model silently."""
    provider, model = embedding_model().split(":", 1)
    call = _embed_gemini if provider == "gemini" else _embed_openai
    last = None
    for attempt in range(1, config.LLM_MAX_ATTEMPTS_PER_PROVIDER + 1):
        start = time.monotonic()
        try:
            vectors, tokens = _traced_embed(call, model, texts,
                                            langsmith_extra=_trace_extra(agent, None, provider, model, attempt))
            _record(thread_id=current_thread_id.get(), agent=agent, prompt_version=None, provider=provider,
                    model=model, attempt=attempt, status="ok", error=None,
                    input_tokens=tokens, output_tokens=None, cost_usd=None,
                    latency_ms=int((time.monotonic() - start) * 1000))
            return vectors
        except Exception as e:
            last = e
            _record(thread_id=current_thread_id.get(), agent=agent, prompt_version=None, provider=provider,
                    model=model, attempt=attempt, status="error", error=str(e)[:1000],
                    input_tokens=None, output_tokens=None, cost_usd=None,
                    latency_ms=int((time.monotonic() - start) * 1000))
            if isinstance(e, _NonRetryable):
                break
            if attempt < config.LLM_MAX_ATTEMPTS_PER_PROVIDER:
                _sleep(min(2 ** (attempt - 1), 8))
    raise LLMError(f"embedding failed ({provider}:{model}): {last}")
