"""LangGraph workflow: handoffs, approval/rejection paths, QA retry policy,
the server-side deploy gate, and persistence across a restart.
All agents and the GitHub deploy are MOCKED."""

import sqlite3
import typing
import uuid

import pytest
from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

import config
import graph as g
import qa_agent
from conftest import create_user

STATE_KEYS = set(typing.get_type_hints(g.PipelineState))


class Fake:
    """Records every agent call. qa_results is consumed one per QA run."""

    def __init__(self, qa_results):
        self.qa_results = list(qa_results)
        self.calls = {"brd": [], "design": [], "plan": [], "codegen": [], "qa": 0, "deploy": []}
        self.returned_keys = []

    def brd(self, req, codebase_context=None):
        self.calls["brd"].append(req)
        return {"title": "BRD", "executive_summary": "s", "business_objectives": ["o"], "in_scope": ["i"],
                "out_of_scope": [], "functional_requirements": [{"id": "FR-1", "description": "d", "priority": "Must"}],
                "assumptions": [], "open_questions": []}

    def design(self, brd, feedback=None, codebase_context=None):
        self.calls["design"].append(feedback)
        return {"title": "Design", "architecture_overview": "o",
                "components": [{"name": "C1", "responsibility": "r", "addresses_requirement_ids": ["FR-1"]}],
                "data_model": [], "tech_stack_suggestions": [], "design_risks": [], "open_questions": []}

    def plan(self, design, feedback=None):
        self.calls["plan"].append(feedback)
        return {"title": "Plan", "summary": "s", "dependencies": [], "implementation_risks": [], "open_questions": [],
                "files": [{"path": "app.py", "implements_component": "C1", "purpose": "p", "key_functions_or_classes": ["f"]}]}

    def codegen(self, plan, design, cid, feedback=None, skill_content=None, previous_codegen_output=None):
        self.calls["codegen"].append(feedback)
        return [{"path": "app.py", "status": "ok", "is_new_file": True, "updated_content": f"v = {len(self.calls['codegen'])}\n"}]

    def run_qa(self, codegen_output, test_cases=None):
        self.calls["qa"] += 1
        status = self.qa_results.pop(0) if self.qa_results else "pass"
        issues = [] if status == "pass" else [{"file": "app.py", "severity": "critical", "description": "bug"}]
        return {"overall_status": status, "issues": issues, "test_case_results": [], "files_checked": ["app.py"]}

    async def deploy(self, codegen_output, token, repo, title=None, body_intro=None):
        self.calls["deploy"].append((token, repo))
        self.last_pr = {"title": title, "body_intro": body_intro}
        return {"status": "success", "branch": "pipeline-x", "files_written": ["app.py"], "files_failed": [],
                "pull_request": {"html_url": f"https://github.com/{repo}/pull/1"}}


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


def start(user_id=None, repo=None):
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = g.graph.invoke({
        "user_request": "add a feature", "codebase_context": None, "codebase_id": None,
        "brd": None, "design": None, "dev_output": None, "test_cases": None, "codegen_output": None,
        "qa_output": None, "qa_retry_count": 0, "qa_feedback": None, "qa_auto_retry": None,
        "deploy_output": None, "approved": None, "feedback": None, "user_id": user_id, "github_repo": repo,
    }, config=cfg)
    return cfg, result


def stage(result):
    return result["__interrupt__"][0].value["stage"] if "__interrupt__" in result else None


def approve(cfg, ok=True, feedback=None):
    return g.graph.invoke(Command(resume={"approved": ok, "feedback": feedback}), config=cfg)


def to_qa_gate(fake):
    cfg, r = start(user_id=create_user(1001, "alice"), repo="alice/demo")
    seen = [stage(r)]
    for _ in range(3):
        r = approve(cfg)
        seen.append(stage(r))
    return cfg, r, seen


# ---------------------------------------------------------------- tests

def test_every_node_update_key_is_declared_in_state(fake):
    """Regression for audit C1: LangGraph drops undeclared keys silently."""
    state = {k: None for k in STATE_KEYS}
    state.update(user_request="r", brd=fake.brd("r"), design=fake.design(None), dev_output=fake.plan(None),
                 codegen_output=fake.codegen(None, None, None), qa_retry_count=0)
    for node in (g.requirements_node, g.design_node, g.dev_node, g.codegen_node, g.qa_node):
        assert set(node(state)) <= STATE_KEYS, node.__name__


