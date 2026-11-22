"""GitHub MCP server used by the Deploy stage.

Exposes exactly three tools: create_branch, write_file, open_pull_request.
There is deliberately NO merge tool. A human must always review and merge
the PR on GitHub. tests/test_mcp_server.py asserts that this stays true.

This runs as a separate process with only GITHUB_PAT / GITHUB_REPO in its
environment, and it re-validates every path it is asked to write, because it
is the last boundary before anything reaches GitHub.
"""

import os
import base64
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

GITHUB_TOKEN = os.getenv("GITHUB_PAT")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # format: "username/repo-name"
# Overridable for GitHub Enterprise and for the local fake-GitHub test server.
API_BASE = f"{os.getenv('GITHUB_API_URL', 'https://api.github.com').rstrip('/')}/repos/{GITHUB_REPO}"
TIMEOUT = 20

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Kept in sync with path_safety.py; duplicated on purpose so this server stays
# a self-contained process with no imports from the pipeline.
DENIED_PREFIXES = (".git/", ".github/workflows/")
DENIED_FILENAMES = {".env", ".git", ".gitmodules"}

mcp = FastMCP("github-mcp-server")


class ToolInputError(ValueError):
    pass


def _validate_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or len(path) > 512:
        raise ToolInputError("invalid path")
    if "\x00" in path or "\\" in path or path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise ToolInputError(f"invalid path: {path!r}")
    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ToolInputError(f"invalid path: {path!r}")
    normalized = "/".join(parts)
    lower = normalized.lower()
    if any(lower.startswith(p) for p in DENIED_PREFIXES) or "/.git/" in f"/{lower}":
        raise ToolInputError(f"writing to {normalized!r} is not allowed")
    name = parts[-1].lower()
    if name in DENIED_FILENAMES or (name.startswith(".env.") and name != ".env.example"):
        raise ToolInputError(f"writing {normalized!r} is not allowed")
    return normalized


def _validate_branch(branch_name: str) -> str:
    if not branch_name or len(branch_name) > 100 or ".." in branch_name or branch_name.startswith(("/", "-")) \
            or any(c in branch_name for c in " ~^:?*[\\"):
        raise ToolInputError(f"invalid branch name: {branch_name!r}")
    return branch_name


def _default_branch() -> str:
    resp = requests.get(API_BASE, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("default_branch") or "main"


@mcp.tool()
def create_branch(branch_name: str) -> str:
    """Create a new branch off the repository's default branch. Use this before writing any files."""
    branch_name = _validate_branch(branch_name)
    base = _default_branch()
    base_ref = requests.get(f"{API_BASE}/git/ref/heads/{quote(base, safe='')}", headers=HEADERS, timeout=TIMEOUT)
    base_ref.raise_for_status()
    base_sha = base_ref.json()["object"]["sha"]

    resp = requests.post(
        f"{API_BASE}/git/refs",
        headers=HEADERS,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        timeout=TIMEOUT,
    )
    if resp.status_code == 422:
        error_message = resp.json().get("message", "")
        if "already exists" in error_message.lower():
            return f"Branch '{branch_name}' already exists. Reusing it."
        raise ToolInputError(f"GitHub rejected this branch name: {error_message}")
    resp.raise_for_status()
    return f"Created branch '{branch_name}' from {base}."


@mcp.tool()
def write_file(branch_name: str, file_path: str, content: str, commit_message: str) -> str:
    """Create or update a file on the given branch with the given content."""
    branch_name = _validate_branch(branch_name)
    file_path = _validate_path(file_path)
    url = f"{API_BASE}/contents/{quote(file_path)}"
    existing = requests.get(url, headers=HEADERS, params={"ref": branch_name}, timeout=TIMEOUT)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    payload = {
        "message": commit_message[:500],
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": branch_name,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, headers=HEADERS, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    action = "Updated" if sha else "Created"
    return f"{action} '{file_path}' on branch '{branch_name}'."


@mcp.tool()
def open_pull_request(branch_name: str, title: str, body: str) -> dict:
    """Open a pull request from the given branch into the default branch. Never merges."""
    branch_name = _validate_branch(branch_name)
    base = _default_branch()
    resp = requests.post(
        f"{API_BASE}/pulls",
        headers=HEADERS,
        json={"title": title[:250], "body": body, "head": branch_name, "base": base},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    pr = resp.json()
    return {"html_url": pr["html_url"], "number": pr["number"], "base": base, "head": branch_name}


if __name__ == "__main__":
    mcp.run(transport="stdio")
