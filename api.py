import logging
import re
import threading
import time
import uuid
import os
import zipfile

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware
from langgraph.types import Command

import config
from db import (save_codebase_files, init_db, save_codebase_context, get_codebase_context, save_brd, get_brd,
                set_codebase_owner, get_codebase_owner)
from agent import generate_brd_json, render_docx
from design_agent import generate_design_json, render_design_docx
from graph import graph
from codebase_context import extract_and_scan
from auth import router as auth_router, init_auth_db, get_current_user, wrong_host_redirect
from llm_gateway import init_llm_db, usage_summary
from workflows import (init_workflows_db, create_workflow, update_workflow, get_workflow, list_workflows,
                       set_workflow_repo)
import documents
from rag.rag_ingest import init_rag_db, ingest_repo, answer_question, repo_is_ingested, GitHubImportError as RagGitHubImportError
from rag.github_import import fetch_github_codebase, GitHubImportError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("api")

# Refuse to boot with a missing/weak session secret: an unset key made
# Starlette sign cookies with the literal string "None" (audit C3).
config.require_web_config()
logger.info(f"LangSmith tracing: {config.langsmith_status()}")

app = FastAPI(title="AI-SDLC Pipeline")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET_KEY,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
    max_age=7 * 24 * 3600,
)
app.include_router(auth_router)
os.makedirs(config.OUTPUT_DIR, exist_ok=True)
init_db()
init_auth_db()
init_rag_db()
init_llm_db()
init_workflows_db()

QA_STAGE = "QA (pre-deploy)"

# Runs currently executing in THIS server process. A run whose workflow row
# says "running" but is not in this set was cut off (page refresh killed the
# request, or the server restarted) and can be resumed from its checkpoint.
_active_runs: set[str] = set()
_active_lock = threading.Lock()


class RequirementRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=8000)

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def _valid_repo(v: str | None) -> str | None:
    if v is None:
        return v
    if not REPO_RE.match(v) or v.split("/")[1] in (".", ".."):
        raise ValueError("must be a GitHub 'owner/repo' name")
    return v


class StartPipelineRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=8000)
    codebase_id: str | None = Field(default=None, max_length=64)
    github_repo: str | None = Field(default=None, max_length=200)
    _check_repo = field_validator("github_repo")(classmethod(lambda cls, v: _valid_repo(v)))

class ResumeRequest(BaseModel):
    approved: bool
    feedback: str | None = Field(default=None, max_length=8000)
    # Only used when approving the final QA gate: the repo to open the PR on,
    # if none was chosen when the run started.
    github_repo: str | None = Field(default=None, max_length=200)
    _check_repo = field_validator("github_repo")(classmethod(lambda cls, v: _valid_repo(v)))

class GithubCodebaseRequest(BaseModel):
    repo_url: str = Field(min_length=3, max_length=300)

class RagIngestRequest(BaseModel):
    repo_full_name: str = Field(max_length=200)
    _check_repo = field_validator("repo_full_name")(classmethod(lambda cls, v: _valid_repo(v)))

