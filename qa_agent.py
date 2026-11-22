import ast
from jinja2 import Environment, TemplateSyntaxError
from pyflakes.checker import Checker

from llm_gateway import generate_json

PROMPT_VERSION = "qa-review-v1"

QA_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_status": {"type": "string", "enum": ["pass", "fail"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "severity": {"type": "string", "enum": ["critical", "warning"]},
                    "description": {"type": "string"}
                },
                "required": ["file", "severity", "description"]
            }
        },
        "test_case_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test_case_id": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "notes": {"type": "string"}
                },
                "required": ["test_case_id", "satisfied", "notes"]
            }
        }
    },
    "required": ["overall_status", "issues", "test_case_results"]
}

QA_SYSTEM_PROMPT = """You are a senior code reviewer checking a set of
generated file changes BEFORE they are pushed as a pull request.

You will be given the full content of every changed file together. Your job
is specifically to catch cross-file mismatches that a single-file review
would miss — for example:

- A template or route referencing a model field name that doesn't actually
  exist in the model file (e.g. `todo.priority` when the model defines
  `is_high_priority`).
- A route name or URL referenced in one file that doesn't match what's
  defined in another.
- Logic that assumes a field, table, or function exists that was never
  actually defined anywhere in the provided files.

Do NOT flag pure style preferences. Do NOT re-check syntax — that has
already been verified separately. Focus only on real correctness issues
that would cause a bug or crash at runtime because of a mismatch between
files.

A "critical" issue means: this will cause visibly wrong or broken behavior.
A "warning" means: this looks risky or worth a human double-checking, but
might be intentional.

If you are given acceptance test cases, check the code against each one
specifically and report a result for every single test case id given to
you — do not skip any, and do not invent test cases that weren't given to
you. A test case is "satisfied" only if the code, as written, would produce
that behavior — reason about it concretely by tracing the actual code, not
by assuming intent. If no test cases are given, return an empty
test_case_results list.

If everything is consistent, return overall_status "pass" with an empty
issues list.
"""


def check_syntax(path: str, content: str) -> dict:
    """Runs a real parser against the file — no LLM involved. Either it
    parses or it doesn't."""
    if path.endswith(".py"):
        try:
            ast.parse(content)
            return {"valid": True, "error": None}
        except SyntaxError as e:
            return {"valid": False, "error": f"Python syntax error: {e}"}

    if path.endswith(".html"):
        try:
            Environment().parse(content)
            return {"valid": True, "error": None}
        except TemplateSyntaxError as e:
            return {"valid": False, "error": f"Jinja2 syntax error: {e}"}

    # Extensions we don't have a real parser for — skip, don't fail
    return {"valid": True, "error": None}


# pyflakes message classes that indicate a real runtime bug, not just style.
# Kept as an explicit allowlist rather than "everything is critical" because
# things like UnusedImport are real findings but shouldn't block a deploy —
# treating them the same as UndefinedName would make this noisy enough that
# people start ignoring it, which defeats the point.
LINT_CRITICAL_TYPES = {"UndefinedName", "UndefinedLocal", "UndefinedExport", "DuplicateArgument"}


def check_lint(path: str, content: str) -> list[dict]:
    """Runs pyflakes against Python files — a real static analyzer, not an
    LLM guess. Only called on files that already passed check_syntax, since
    pyflakes needs a valid AST to walk. Returns a list of issue dicts in the
    same shape QA already uses, so they merge into the existing issues list."""
    if not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []  # already caught and reported by check_syntax — don't double-report

    checker = Checker(tree, filename=path)
    issues = []
    for msg in checker.messages:
        cls_name = msg.__class__.__name__
        severity = "critical" if cls_name in LINT_CRITICAL_TYPES else "warning"
        issues.append({
            "file": path,
            "severity": severity,
            "description": f"pyflakes ({cls_name}): {msg.message % msg.message_args}"
        })
    return issues


def run_syntax_checks(codegen_output: list[dict]) -> list[dict]:
    """Runs check_syntax on every successfully-generated file. Returns the
    codegen_output list with a 'syntax' key added to each item."""
    results = []
    for file_result in codegen_output:
        if file_result.get("status") != "ok":
            results.append(file_result)
            continue
        syntax = check_syntax(file_result["path"], file_result["updated_content"])
        results.append({**file_result, "syntax": syntax})
    return results


def generate_semantic_review(files_for_review: list[dict], test_cases: list[dict] | None = None) -> dict:
    """Sends all syntax-valid files together to the LLM for cross-file
    consistency review, plus — if provided — checks against explicit
    acceptance test cases instead of just general "does this look right.\""""
    if not files_for_review:
        return {"overall_status": "pass", "issues": [], "test_case_results": []}

    files_text = "\n\n".join(
        f"--- {f['path']} ---\n{f['updated_content']}"
        for f in files_for_review
    )
    contents = f"Here are the changed files:\n\n{files_text}"

    if test_cases:
        tc_text = "\n".join(
            f"- [{tc['id']}] Given {tc['given']}, when {tc['when']}, then {tc['then']}"
            for tc in test_cases
        )
        contents += f"\n\nAcceptance test cases to check against:\n{tc_text}"

    return generate_json(
        agent="qa_review", prompt_version=PROMPT_VERSION,
        system=QA_SYSTEM_PROMPT, contents=contents, schema=QA_SCHEMA, timeout_s=90,
    )


def run_qa(codegen_output: list[dict], test_cases: list[dict] | None = None) -> dict:
    """Full QA pass:
    1. Syntax check every file (free, deterministic — ast.parse / Jinja2 parser)
    2. Lint check every valid Python file (free, deterministic — real pyflakes)
    3. Send valid files + any test cases to the LLM for semantic review

    Note on test_cases: this makes QA check against concrete, specific
    criteria instead of vague "looks fine" review — a real improvement in
    rigor. It is still an LLM reading code and reasoning about it, not an
    execution engine actually running the test cases against a live app."""
    checked = run_syntax_checks(codegen_output)

    syntax_issues = [
        {"file": f["path"], "severity": "critical", "description": f["syntax"]["error"]}
        for f in checked
        if f.get("syntax") and not f["syntax"]["valid"]
    ]

    valid_files = [
        f for f in checked
        if f.get("status") == "ok" and f.get("syntax", {}).get("valid", True)
    ]

    lint_issues = []
    for f in valid_files:
        lint_issues.extend(check_lint(f["path"], f["updated_content"]))

    semantic_result = generate_semantic_review(valid_files, test_cases=test_cases)

    test_case_issues = [
        {
            "file": "acceptance test cases",
            "severity": "critical",
            "description": f"[{r['test_case_id']}] not satisfied: {r['notes']}"
        }
        for r in semantic_result.get("test_case_results", [])
        if not r.get("satisfied", True)
    ]

    all_issues = syntax_issues + lint_issues + semantic_result.get("issues", []) + test_case_issues
    overall_status = "fail" if any(i["severity"] == "critical" for i in all_issues) else "pass"

    return {
        "overall_status": overall_status,
        "issues": all_issues,
        "test_case_results": semantic_result.get("test_case_results", []),
        "files_checked": [f["path"] for f in checked if f.get("status") == "ok"]
    }