@pytest.mark.parametrize("fake", [["pass"]], indirect=True)
def test_happy_path_stages_and_no_pre_qa_code_gate(fake):
    cfg, r, seen = to_qa_gate(fake)
    # Decision 2: no "Generated Code" gate; code is reviewed at the QA gate.
    assert seen == ["Requirements (BRD)", "Design", "Development", "QA (pre-deploy)"]
    data = r["__interrupt__"][0].value["data"]
    assert data["overall_status"] == "pass"
    assert data["generated_files"][0]["path"] == "app.py"
    assert data["execution"]["performed"] is False  # honest: nothing executed
    final = approve(cfg)
    assert "__interrupt__" not in final
    assert final["deploy_output"]["status"] == "success"
    assert fake.calls["deploy"] == [("gho_fake_alice", "alice/demo")]


@pytest.mark.parametrize("fake", [["fail", "fail", "fail", "fail", "fail"]], indirect=True)
def test_failing_qa_auto_retries_exactly_three_times(fake):
    cfg, r, seen = to_qa_gate(fake)
    assert seen[-1] == "QA (pre-deploy)"
    assert len(fake.calls["codegen"]) == 1 + config.MAX_QA_RETRIES == 4
    assert fake.calls["qa"] == 4
    assert fake.calls["codegen"][0] is None
    assert all("QA found issues" in fb for fb in fake.calls["codegen"][1:])
    data = r["__interrupt__"][0].value["data"]
    assert data["retries_used"] == 3 and data["overall_status"] == "fail"


@pytest.mark.parametrize("fake", [["fail", "pass"]], indirect=True)
def test_qa_passes_after_one_retry(fake):
    cfg, r, _ = to_qa_gate(fake)
    data = r["__interrupt__"][0].value["data"]
    assert data["overall_status"] == "pass" and data["retries_used"] == 1
    assert len(fake.calls["codegen"]) == 2
    assert g.graph.get_state(cfg).values["qa_feedback"] is None  # cleared on pass


@pytest.mark.parametrize("fake", [["fail"] * 4], indirect=True)
def test_approving_failed_qa_never_deploys(fake):
    """Regression for audit C2: the graph itself refuses, not just the UI."""
    cfg, r, _ = to_qa_gate(fake)
    r = approve(cfg, ok=True)
    assert stage(r) == "QA (pre-deploy)"
    assert fake.calls["deploy"] == []
    assert g.graph.get_state(cfg).values.get("deploy_output") is None


@pytest.mark.parametrize("fake", [["fail"] * 4 + ["pass"]], indirect=True)
def test_human_rejection_feedback_at_qa_reaches_coding_agent(fake):
    """Regression for audit C9-class bug: human feedback must reach codegen."""
    cfg, r, _ = to_qa_gate(fake)
    r = approve(cfg, ok=False, feedback="Rename v to value")
    assert fake.calls["codegen"][-1] == "Rename v to value"
    assert stage(r) == "QA (pre-deploy)"
    assert r["__interrupt__"][0].value["data"]["overall_status"] == "pass"


def test_rejection_with_feedback_regenerates_each_document_stage(fake):
    cfg, r = start()
    r = approve(cfg, ok=False, feedback="add audit logging")
    assert stage(r) == "Requirements (BRD)"
    assert "add audit logging" in fake.calls["brd"][-1]
    r = approve(cfg)
    assert stage(r) == "Design"
    r = approve(cfg, ok=False, feedback="use postgres")
    assert stage(r) == "Design" and fake.calls["design"][-1] == "use postgres"
    r = approve(cfg)
    r = approve(cfg, ok=False, feedback="split files")
    assert stage(r) == "Development" and fake.calls["plan"][-1] == "split files"


@pytest.mark.parametrize("fake", [["pass"]], indirect=True)
def test_deploy_without_login_fails_cleanly(fake):
    cfg, r = start()
    for _ in range(3):
        r = approve(cfg)
    assert stage(r) == "QA (pre-deploy)"
    final = approve(cfg)
    assert final["deploy_output"]["status"] == "failed"
    assert fake.calls["deploy"] == []


def test_workflow_state_survives_restart(fake):
    """A brand-new checkpointer on the same file (= app restart) still sees
    the paused run and can resume it."""
    cfg, r = start()
    r = approve(cfg)
    assert stage(r) == "Design"

    conn = sqlite3.connect(config.CHECKPOINT_DB_PATH, check_same_thread=False)
    restarted = g.graph_builder.compile(checkpointer=SqliteSaver(conn))
    snap = restarted.get_state(cfg)
    assert snap.next == ("approval_design",)
    assert snap.values["brd"]["title"] == "BRD"
    r2 = restarted.invoke(Command(resume={"approved": True, "feedback": None}), config=cfg)
    assert stage(r2) == "Development"
    conn.close()


def test_llm_calls_are_traced_to_thread(monkeypatch):
    import llm_gateway
    seen = []
    monkeypatch.setattr(g, "generate_brd_json", lambda req, codebase_context=None: seen.append(llm_gateway.current_thread_id.get()) or {"title": "x"})
    cfg, _ = start()
    assert seen == [cfg["configurable"]["thread_id"]]
