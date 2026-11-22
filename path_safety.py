"""Validation for repository-relative file paths produced by agents.

Generated paths come from an LLM, and the LLM's prompt contains untrusted
repository content, so a path is treated as untrusted input. The same rules
are enforced again inside the MCP server (the last boundary before GitHub).
"""

from pathlib import PurePosixPath

MAX_PATH_LEN = 512

# Writing here would let generated code change CI or leak through it, or
# corrupt repository internals. Nothing in the pipeline legitimately needs them.
DENIED_PREFIXES = (".git/", ".github/workflows/")
DENIED_FILENAMES = {".env", ".git", ".gitmodules"}


class UnsafePathError(ValueError):
    pass


def validate_repo_path(path: str) -> str:
    """Returns the normalized POSIX path, or raises UnsafePathError."""
    if not isinstance(path, str) or not path.strip():
        raise UnsafePathError("empty path")
    if len(path) > MAX_PATH_LEN:
        raise UnsafePathError("path too long")
    if "\x00" in path or "\\" in path:
        raise UnsafePathError(f"illegal character in path: {path!r}")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise UnsafePathError(f"absolute path not allowed: {path!r}")

    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if not parts:
        raise UnsafePathError(f"empty path: {path!r}")
    if any(p == ".." for p in parts):
        raise UnsafePathError(f"parent-directory segment not allowed: {path!r}")
    normalized = "/".join(parts)

    lower = normalized.lower()
    if any(lower.startswith(prefix) for prefix in DENIED_PREFIXES) or "/.git/" in f"/{lower}":
        raise UnsafePathError(f"writing to {normalized!r} is not allowed")
    name = parts[-1].lower()
    if name in DENIED_FILENAMES or (name.startswith(".env.") and name != ".env.example"):
        raise UnsafePathError(f"writing secret/config file {normalized!r} is not allowed")
    return normalized
