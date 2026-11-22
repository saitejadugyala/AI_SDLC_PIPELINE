"""Path validation and the Coding agent's handling of model-returned paths.
LLM is MOCKED."""

import pytest

import dev_agent
from path_safety import validate_repo_path, UnsafePathError


@pytest.mark.parametrize("path,expected", [
    ("app.py", "app.py"),
    ("./src/app.py", "src/app.py"),
    ("templates/index.html", "templates/index.html"),
    (".github/ISSUE_TEMPLATE/bug.md", ".github/ISSUE_TEMPLATE/bug.md"),
    (".env.example", ".env.example"),
])
def test_valid_paths(path, expected):
    assert validate_repo_path(path) == expected


@pytest.mark.parametrize("path", [
    "", "   ", "/etc/passwd", "C:\\x", "../outside.py", "a/../../b", "src\\win.py",
    ".git/config", "sub/.git/hooks/pre-commit", ".github/workflows/ci.yml",
    ".env", "config/.env.production", "a\x00b",
])
def test_unsafe_paths(path):
    with pytest.raises(UnsafePathError):
        validate_repo_path(path)


def test_model_cannot_redirect_the_written_path(monkeypatch):
    """Regression for audit C6: the model's own 'path' field used to override the plan."""
    monkeypatch.setattr(dev_agent, "generate_code_for_file", lambda *a, **k: {
        "path": ".github/workflows/evil.yml", "is_new_file": True, "updated_content": "x = 1\n"})
    plan = {"files": [{"path": "app.py", "purpose": "p", "implements_component": "C", "key_functions_or_classes": []}]}
    out = dev_agent.generate_all_code(plan, {"title": "t", "architecture_overview": "o"}, None)
    assert out == [{"path": "app.py", "status": "ok", "is_new_file": True, "updated_content": "x = 1\n"}]


def test_unsafe_planned_path_is_rejected_without_llm_call(monkeypatch):
    called = []
    monkeypatch.setattr(dev_agent, "generate_code_for_file", lambda *a, **k: called.append(1))
    plan = {"files": [{"path": "../../etc/x", "purpose": "p", "implements_component": "C", "key_functions_or_classes": []}]}
    out = dev_agent.generate_all_code(plan, {"title": "t", "architecture_overview": "o"}, None)
    assert out[0]["status"] == "failed" and "Unsafe path" in out[0]["error"]
    assert called == []


def test_retry_edits_previous_attempt_but_keeps_original_new_file_flag(monkeypatch):
    seen = {}

    def gen(file_plan, current_content, design_summary, feedback=None, skill_content=None):
        seen.update(current=current_content, feedback=feedback)
        return {"path": file_plan["path"], "is_new_file": False, "updated_content": "v2"}
    monkeypatch.setattr(dev_agent, "generate_code_for_file", gen)
    plan = {"files": [{"path": "new.py", "purpose": "p", "implements_component": "C", "key_functions_or_classes": []}]}
    prev = [{"path": "new.py", "status": "ok", "is_new_file": True, "updated_content": "v1"}]
    out = dev_agent.generate_all_code(plan, {"title": "t", "architecture_overview": "o"}, None,
                                      feedback="fix it", previous_codegen_output=prev)
    assert seen == {"current": "v1", "feedback": "fix it"}
    assert out[0]["is_new_file"] is True and out[0]["updated_content"] == "v2"


def test_qa_static_layers_without_llm(monkeypatch):
    """Syntax + pyflakes are real; only the LLM semantic layer is MOCKED."""
    import qa_agent
    monkeypatch.setattr(qa_agent, "generate_semantic_review",
                        lambda files, test_cases=None: {"overall_status": "pass", "issues": [], "test_case_results": []})
    out = qa_agent.run_qa([
        {"path": "a.py", "status": "ok", "updated_content": "def f(:\n"},
        {"path": "b.py", "status": "ok", "updated_content": "x = undefined_name\n"},
        {"path": "t.html", "status": "ok", "updated_content": "{% if x %}"},
        {"path": "ok.py", "status": "ok", "updated_content": "import os\nprint(os.sep)\n"},
    ])
    assert out["overall_status"] == "fail"
    by_file = {i["file"]: i["description"] for i in out["issues"]}
    assert "Python syntax error" in by_file["a.py"]
    assert "UndefinedName" in by_file["b.py"]
    assert "Jinja2 syntax error" in by_file["t.html"]
    assert "ok.py" not in by_file
