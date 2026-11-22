"""LLM gateway: routing, retries, fallback, validation, usage tracking.
Providers are MOCKED (no network)."""

import sqlite3

import pytest

import config
import llm_gateway as gw

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["title", "n"],
}


def _calls(thread_id):
    conn = sqlite3.connect(config.APP_DB_PATH)
    rows = conn.execute(
        "SELECT provider, attempt, status, input_tokens, cost_usd FROM llm_calls WHERE thread_id = ? ORDER BY id",
        (thread_id,),
    ).fetchall()
    conn.close()
    return rows


@pytest.fixture(autouse=True)
def _db():
    gw.init_llm_db()


def _scripted(outputs):
    """Provider stub returning/raising each item of `outputs` in turn."""
    seen = []

    def call(system, contents, schema, timeout_s):
        seen.append(contents)
        item = outputs[len(seen) - 1]
        if isinstance(item, Exception):
            raise item
        return gw._Raw(text=item, model="mock-model", input_tokens=10, output_tokens=5, cost_usd=0.001)
    call.seen = seen
    return call


def test_valid_json_returned_and_usage_recorded(monkeypatch):
    monkeypatch.setitem(gw.PROVIDERS, "gemini", _scripted(['{"title": "x", "n": 1}']))
    token = gw.current_thread_id.set("t-ok")
    try:
        out = gw.generate_json(agent="a", prompt_version="v1", system="s", contents="c", schema=SCHEMA)
    finally:
        gw.current_thread_id.reset(token)
    assert out == {"title": "x", "n": 1}
    assert _calls("t-ok") == [("gemini", 1, "ok", 10, 0.001)]
    assert gw.usage_summary("t-ok")["a"]["calls"] == 1


def test_invalid_json_then_schema_violation_then_ok_retries_with_correction(monkeypatch):
    stub = _scripted(["not json", '{"title": "x"}', '```json\n{"title": "x", "n": 2}\n```'])
    monkeypatch.setitem(gw.PROVIDERS, "gemini", stub)
    token = gw.current_thread_id.set("t-retry")
    try:
        out = gw.generate_json(agent="a", prompt_version="v1", system="s", contents="ORIGINAL", schema=SCHEMA)
    finally:
        gw.current_thread_id.reset(token)
    assert out["n"] == 2
    assert [r[2] for r in _calls("t-retry")] == ["invalid_output", "invalid_output", "ok"]
    assert "previous response was rejected" in stub.seen[1]
    assert stub.seen[1].startswith("ORIGINAL")


def test_non_retryable_error_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-test")
    gem = _scripted([gw._NonRetryable("gemini 400: API key not valid")])
    orr = _scripted(['{"title": "fallback", "n": 3}'])
    monkeypatch.setitem(gw.PROVIDERS, "gemini", gem)
    monkeypatch.setitem(gw.PROVIDERS, "openrouter", orr)
    token = gw.current_thread_id.set("t-fb")
    try:
        out = gw.generate_json(agent="a", prompt_version="v1", system="s", contents="c", schema=SCHEMA)
    finally:
        gw.current_thread_id.reset(token)
    assert out["title"] == "fallback"
    assert len(gem.seen) == 1, "a rejected request must not be retried on the same provider"
    assert [(r[0], r[2]) for r in _calls("t-fb")] == [("gemini", "rejected"), ("openrouter", "ok")]


def test_all_providers_fail_raises_llm_error_which_is_runtime_error(monkeypatch):
    monkeypatch.setitem(gw.PROVIDERS, "gemini", _scripted([TimeoutError("t")] * 3))
    with pytest.raises(RuntimeError) as ei:
        gw.generate_json(agent="a", prompt_version="v1", system="s", contents="c", schema=SCHEMA)
    assert isinstance(ei.value, gw.LLMError)
    assert "gemini#3" in str(ei.value)


