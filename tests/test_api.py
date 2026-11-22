"""HTTP API: auth, ownership, QA gate, error recovery, uploads, new endpoints.
Agents and GitHub are MOCKED; requests go through FastAPI's TestClient."""

import io
import os
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

import config
import graph as g
import qa_agent
import api
from conftest import create_user, session_cookie_for
from test_graph_flow import Fake


@pytest.fixture
def fake(monkeypatch, request):
    f = Fake(getattr(request, "param", []))
    monkeypatch.setattr(g, "generate_brd_json", f.brd)
    monkeypatch.setattr(g, "generate_design_json", f.design)
    monkeypatch.setattr(g, "generate_dev_plan_json", f.plan)
    monkeypatch.setattr(g, "generate_test_cases", lambda req, design, feedback=None: [])
    monkeypatch.setattr(g, "generate_all_code", f.codegen)
    monkeypatch.setattr(qa_agent, "run_qa", f.run_qa)
    monkeypatch.setattr(g, "_run_deploy", f.deploy)
    return f


def client_for(user_id=None):
    c = TestClient(api.app, raise_server_exceptions=False, base_url=config.APP_BASE_URL)
    if user_id is not None:
        c.cookies.set("session", session_cookie_for(user_id))
    return c


@pytest.fixture
def alice():
    return client_for(create_user(2001, "alice2"))


@pytest.fixture
def bob():
    return client_for(create_user(2002, "bob2"))


@pytest.fixture(autouse=True)
def fake_repo_permissions(monkeypatch):
    """Deploy-target permission check against GitHub — MOCKED. Repos named
    '*/readonly' or '*/secret' are rejected like GitHub would."""
    from fastapi import HTTPException
    checked = []

    def verify(repo, token):
        checked.append((repo, token))
        if repo.endswith("/readonly"):
            raise HTTPException(status_code=403, detail=f"You don't have push access to '{repo}'.")
        if repo.endswith("/secret"):
            raise HTTPException(status_code=400, detail="Only public repositories can be deploy targets.")
    monkeypatch.setattr(api, "verify_deploy_repo", verify)
    return checked


@pytest.fixture(autouse=True)
def fake_repo_fetch(monkeypatch):
    """Selecting a deploy repo auto-fetches its files — MOCKED here."""
    monkeypatch.setattr(api, "fetch_github_codebase", lambda repo, token=None: {
        "root_path": f"github:{repo}", "file_tree": ["app.py"], "file_previews": {},
        "file_full_contents": {"app.py": "x = 1\n"}})


def advance_to_qa(c, thread_id):
    r = None
    for _ in range(3):
        r = c.post(f"/pipeline/{thread_id}/resume", json={"approved": True})
        assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- startup / auth

