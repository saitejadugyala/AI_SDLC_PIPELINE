"""Central configuration. Every module reads settings from here instead of
calling os.getenv / load_dotenv itself, so there is one place that knows
which variables exist, what their defaults are, and which are required.

Paths resolve relative to the repository root, not the process CWD, so
running uvicorn from another directory no longer creates a second app.db.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _path(name: str, default: Path) -> str:
    value = _env(name)
    p = Path(value) if value else default
    if not p.is_absolute():
        p = ROOT_DIR / p
    return str(p)


# ---------- storage ----------
APP_DB_PATH = _path("APP_DB_PATH", ROOT_DIR / "app.db")
CHECKPOINT_DB_PATH = _path("CHECKPOINT_DB_PATH", ROOT_DIR / "checkpoints.db")
OUTPUT_DIR = _path("OUTPUT_DIR", ROOT_DIR / "outputs")

# ---------- web / auth ----------
APP_BASE_URL = _env("APP_BASE_URL", "http://localhost:8000")
SESSION_SECRET_KEY = _env("SESSION_SECRET_KEY")
GITHUB_OAUTH_CLIENT_ID = _env("GITHUB_OAUTH_CLIENT_ID")
GITHUB_OAUTH_CLIENT_SECRET = _env("GITHUB_OAUTH_CLIENT_SECRET")
GITHUB_OAUTH_SCOPE = "public_repo"  # deliberate: public repos only, never 'repo'
COOKIE_SECURE = APP_BASE_URL.startswith("https://")

# ---------- LLM ----------
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_EMBED_MODEL = _env("GEMINI_EMBED_MODEL", "gemini-embedding-001")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_EMBED_MODEL = _env("OPENAI_EMBED_MODEL", "text-embedding-3-small")
# RAG embeddings: "auto" = Gemini if GEMINI_API_KEY is set, otherwise OpenAI.
EMBEDDING_PROVIDER = _env("EMBEDDING_PROVIDER", "auto")
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "openrouter/auto")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Comma-separated provider order; providers without a key are skipped.
LLM_PROVIDER_ORDER = [p.strip() for p in _env("LLM_PROVIDER_ORDER", "gemini,openai,openrouter").split(",") if p.strip()]
LLM_MAX_ATTEMPTS_PER_PROVIDER = int(_env("LLM_MAX_ATTEMPTS_PER_PROVIDER", "3"))
LLM_TIMEOUT_SECONDS = int(_env("LLM_TIMEOUT_SECONDS", "90"))

# ---------- observability (LangSmith) ----------
# The langsmith SDK and LangGraph read these from the environment themselves;
# they are listed here so there is one documented place for every setting.
LANGSMITH_TRACING = (_env("LANGSMITH_TRACING", "false") or "").lower() == "true"
LANGSMITH_API_KEY = _env("LANGSMITH_API_KEY")
LANGSMITH_PROJECT = _env("LANGSMITH_PROJECT", "ai-sdlc-pipeline")
LANGSMITH_ENDPOINT = _env("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
if LANGSMITH_TRACING and LANGSMITH_PROJECT:
    os.environ.setdefault("LANGSMITH_PROJECT", LANGSMITH_PROJECT)


def langsmith_status() -> str:
    if not LANGSMITH_TRACING:
        return "disabled (set LANGSMITH_TRACING=true to enable)"
    if not LANGSMITH_API_KEY:
        return "MISCONFIGURED: LANGSMITH_TRACING=true but LANGSMITH_API_KEY is empty"
    return f"enabled (project '{LANGSMITH_PROJECT}')"


# ---------- pipeline ----------
MAX_QA_RETRIES = int(_env("MAX_QA_RETRIES", "3"))
GITHUB_API_URL = _env("GITHUB_API_URL", "https://api.github.com")
MCP_SERVER_PATH = _path("MCP_SERVER_PATH", ROOT_DIR / "mcp_server" / "github_mcp_server.py")

# ---------- uploads ----------
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_UNCOMPRESSED_BYTES = int(_env("MAX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(_env("MAX_ZIP_ENTRIES", "5000"))


class ConfigError(RuntimeError):
    pass


def require_web_config() -> None:
    """Called once at API startup. Refuses to boot with settings that would
    make the app insecure (unsigned/forgeable sessions) rather than limping
    along. OAuth/LLM keys are NOT required here: the app can boot and serve
    a clear error on the routes that need them."""
    if not SESSION_SECRET_KEY or len(SESSION_SECRET_KEY) < 32:
        raise ConfigError(
            "SESSION_SECRET_KEY must be set to a random value of at least 32 characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )


def oauth_configured() -> bool:
    return bool(GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET)
