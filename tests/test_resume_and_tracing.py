"""Refresh/restart recovery (state comes from the LangGraph checkpoint) and
LangSmith tracing of LLM calls. Agents are MOCKED; LangSmith runs in
"local" mode (run trees are built in memory, nothing is sent)."""

import pytest
from langsmith import traceable, tracing_context

import api
import graph as g
import llm_gateway as gw
from workflows import update_workflow
from test_api import client_for, advance_to_qa  # noqa: F401  (fixtures below reuse the same setup)
from test_api import fake, fake_repo_fetch, fake_repo_permissions  # noqa: F401
from conftest import create_user

# pytest fixtures shared with test_api (referenced so linters see them as used)
_SHARED_FIXTURES = (fake, fake_repo_fetch, fake_repo_permissions)


@pytest.fixture
def carol():
    return client_for(create_user(3001, "carol3"))


def status(c, tid):
    r = c.get(f"/pipeline/{tid}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- restore after refresh

def test_refresh_at_review_gate_restores_pending_stage(fake, carol):
    tid = carol.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    carol.post(f"/pipeline/{tid}/resume", json={"approved": True})
    # "refresh": the page only knows the thread id from its URL and asks the server
    s = status(carol, tid)
    assert s["state"] == "awaiting_approval"
    assert s["pending"]["stage"] == "Design"
    assert s["pending"]["data"]["title"] == "Design"
    assert {d["kind"] for d in s["documents"]} == {"brd", "design"}


def test_interrupted_run_is_detected_and_resumes_from_checkpoint(fake, carol):
    tid = carol.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    # Simulate: BRD approved, server dies while the Design step runs.
    snap_before = g.graph.get_state({"configurable": {"thread_id": tid}})
    assert snap_before.next == ("approval_requirements",)
    g.graph.update_state({"configurable": {"thread_id": tid}}, {"approved": True, "feedback": None},
                         as_node="approval_requirements")
    update_workflow(tid, status="running", current_stage="Requirements (BRD)")

    s = status(carol, tid)
    assert s["state"] == "interrupted" and s["next"] == ["design"]
    assert [w["status"] for w in carol.get("/pipeline").json()["workflows"] if w["thread_id"] == tid] == ["interrupted"]

    calls_before = len(fake.calls["design"])
    r = carol.post(f"/pipeline/{tid}/resume", json={"approved": True})
    assert r.status_code == 200 and r.json()["stage"] == "Design"
    assert len(fake.calls["design"]) == calls_before + 1      # only the cut-off step re-ran
    assert len(fake.calls["brd"]) == 1                         # earlier work was NOT redone


def test_failed_step_reports_failed_and_is_resumable(fake, carol, monkeypatch):
    tid = carol.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    monkeypatch.setattr(g, "generate_design_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("llm timeout")))
    assert carol.post(f"/pipeline/{tid}/resume", json={"approved": True}).status_code == 502
    s = status(carol, tid)
    assert s["state"] == "failed" and "llm timeout" in s["workflow"]["last_error"]
    monkeypatch.setattr(g, "generate_design_json", fake.design)
    assert carol.post(f"/pipeline/{tid}/resume", json={"approved": True}).json()["stage"] == "Design"


def test_running_run_is_reported_and_double_execution_blocked(fake, carol):
    tid = carol.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    api._active_runs.add(tid)  # as if the step were executing in another request right now
    try:
        assert status(carol, tid)["state"] == "running"
        assert carol.post(f"/pipeline/{tid}/resume", json={"approved": True}).status_code == 409
    finally:
        api._active_runs.discard(tid)
    assert status(carol, tid)["state"] == "awaiting_approval"


@pytest.mark.parametrize("fake", [["pass"]], indirect=True)
def test_completed_run_restores_result_for_completion_page(fake, carol):
    tid = carol.post("/pipeline/start", json={"request_text": "x", "github_repo": "carol3/demo"}).json()["thread_id"]
    advance_to_qa(carol, tid)
    carol.post(f"/pipeline/{tid}/resume", json={"approved": True})
    s = status(carol, tid)
    assert s["state"] == "completed"
    assert s["result"]["deploy_output"]["pull_request"]["html_url"].endswith("/pull/1")
    assert s["result"]["qa_output"]["overall_status"] == "pass"
    assert {d["kind"] for d in s["documents"]} == {"brd", "design", "plan", "qa"}


def test_restore_is_owner_only(fake, carol):
    tid = carol.post("/pipeline/start", json={"request_text": "x"}).json()["thread_id"]
    dave = client_for(create_user(3002, "dave3"))
    assert dave.get(f"/pipeline/{tid}").status_code == 404


# ---------------------------------------------------------------- LangSmith

def _stub(text='{"title": "t", "n": 1}', tokens=(12, 5), cost=None):
    def call(system, contents, schema, timeout_s):
        return gw._Raw(text=text, model="stub-model", input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost)
    return call


SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "n": {"type": "integer"}},
          "required": ["title", "n"]}


def _capture(fn):
    """Runs fn under a parent traceable in LangSmith 'local' mode; returns the parent run tree."""
    captured = {}

    @traceable(name="test-parent")
    def parent():
        return fn()

    with tracing_context(enabled="local"):
        parent(langsmith_extra={"on_end": lambda run: captured.setdefault("run", run)})
    return captured["run"]


def test_llm_calls_are_traced_with_usage_and_model(monkeypatch):
    monkeypatch.setitem(gw.PROVIDERS, "openai", _stub(tokens=(12, 5)))
    monkeypatch.setattr(gw.config, "GEMINI_API_KEY", None)
    monkeypatch.setattr(gw.config, "OPENAI_API_KEY", "sk-test")
    gw.init_llm_db()
    token = gw.current_thread_id.set("thread-xyz")
    try:
        run = _capture(lambda: gw.generate_json(agent="requirements", prompt_version="brd-v1",
                                                system="SYS", contents="USER", schema=SCHEMA))
    finally:
        gw.current_thread_id.reset(token)
    [child] = run.child_runs
    assert child.run_type == "llm" and child.name == "requirements · openai"
    md = child.extra["metadata"]
    assert md["ls_provider"] == "openai" and md["agent"] == "requirements"
    assert md["prompt_version"] == "brd-v1" and md["thread_id"] == "thread-xyz"
    assert child.inputs["messages"][0] == {"role": "system", "content": "SYS"}
    assert child.outputs["usage_metadata"] == {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}
    assert "call" not in child.inputs  # the provider function object is not logged


def test_retries_and_fallback_each_appear_as_runs(monkeypatch):
    monkeypatch.setattr(gw.config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(gw.config, "OPENROUTER_API_KEY", "or-test")
    monkeypatch.setitem(gw.PROVIDERS, "gemini", _stub(text="not json"))
    monkeypatch.setitem(gw.PROVIDERS, "openai", lambda *a: (_ for _ in ()).throw(gw._NonRetryable("openai 401")))
    monkeypatch.setitem(gw.PROVIDERS, "openrouter", _stub(cost=0.0004))
    gw.init_llm_db()
    run = _capture(lambda: gw.generate_json(agent="design", prompt_version="v", system="s", contents="c", schema=SCHEMA))
    names = [(c.name, bool(c.error)) for c in run.child_runs]
    assert names == [("design · gemini", False)] * 3 + [("design · openai", True), ("design · openrouter", False)]
    assert run.child_runs[-1].outputs["usage_metadata"]["total_cost"] == 0.0004


def test_tracing_disabled_by_default_in_tests():
    import config
    assert config.LANGSMITH_TRACING is False
    assert "disabled" in config.langsmith_status()