class RagChatRequest(BaseModel):
    repo_full_name: str = Field(max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    _check_repo = field_validator("repo_full_name")(classmethod(lambda cls, v: _valid_repo(v)))


# ---------------------------------------------------------------- helpers

def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Log in with GitHub first")
    return user


def require_owned_workflow(thread_id: str, user: dict) -> dict:
    wf = get_workflow(thread_id)
    # 404 (not 403) for someone else's thread, so ids can't be probed.
    if wf is None or wf["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="No such pipeline run")
    return wf


def _docx_path(name: str) -> str:
    return os.path.join(config.OUTPUT_DIR, f"{name}.docx")


def _file_or_404(path: str, filename: str) -> FileResponse:
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(path, filename=filename)


def format_interrupt_response(thread_id, result):
    payload = result["__interrupt__"][0].value
    return {"status": "paused", "thread_id": thread_id, "stage": payload["stage"], "data": payload["data"],
            "documents": documents.list_documents(thread_id)}


def _render_documents(thread_id: str, stage: str | None, data: dict | None) -> None:
    """Render the reviewed stage's DOCX. Never fails the request: the pipeline
    result matters more than the document, and the error is logged."""
    try:
        state = graph.get_state({"configurable": {"thread_id": thread_id}}).values
        if stage is not None:
            documents.write_for_stage(thread_id, stage, data or {}, state)
        else:  # completed run: make sure every document exists
            for st, value in (("Requirements (BRD)", state.get("brd")), ("Design", state.get("design")),
                              ("Development", state.get("dev_output"))):
                if value and not documents.get_document(thread_id, documents.STAGE_TO_KIND[st]):
                    documents.write_for_stage(thread_id, st, value, state)
            if state.get("qa_output"):
                qa = {**state["qa_output"], "generated_files": state.get("codegen_output") or []}
                documents.write_for_stage(thread_id, QA_STAGE, qa, state)
    except Exception:
        logger.exception(f"DOCX rendering failed | thread_id={thread_id} | stage={stage}")


def verify_deploy_repo(repo: str, token: str) -> None:
    """The deploy target must be a PUBLIC repo the user can push to (decision 7)."""
    try:
        resp = requests.get(f"{config.GITHUB_API_URL}/repos/{repo}",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                            timeout=10)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")
    if resp.status_code == 404:
        raise HTTPException(status_code=400, detail=f"Repository '{repo}' not found (or not visible to your login).")
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="GitHub rejected your login token — log out and log in again.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub returned {resp.status_code} for '{repo}'.")
    info = resp.json()
    if info.get("private"):
        raise HTTPException(status_code=400, detail="Only public repositories can be deploy targets.")
    if not (info.get("permissions") or {}).get("push"):
        raise HTTPException(status_code=403, detail=f"You don't have push access to '{repo}'.")


def _run_graph(thread_id: str, graph_input) -> dict:
    """Runs/resumes the graph and keeps the workflows row in sync. ANY
    exception (not just RuntimeError, audit C8) becomes a structured 502; the
    checkpoint stays at the last good step, so the run can be retried."""
    config_ = {
        "configurable": {"thread_id": thread_id},
        # Picked up by LangSmith when tracing is enabled: groups every graph
        # step and LLM call of one pipeline run under the same thread.
        "run_name": "ai-sdlc-pipeline",
        "metadata": {"thread_id": thread_id},
        "tags": ["ai-sdlc"],
    }
    with _active_lock:
        if thread_id in _active_runs:
            raise HTTPException(status_code=409, detail="This run is already in progress — wait for the current step to finish.")
        _active_runs.add(thread_id)
    update_workflow(thread_id, status="running")
    try:
        result = graph.invoke(graph_input, config=config_)
    except Exception as e:
        logger.exception(f"PIPELINE failed | thread_id={thread_id}")
        update_workflow(thread_id, status="failed", last_error=f"{type(e).__name__}: {e}"[:2000])
        raise HTTPException(status_code=502, detail={
            "message": f"{type(e).__name__}: {e}"[:1000],
            "thread_id": thread_id,
            "retryable": True,
        })
    finally:
        with _active_lock:
            _active_runs.discard(thread_id)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        update_workflow(thread_id, status="awaiting_approval", current_stage=payload["stage"])
        _render_documents(thread_id, payload["stage"], payload["data"])
        return format_interrupt_response(thread_id, result)

    update_workflow(thread_id, status="completed", current_stage="Deploy")
    _render_documents(thread_id, None, None)
    docs = documents.list_documents(thread_id)
    design_doc = next((d for d in docs if d["kind"] == "design"), None)
    return {"status": "completed", "thread_id": thread_id, "result": result, "documents": docs,
            "download_url": design_doc["url"] if design_doc else None}


def _check_codebase_access(codebase_id: str, user: dict) -> dict:
    owner = get_codebase_owner(codebase_id)
    context = get_codebase_context(codebase_id)
    if context is None or owner != user["id"]:
        raise HTTPException(status_code=404, detail="No codebase found for that codebase_id")
    return context


def _store_codebase(context: dict, user: dict, source: str) -> str:
    codebase_id = uuid.uuid4().hex
    save_codebase_context(codebase_id, context)
    save_codebase_files(codebase_id, context["file_full_contents"])
    set_codebase_owner(codebase_id, user["id"], source)
    return codebase_id


# ---------------------------------------------------------------- legacy standalone BRD / design

@app.post("/brd")
def create_brd(payload: RequirementRequest, request: Request):
    require_user(request)
    start = time.time()
    logger.info(f"BRD request received | chars={len(payload.request_text)}")
    try:
        data = generate_brd_json(payload.request_text)
    except RuntimeError as e:
        logger.error(f"BRD request failed | reason={e}")
        raise HTTPException(status_code=502, detail=str(e))

    doc_id = uuid.uuid4().hex[:12]
    render_docx(data, _docx_path(doc_id))
    save_brd(doc_id, data)

    logger.info(f"BRD request completed | doc_id={doc_id} | took={round(time.time()-start,2)}s")
    return {"doc_id": doc_id, "brd_json": data, "download_url": f"/brd/{doc_id}/download"}


@app.get("/brd/{doc_id}/download")
def download_brd(doc_id: str, request: Request):
    require_user(request)
    if not doc_id.isalnum():
        raise HTTPException(status_code=404, detail="Document not found")
    return _file_or_404(_docx_path(doc_id), f"{doc_id}.docx")


@app.post("/brd/{doc_id}/design")
def create_design(doc_id: str, request: Request):
    require_user(request)
    brd = get_brd(doc_id)
    if brd is None:
        raise HTTPException(status_code=404, detail="No BRD found for that doc_id")
    logger.info(f"DESIGN request received | doc_id={doc_id}")
    start = time.time()
    try:
        design_data = generate_design_json(brd)
    except RuntimeError as e:
        logger.error(f"DESIGN failed | doc_id={doc_id} | reason={e}")
        raise HTTPException(status_code=502, detail=str(e))

    render_design_docx(design_data, _docx_path(f"{doc_id}_design"))
    logger.info(f"DESIGN completed | doc_id={doc_id} | took={round(time.time()-start,2)}s")
    return {"doc_id": doc_id, "design_json": design_data, "download_url": f"/brd/{doc_id}/design/download"}


@app.get("/brd/{doc_id}/design/download")
def download_design(doc_id: str, request: Request):
    require_user(request)
    if not doc_id.isalnum():
        raise HTTPException(status_code=404, detail="Document not found")
    return _file_or_404(_docx_path(f"{doc_id}_design"), f"{doc_id}_design.docx")


# ---------------------------------------------------------------- repos

@app.get("/repos")
def list_repos(request: Request):
    user = require_user(request)
    resp = requests.get(
        "https://api.github.com/user/repos",
        headers={"Authorization": f"Bearer {user['access_token']}"},
        # Public repos only (decision 7) — the OAuth scope is public_repo, so a
        # private repo could be selected here but never deployed to.
        params={"per_page": 100, "sort": "updated", "affiliation": "owner", "visibility": "public"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch repos from GitHub — try logging in again")

    repos = [{"full_name": r["full_name"], "private": r["private"]} for r in resp.json() if not r.get("private")]
    return {"repos": repos}


# ---------------------------------------------------------------- pipeline

@app.post("/pipeline/start")
def start_pipeline(payload: StartPipelineRequest, request: Request):
    user = require_user(request)
    thread_id = str(uuid.uuid4())
    logger.info(f"PIPELINE start | thread_id={thread_id} | codebase_id={payload.codebase_id} | user_id={user['id']} | github_repo={payload.github_repo}")

    codebase_context = None
    codebase_id = payload.codebase_id
    if payload.github_repo:
        # Fail fast: the deploy target must be public and pushable by this user.
        verify_deploy_repo(payload.github_repo, user["access_token"])
    if payload.codebase_id:
        # Explicit zip upload / URL import always wins — it's a deliberate user action.
        codebase_context = _check_codebase_access(payload.codebase_id, user)
    elif payload.github_repo:
        # No codebase given, but a Deploy-target repo was selected — auto-fetch its
        # real files so Codegen isn't working blind.
        try:
            codebase_context = fetch_github_codebase(payload.github_repo, token=user["access_token"])
            logger.info(f"CODEBASE auto-fetched from deploy target | repo={payload.github_repo} | files={len(codebase_context['file_tree'])}")
        except GitHubImportError as e:
            raise HTTPException(status_code=400, detail=f"Couldn't read '{payload.github_repo}': {e}")
        # Saved under a codebase_id so Codegen can anchor edits to real file content.
        codebase_id = _store_codebase(codebase_context, user, f"github:{payload.github_repo}")

    create_workflow(thread_id, user["id"], payload.request_text, payload.github_repo, codebase_id)
    return _run_graph(thread_id, {
        "user_request": payload.request_text, "codebase_context": codebase_context,
        "codebase_id": codebase_id,
        "brd": None, "design": None,
        "dev_output": None, "test_cases": None, "codegen_output": None,
        "qa_output": None, "qa_retry_count": 0, "qa_feedback": None, "qa_auto_retry": None,
        "deploy_output": None, "approved": None, "feedback": None,
        "user_id": user["id"], "github_repo": payload.github_repo,
    })


@app.post("/pipeline/{thread_id}/resume")
def resume_pipeline(thread_id: str, payload: ResumeRequest, request: Request):
    user = require_user(request)
    require_owned_workflow(thread_id, user)
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    pending = [i for task in snapshot.tasks for i in task.interrupts]
    logger.info(f"PIPELINE resume | thread_id={thread_id} | approved={payload.approved} | pending={len(pending)} | next={snapshot.next}")

    if pending:
        stage = pending[0].value.get("stage")
        qa_status = (pending[0].value.get("data") or {}).get("overall_status")
        if payload.approved and stage == QA_STAGE and qa_status != "pass":
            # API-level gate; the graph enforces the same rule (qa_gate_router).
            raise HTTPException(status_code=409, detail="QA has not passed — deploy cannot be approved. Reject with feedback to send it back to the Coding agent.")
        if not payload.approved and not (payload.feedback or "").strip():
            raise HTTPException(status_code=422, detail="Rejecting requires feedback explaining what should change.")
        resume = {"approved": payload.approved, "feedback": payload.feedback}
        if payload.approved and stage == QA_STAGE:
            repo = payload.github_repo or snapshot.values.get("github_repo")
            if not repo:
                raise HTTPException(status_code=409, detail="Choose a public GitHub repository to open the pull request on.")
            verify_deploy_repo(repo, user["access_token"])
            if repo != snapshot.values.get("github_repo"):
                resume["github_repo"] = repo
                set_workflow_repo(thread_id, repo)
        return _run_graph(thread_id, Command(resume=resume))

    if snapshot.next:
        # A node failed last time (LLM/network error). The checkpoint is at the
        # last good step, so invoking with None re-runs the failed node.
        logger.info(f"PIPELINE retry after failure | thread_id={thread_id} | next={snapshot.next}")
        return _run_graph(thread_id, None)

    raise HTTPException(status_code=409, detail="This pipeline run has already completed.")


@app.get("/pipeline")
def my_pipelines(request: Request):
    user = require_user(request)
    out = []
    for w in list_workflows(user["id"]):
        item = {k: w[k] for k in ("thread_id", "request_text", "github_repo", "status", "current_stage", "created_at", "updated_at")}
        if item["status"] == "running" and w["thread_id"] not in _active_runs:
            item["status"] = "interrupted"  # cut off by a refresh/restart; resumable
        out.append(item)
    return {"workflows": out}


def run_state(thread_id: str, wf: dict, snapshot) -> str:
    """awaiting_approval | running | interrupted | failed | completed — derived
    from the checkpoint (source of truth) plus whether this process is
    executing the run right now."""
    if thread_id in _active_runs:
        return "running"
    if any(task.interrupts for task in snapshot.tasks):
        return "awaiting_approval"
    if snapshot.next:
        return "failed" if wf["status"] == "failed" else "interrupted"
    if snapshot.values:
        return "completed"
    return "interrupted"  # started but no checkpoint yet (cut off during the very first step)


@app.get("/pipeline/{thread_id}")
def pipeline_status(thread_id: str, request: Request):
    """Current state of one run — lets the UI re-attach after a refresh or a
    server restart. Everything comes from the LangGraph checkpoint."""
    user = require_user(request)
    wf = require_owned_workflow(thread_id, user)
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    pending = [i for task in snapshot.tasks for i in task.interrupts]
    state = run_state(thread_id, wf, snapshot)
    out = {
        "thread_id": thread_id,
        "state": state,
        "workflow": {k: v for k, v in wf.items() if k != "user_id"},
        "next": list(snapshot.next),
        "usage": usage_summary(thread_id),
        "documents": documents.list_documents(thread_id),
    }
    if pending:
        out["pending"] = {"stage": pending[0].value["stage"], "data": pending[0].value["data"]}
    if state == "completed":
        v = snapshot.values
        out["result"] = {
            "deploy_output": v.get("deploy_output"),
            "qa_output": v.get("qa_output"),
            "design": {"title": (v.get("design") or {}).get("title")},
            "brd": {"title": (v.get("brd") or {}).get("title")},
        }
    return out


@app.get("/pipeline/{thread_id}/documents")
def pipeline_documents(thread_id: str, request: Request):
    user = require_user(request)
    require_owned_workflow(thread_id, user)
    return {"documents": documents.list_documents(thread_id)}


@app.get("/pipeline/{thread_id}/documents/{kind}")
def download_pipeline_document(thread_id: str, kind: str, request: Request):
    user = require_user(request)
    require_owned_workflow(thread_id, user)
    found = documents.get_document(thread_id, kind)
    if not found:
        raise HTTPException(status_code=404, detail="Document not found")
    path, filename = found
    return FileResponse(path, filename=filename,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/pipeline/{thread_id}/download")
def download_pipeline_design(thread_id: str, request: Request):
    """Kept for old links: the run's design document."""
    return download_pipeline_document(thread_id, "design", request)


# ---------------------------------------------------------------- codebase input

@app.post("/codebase/upload")
async def upload_codebase(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    zip_bytes = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(zip_bytes) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Zip file too large (max {config.MAX_UPLOAD_BYTES // (1024*1024)}MB)")
    try:
        context = extract_and_scan(zip_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Unsafe zip contents: {e}")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File is not a valid zip archive")

    codebase_id = _store_codebase(context, user, "zip")
    logger.info(f"CODEBASE uploaded | codebase_id={codebase_id} | files={len(context['file_tree'])}")
    return {"codebase_id": codebase_id, "file_count": len(context["file_tree"]), "file_tree": context["file_tree"][:50]}


@app.post("/codebase/from-github")
def codebase_from_github(payload: GithubCodebaseRequest, request: Request):
    """Import a public repo by URL as the grounding codebase. The UI always
    called this, but the endpoint never existed (audit C5)."""
    user = require_user(request)
    try:
        context = fetch_github_codebase(payload.repo_url, token=user["access_token"])
    except GitHubImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub request failed: {e}")

    codebase_id = _store_codebase(context, user, context["root_path"])
    logger.info(f"CODEBASE imported | codebase_id={codebase_id} | source={context['root_path']} | files={len(context['file_tree'])}")
    return {"codebase_id": codebase_id, "file_count": len(context["file_tree"]), "file_tree": context["file_tree"][:50]}


# ---------------------------------------------------------------- RAG

@app.post("/rag/ingest")
def rag_ingest(payload: RagIngestRequest, request: Request):
    user = require_user(request)
    logger.info(f"RAG ingest requested | repo={payload.repo_full_name} | user_id={user['id']}")
    try:
        stats = ingest_repo(payload.repo_full_name, token=user["access_token"])
    except RagGitHubImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    logger.info(f"RAG ingest completed | repo={payload.repo_full_name} | {stats}")
    return stats


@app.get("/rag/status")
def rag_status(repo_full_name: str, request: Request):
    require_user(request)
    return {"ingested": repo_is_ingested(repo_full_name)}


@app.post("/rag/chat")
def rag_chat(payload: RagChatRequest, request: Request):
    require_user(request)
    logger.info(f"RAG chat | repo={payload.repo_full_name} | chars={len(payload.question)}")
    try:
        result = answer_question(payload.repo_full_name, payload.question)
    except Exception as e:
        logger.error(f"RAG chat failed | reason={e}")
        raise HTTPException(status_code=502, detail=str(e))
    return result


@app.get("/")
def serve_ui(request: Request):
    redirect = wrong_host_redirect(request)
    if redirect:
        return redirect
    return FileResponse(os.path.join(config.ROOT_DIR, "static", "index.html"))