def test_app_refuses_to_start_without_session_secret():
    """Regression for audit C3 (cookies were signed with the string 'None')."""
    env = {**os.environ, "SESSION_SECRET_KEY": ""}
    r = subprocess.run([sys.executable, "-c", "import api"], cwd=config.ROOT_DIR, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "SESSION_SECRET_KEY must be set" in r.stderr


def test_cookie_signed_with_wrong_secret_is_not_a_login():
    import base64, json, itsdangerous
    forged = itsdangerous.TimestampSigner("None").sign(base64.b64encode(json.dumps({"user_id": 1}).encode())).decode()
    c = TestClient(api.app)
    c.cookies.set("session", forged)
    assert c.get("/auth/me").json() == {"logged_in": False}


def test_pipeline_endpoints_require_login(fake):
    c = client_for()
    assert c.post("/pipeline/start", json={"request_text": "x"}).status_code == 401
    assert c.post("/pipeline/abc/resume", json={"approved": True}).status_code == 401
    assert c.get("/pipeline").status_code == 401
    assert c.post("/codebase/upload", files={"file": ("a.zip", b"x")}).status_code == 401
    assert c.get("/rag/status", params={"repo_full_name": "a/b"}).status_code == 401


def test_oauth_login_sets_hardened_state_cookie():
    r = client_for().get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 307
    assert "scope=public_repo" in r.headers["location"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/auth/github/callback" in cookie


def test_oauth_callback_rejects_state_mismatch_with_friendly_redirect():
    c = client_for()
    c.cookies.set("oauth_state", "expected", path="/auth/github/callback")
    r = c.get("/auth/github/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?login_error=expired"
    assert c.get("/auth/me").json() == {"logged_in": False}  # still no login


def test_oauth_callback_without_state_cookie_is_rejected():
    r = client_for().get("/auth/github/callback", params={"code": "c", "state": "s"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=expired"


def test_oauth_user_cancel_is_reported():
    r = client_for().get("/auth/github/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=denied"


def test_wrong_host_is_redirected_to_app_base_url():
    """127.0.0.1 vs localhost: the state cookie must be set on the host GitHub returns to."""
    c = TestClient(api.app, base_url="http://127.0.0.1:8000")
    r = c.get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"{config.APP_BASE_URL}/auth/github/login"
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"{config.APP_BASE_URL}/"


# ---------------------------------------------------------------- pipeline

def test_start_creates_owned_workflow_and_lists_it(fake, alice):
    r = alice.post("/pipeline/start", json={"request_text": "build X"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused" and body["stage"] == "Requirements (BRD)"
    listed = alice.get("/pipeline").json()["workflows"]
    assert listed[0]["thread_id"] == body["thread_id"]
    assert listed[0]["status"] == "awaiting_approval"
    status = alice.get(f"/pipeline/{body['thread_id']}").json()
    assert status["pending"]["stage"] == "Requirements (BRD)"
    assert "user_id" not in status["workflow"]


def test_other_user_cannot_see_or_resume_thread(fake, alice, bob):
    """Regression for audit C4."""
    tid = alice.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    assert bob.post(f"/pipeline/{tid}/resume", json={"approved": True}).status_code == 404
    assert bob.get(f"/pipeline/{tid}").status_code == 404
    assert bob.get(f"/pipeline/{tid}/download").status_code == 404
    assert bob.get("/pipeline").json()["workflows"] == []


def test_reject_requires_feedback(fake, alice):
    tid = alice.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    assert alice.post(f"/pipeline/{tid}/resume", json={"approved": False, "feedback": "  "}).status_code == 422


@pytest.mark.parametrize("fake", [["fail"] * 5], indirect=True)
def test_api_refuses_to_approve_failed_qa(fake, alice):
    tid = alice.post("/pipeline/start", json={"request_text": "x", "github_repo": "alice2/demo"}).json()["thread_id"]
    qa = advance_to_qa(alice, tid)
    assert qa["stage"] == "QA (pre-deploy)" and qa["data"]["overall_status"] == "fail"
    r = alice.post(f"/pipeline/{tid}/resume", json={"approved": True})
    assert r.status_code == 409
    assert fake.calls["deploy"] == []


@pytest.mark.parametrize("fake", [["pass"]], indirect=True)
def test_full_flow_to_mocked_pr(fake, alice, monkeypatch):
    monkeypatch.setattr(api, "fetch_github_codebase", lambda repo, token=None: {
        "root_path": f"github:{repo}", "file_tree": ["app.py"], "file_previews": {}, "file_full_contents": {"app.py": "x=1\n"}})
    tid = alice.post("/pipeline/start", json={"request_text": "x", "github_repo": "alice2/demo"}).json()["thread_id"]
    qa = advance_to_qa(alice, tid)
    assert qa["data"]["overall_status"] == "pass"
    done = alice.post(f"/pipeline/{tid}/resume", json={"approved": True})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "completed"
    assert body["result"]["deploy_output"]["pull_request"]["html_url"].endswith("/pull/1")
    assert alice.get(body["download_url"]).status_code == 200
    assert alice.get("/pipeline").json()["workflows"][0]["status"] == "completed"
    assert alice.post(f"/pipeline/{tid}/resume", json={"approved": True}).status_code == 409


def test_node_failure_returns_structured_502_and_retry_resumes(fake, alice, monkeypatch):
    """Regression for audit C8: non-RuntimeError failures used to be bare 500s
    and the run could not continue."""
    tid = alice.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]

    def broken(*a, **k):
        raise ValueError("model returned garbage")
    monkeypatch.setattr(g, "generate_design_json", broken)
    r = alice.post(f"/pipeline/{tid}/resume", json={"approved": True})
    assert r.status_code == 502
    assert r.json()["detail"]["retryable"] is True and r.json()["detail"]["thread_id"] == tid
    assert alice.get("/pipeline").json()["workflows"][0]["status"] == "failed"

    monkeypatch.setattr(g, "generate_design_json", fake.design)
    r = alice.post(f"/pipeline/{tid}/resume", json={"approved": True})
    assert r.status_code == 200 and r.json()["stage"] == "Design"


def test_request_validation(alice):
    assert alice.post("/pipeline/start", json={"request_text": ""}).status_code == 422
    assert alice.post("/pipeline/start", json={"request_text": "x", "github_repo": "../etc"}).status_code == 422


# ---------------------------------------------------------------- codebase input

def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_upload_valid_zip_and_ownership(fake, alice, bob):
    r = alice.post("/codebase/upload", files={"file": ("p.zip", _zip({"app.py": "print(1)\n"}))})
    assert r.status_code == 200, r.text
    cid = r.json()["codebase_id"]
    assert r.json()["file_tree"] == ["app.py"]
    assert alice.post("/pipeline/start", json={"request_text": "x", "codebase_id": cid}).status_code == 200
    # bob can't use alice's codebase
    assert bob.post("/pipeline/start", json={"request_text": "x", "codebase_id": cid}).status_code == 404


def test_upload_rejects_zip_slip_and_zip_bomb(alice, monkeypatch):
    r = alice.post("/codebase/upload", files={"file": ("p.zip", _zip({"../evil.py": "x"}))})
    assert r.status_code == 400 and "Unsafe path" in r.text
    monkeypatch.setattr(config, "MAX_UNCOMPRESSED_BYTES", 1_000_000)
    bomb = _zip({"big.txt": "0" * 5_000_000})
    assert len(bomb) < 20_000
    r = alice.post("/codebase/upload", files={"file": ("p.zip", bomb)})
    assert r.status_code == 400 and "expands to" in r.text


def test_upload_temp_dirs_are_cleaned(alice):
    import glob, tempfile
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "codebase_upload_*")))
    alice.post("/codebase/upload", files={"file": ("p.zip", _zip({"a.py": "1"}))})
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "codebase_upload_*")))
    assert after <= before


def test_codebase_from_github_endpoint_exists(alice, monkeypatch):
    """Regression for audit C5 (UI called a route that didn't exist)."""
    captured = {}

    def fake_fetch(url, token=None):
        captured.update(url=url, token=token)
        return {"root_path": "github:o/r@main", "file_tree": ["a.py", "b.py"], "file_previews": {},
                "file_full_contents": {"a.py": "1", "b.py": "2"}}
    monkeypatch.setattr(api, "fetch_github_codebase", fake_fetch)
    r = alice.post("/codebase/from-github", json={"repo_url": "https://github.com/o/r"})
    assert r.status_code == 200, r.text
    assert r.json()["file_count"] == 2
    assert captured == {"url": "https://github.com/o/r", "token": "gho_fake_alice2"}


def test_missing_documents_are_404_not_500(alice):
    assert alice.get("/brd/doesnotexist/download").status_code == 404
    assert alice.get("/brd/doesnotexist/design/download").status_code == 404


# ---------------------------------------------------------------- documents

def test_each_review_gate_renders_its_docx_with_clean_name(fake, alice):
    from docx import Document
    body = alice.post("/pipeline/start", json={"request_text": "x"}).json()
    tid = body["thread_id"]
    assert [d["kind"] for d in body["documents"]] == ["brd"]
    brd_doc = body["documents"][0]
    assert brd_doc["filename"] == "BRD_-_BRD.docx"  # title "BRD" from the fake agent
    r = alice.get(brd_doc["url"])
    assert r.status_code == 200 and 'filename="BRD_-_BRD.docx"' in r.headers["content-disposition"]
    doc = Document(io.BytesIO(r.content))
    assert any("Functional Requirements" in p.text for p in doc.paragraphs)

    kinds = []
    for _ in range(3):
        kinds = [d["kind"] for d in alice.post(f"/pipeline/{tid}/resume", json={"approved": True}).json()["documents"]]
    assert kinds == ["brd", "design", "plan", "qa"]
    qa = Document(io.BytesIO(alice.get(f"/pipeline/{tid}/documents/qa").content))
    text = "\n".join(p.text for p in qa.paragraphs)
    assert "QA Report" in text and "PASS" in text and "not executed" in text.lower()
    assert alice.get(f"/pipeline/{tid}/documents").json()["documents"][-1]["filename"] == "BRD_-_QA_Report.docx"


def test_documents_are_owner_only(fake, alice, bob):
    tid = alice.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    assert bob.get(f"/pipeline/{tid}/documents").status_code == 404
    assert bob.get(f"/pipeline/{tid}/documents/brd").status_code == 404
    assert alice.get(f"/pipeline/{tid}/documents/nope").status_code == 404


def test_clean_filename():
    from documents import clean_filename
    assert clean_filename("Self-Service Password Reset!", "BRD") == "Self-Service_Password_Reset_-_BRD.docx"
    assert clean_filename("../../etc/passwd", "Design") == "etcpasswd_-_Design.docx"
    assert clean_filename(None, "QA_Report") == "Pipeline_-_QA_Report.docx"


# ---------------------------------------------------------------- deploy target chosen at the QA gate

@pytest.mark.parametrize("fake", [["pass"]], indirect=True)
def test_deploy_repo_can_be_chosen_at_qa_gate(fake, alice, fake_repo_permissions):
    tid = alice.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    qa = advance_to_qa(alice, tid)
    assert qa["data"]["deploy_target"] is None and qa["data"]["grounded_on_repo"] is False
    # approving without any repo is refused
    assert alice.post(f"/pipeline/{tid}/resume", json={"approved": True}).status_code == 409
    # a repo the user can't push to is refused, and nothing deploys
    assert alice.post(f"/pipeline/{tid}/resume", json={"approved": True, "github_repo": "alice2/readonly"}).status_code == 403
    assert fake.calls["deploy"] == []
    done = alice.post(f"/pipeline/{tid}/resume", json={"approved": True, "github_repo": "alice2/target"})
    assert done.status_code == 200, done.text
    assert fake.calls["deploy"] == [("gho_fake_alice2", "alice2/target")]
    assert alice.get("/pipeline").json()["workflows"][0]["github_repo"] == "alice2/target"
    assert {d["kind"] for d in done.json()["documents"]} == {"brd", "design", "plan", "qa"}


def test_start_rejects_repo_without_push_access(fake, alice):
    before = len(alice.get("/pipeline").json()["workflows"])
    r = alice.post("/pipeline/start", json={"request_text": "x", "github_repo": "someone/readonly"})
    assert r.status_code == 403
    assert len(alice.get("/pipeline").json()["workflows"]) == before


# ---------------------------------------------------------------- revoked GitHub token

def test_revoked_github_token_logs_user_out_with_clear_message(monkeypatch):
    """GitHub answers 401 for a revoked token (MOCKED). The app session must end
    so the UI sends the user to log in again, instead of 'Could not load repos'."""
    class Resp:
        status_code = 401
        def json(self):
            return {"message": "Bad credentials"}
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: Resp())
    c = client_for(create_user(2050, "revoked-user"))
    assert c.get("/auth/me").json()["logged_in"] is True
    r = c.get("/repos")
    assert r.status_code == 401 and "log in with GitHub again" in r.json()["detail"]
    # The server tells the browser to drop the session cookie (expired, empty value).
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("session=null") and "expires=thu, 01 jan 1970" in cookie


def test_repos_lists_public_only(monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return [{"full_name": "u/pub", "private": False}, {"full_name": "u/priv", "private": True}]
    captured = {}
    monkeypatch.setattr(api.requests, "get", lambda url, **k: captured.update(url=url, params=k["params"]) or Resp())
    r = client_for(create_user(2051, "lister")).get("/repos")
    assert r.json() == {"repos": [{"full_name": "u/pub", "private": False}]}
    assert captured["params"]["visibility"] == "public"
