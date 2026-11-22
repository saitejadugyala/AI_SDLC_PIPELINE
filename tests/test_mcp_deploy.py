"""GitHub MCP server and the Deploy stage.

The MCP server process and the stdio MCP transport are REAL. GitHub is a
local FAKE HTTP server (GITHUB_API_URL points at it), so no real branch or
PR is created. This is a mocked integration test, not a real GitHub deployment.
"""

import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastmcp import Client

import config
import graph as g

SERVER_PATH = config.ROOT_DIR / "mcp_server" / "github_mcp_server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location("github_mcp_server_under_test", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_exposes_exactly_three_tools_and_no_merge():
    mod = load_server_module()

    async def tools():
        async with Client(mod.mcp) as c:
            return sorted(t.name for t in await c.list_tools())
    names = asyncio.run(tools())
    assert names == ["create_branch", "open_pull_request", "write_file"]
    assert not any("merge" in n.lower() for n in names)
    source = SERVER_PATH.read_text().lower()
    assert "def merge" not in source and "/merge" not in source  # no merge code path at all


@pytest.mark.parametrize("path", [".github/workflows/x.yml", "../x", "/abs", ".git/config", ".env"])
def test_write_file_rejects_unsafe_paths_at_tool_boundary(path):
    mod = load_server_module()
    with pytest.raises(mod.ToolInputError):
        mod.write_file("pipeline-1", path, "x", "m")


# ---------------------------------------------------------------- fake GitHub

class FakeGitHub(BaseHTTPRequestHandler):
    log = []
    files = {}

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=None):
        FakeGitHub.log.append((self.command, self.path, self.headers.get("Authorization"), body))

    def do_GET(self):
        self._record()
        path = self.path.split("?")[0]
        if path == "/repos/octo/demo":
            return self._send(200, {"default_branch": "trunk"})
        if path == "/repos/octo/demo/git/ref/heads/trunk":
            return self._send(200, {"object": {"sha": "base123"}})
        if path.startswith("/repos/octo/demo/contents/"):
            return self._send(404, {"message": "Not Found"})
        self._send(404, {})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        body = self._body()
        self._record(body)
        if self.path == "/repos/octo/demo/git/refs":
            return self._send(201, {"ref": body["ref"]})
        if self.path == "/repos/octo/demo/pulls":
            return self._send(201, {"html_url": "https://github.com/octo/demo/pull/7", "number": 7})
        self._send(404, {})

    def do_PUT(self):
        body = self._body()
        self._record(body)
        FakeGitHub.files[self.path] = body
        self._send(201, {"content": {}})

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_github(monkeypatch):
    FakeGitHub.log, FakeGitHub.files = [], {}
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "GITHUB_API_URL", f"http://127.0.0.1:{srv.server_port}")
    yield FakeGitHub
    srv.shutdown()


def test_deploy_through_real_mcp_subprocess(fake_github):
    codegen_output = [
        {"path": "app.py", "status": "ok", "is_new_file": False, "updated_content": "print('hi')\n"},
        {"path": "skipped.py", "status": "failed", "error": "generation failed"},
        {"path": ".github/workflows/evil.yml", "status": "ok", "is_new_file": True, "updated_content": "x"},
    ]
    result = asyncio.run(g._run_deploy(codegen_output, "gho_user_token", "octo/demo"))

    assert result["status"] == "partial"
    assert result["files_written"] == ["app.py"]
    assert [f["path"] for f in result["files_failed"]] == [".github/workflows/evil.yml"]
    assert result["pull_request"]["html_url"] == "https://github.com/octo/demo/pull/7"

    methods_paths = [(m, p.split("?")[0]) for m, p, _, _ in fake_github.log]
    assert ("POST", "/repos/octo/demo/git/refs") in methods_paths
    assert ("PUT", "/repos/octo/demo/contents/app.py") in methods_paths
    pr_calls = [b for m, p, _, b in fake_github.log if p == "/repos/octo/demo/pulls"]
    assert pr_calls[0]["base"] == "trunk"  # detected default branch, not hardcoded main
    assert all(auth == "Bearer gho_user_token" for _, _, auth, _ in fake_github.log)
    assert not any("merge" in p for _, p, _, _ in fake_github.log)
    put = fake_github.files["/repos/octo/demo/contents/app.py"]
    assert put["branch"] == result["branch"]