def test_no_provider_configured(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    with pytest.raises(gw.LLMError, match="No LLM provider configured"):
        gw.generate_text(agent="a", prompt_version="v1", system="s", contents="c")


def test_openrouter_request_and_response_parsing(monkeypatch):
    """Exercises the real _call_openrouter with requests.post MOCKED."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-test")
    captured = {}

    class Resp:
        status_code = 200
        def json(self):
            return {"model": "some/model", "choices": [{"message": {"content": '{"title": "t", "n": 1}'}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.0002}}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json, timeout=timeout)
        return Resp()

    monkeypatch.setattr(gw.requests, "post", fake_post)
    raw = gw._call_openrouter("sys", "user", SCHEMA, 30)
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer or-test"
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["body"]["usage"] == {"include": True}
    assert (raw.model, raw.input_tokens, raw.output_tokens, raw.cost_usd) == ("some/model", 7, 3, 0.0002)


def test_openrouter_4xx_is_non_retryable_and_5xx_is_retryable(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-test")

    def resp(code):
        class R:
            status_code = code
            text = "err"
        return R()

    monkeypatch.setattr(gw.requests, "post", lambda *a, **k: resp(401))
    with pytest.raises(gw._NonRetryable):
        gw._call_openrouter("s", "u", None, 5)
    monkeypatch.setattr(gw.requests, "post", lambda *a, **k: resp(503))
    with pytest.raises(RuntimeError):
        gw._call_openrouter("s", "u", None, 5)


def test_importing_agents_needs_no_api_key():
    """Audit C11: agent modules built a Gemini client at import time."""
    import subprocess, sys, os
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
    r = subprocess.run([sys.executable, "-c", "import agent, design_agent, dev_agent, qa_agent, rag.rag_ingest"],
                       cwd=config.ROOT_DIR, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_openai_request_and_response_parsing(monkeypatch):
    """Exercises the real _call_openai with requests.post MOCKED."""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "OPENAI_MODEL", "test-model")
    captured = {}

    class Resp:
        status_code = 200
        def json(self):
            return {"model": "test-model-2026", "choices": [{"message": {"content": '{"title": "t", "n": 1}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4}}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        return Resp()

    monkeypatch.setattr(gw.requests, "post", fake_post)
    raw = gw._call_openai("sys", "user", SCHEMA, 30)
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer sk-test"}
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "usage" not in captured["body"]  # OpenRouter-only extension
    assert (raw.model, raw.input_tokens, raw.output_tokens, raw.cost_usd) == ("test-model-2026", 11, 4, None)
    assert gw._parse(raw, SCHEMA) == {"title": "t", "n": 1}


def test_fallback_order_gemini_openai_openrouter(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(config, "LLM_PROVIDER_ORDER", ["gemini", "openai", "openrouter"])
    assert gw.available_providers() == ["gemini", "openai", "openrouter"]
    gem = _scripted([gw._NonRetryable("gemini 403")])
    oai = _scripted([gw._NonRetryable("openai 401: invalid key")])
    orr = _scripted(['{"title": "third", "n": 1}'])
    monkeypatch.setitem(gw.PROVIDERS, "gemini", gem)
    monkeypatch.setitem(gw.PROVIDERS, "openai", oai)
    monkeypatch.setitem(gw.PROVIDERS, "openrouter", orr)
    token = gw.current_thread_id.set("t-3way")
    try:
        out = gw.generate_json(agent="a", prompt_version="v1", system="s", contents="c", schema=SCHEMA)
    finally:
        gw.current_thread_id.reset(token)
    assert out["title"] == "third"
    assert [(r[0], r[2]) for r in _calls("t-3way")] == [("gemini", "rejected"), ("openai", "rejected"), ("openrouter", "ok")]


def test_only_openai_configured(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    assert gw.available_providers() == ["openai"]


def test_env_example_documents_every_config_variable():
    """Keeps .env.example in sync with what config.py actually reads."""
    import re
    src = (config.ROOT_DIR / "config.py").read_text()
    used = set(re.findall(r'_(?:env|path)\("([A-Z0-9_]+)"', src)) - {"GOOGLE_API_KEY"}
    example = (config.ROOT_DIR / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", example, re.M))
    assert used - documented == set(), f"undocumented in .env.example: {sorted(used - documented)}"
    assert documented - used == set(), f"documented but unused: {sorted(documented - used)}"
    # No real secrets in the committed template. Only NAMES are reported —
    # never values — so a failure can't leak a secret into test output.
    with_values = [line.split("=", 1)[0] for line in example.splitlines()
                   if re.match(r"^[A-Z_]*(KEY|SECRET)[A-Z_]*=\S", line) and not line.startswith("LLM_")]
    assert not with_values, "secret values present in .env.example for: " + ", ".join(with_values)
