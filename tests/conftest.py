"""Test harness.

Environment is set BEFORE any app module is imported, because config.py reads
it at import time. Every database and output file goes to a temp dir, never to
the developer's real app.db/checkpoints.db.

All LLM and GitHub interactions in this suite are MOCKED. A guard fixture makes
any accidental real provider call fail loudly instead of hitting the network.
"""

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="sdlc_tests_")
TEST_SESSION_SECRET = "test-session-secret-" + "x" * 40
os.environ.update({
    "APP_DB_PATH": os.path.join(_TMP, "app.db"),
    "CHECKPOINT_DB_PATH": os.path.join(_TMP, "checkpoints.db"),
    "OUTPUT_DIR": os.path.join(_TMP, "outputs"),
    "SESSION_SECRET_KEY": TEST_SESSION_SECRET,
    "GITHUB_OAUTH_CLIENT_ID": "test-client-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "test-client-secret",
    "GEMINI_API_KEY": "test-key-not-real",
    "OPENROUTER_API_KEY": "",
    "OPENAI_API_KEY": "",
    "LLM_PROVIDER_ORDER": "gemini,openai,openrouter",
    "MAX_QA_RETRIES": "3",
    "LANGSMITH_TRACING": "false",  # tests never send traces anywhere
    "LANGCHAIN_TRACING_V2": "false",
})

import itsdangerous  # noqa: E402
import pytest  # noqa: E402

import llm_gateway  # noqa: E402


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """Block real HTTP from the test process (GitHub, OpenRouter, ...).
    localhost stays allowed for the fake-GitHub server used by the MCP test."""
    import requests
    real = requests.Session.request

    def guarded(self, method, url, *a, **k):
        if not str(url).startswith(("http://127.0.0.1", "http://localhost")):
            raise AssertionError(f"Real network call in a test: {method} {url} — mock it.")
        return real(self, method, url, *a, **k)
    monkeypatch.setattr(requests.Session, "request", guarded)
    yield


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Fail loudly if any test reaches a real provider."""
    def boom(*a, **k):
        raise AssertionError("Real LLM provider called in a test — mock it.")
    monkeypatch.setitem(llm_gateway.PROVIDERS, "gemini", boom)
    monkeypatch.setitem(llm_gateway.PROVIDERS, "openai", boom)
    monkeypatch.setitem(llm_gateway.PROVIDERS, "openrouter", boom)
    monkeypatch.setattr(llm_gateway, "_sleep", lambda s: None)
    yield


def session_cookie_for(user_id: int) -> str:
    """Builds a session cookie exactly the way Starlette's SessionMiddleware
    does, signed with the TEST secret. This is how tests 'log in'."""
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    return itsdangerous.TimestampSigner(TEST_SESSION_SECRET).sign(data).decode()


def create_user(github_id: int, username: str) -> int:
    import sqlite3
    import auth
    auth.init_auth_db()
    conn = sqlite3.connect(os.environ["APP_DB_PATH"])
    conn.execute(
        "INSERT OR IGNORE INTO users (github_id, username, avatar_url, access_token) VALUES (?, ?, ?, ?)",
        (github_id, username, None, f"gho_fake_{username}"),
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE github_id = ?", (github_id,)).fetchone()[0]
    conn.close()
    return uid
