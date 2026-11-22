import json

from llm_gateway import generate_json
from path_safety import validate_repo_path, UnsafePathError

PLAN_PROMPT_VERSION = "plan-v1"
TESTCASE_PROMPT_VERSION = "testcases-v1"
CODE_PROMPT_VERSION = "code-v1"
DEV_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                    "implements_component": {"type": "string"},
                    "key_functions_or_classes": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["path", "purpose", "implements_component", "key_functions_or_classes"]
            }
        },
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "implementation_risks": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["title", "summary", "files", "dependencies", "implementation_risks", "open_questions"]
}
DEV_SYSTEM_PROMPT = """You are a senior software engineer creating an
IMPLEMENTATION PLAN — not code — from a technical design document.

Rules:
1. Do NOT write actual code. List files, their purpose, and what they'd
   contain at a high level (function/class names, not implementations).
2. Every file must reference which design component it implements, using
   the component's exact name from the design (implements_component field).
3. List real dependencies (libraries/packages) implied by the design's
   tech_stack_suggestions — don't invent unrelated ones.
4. implementation_risks are about building this specifically — integration
   complexity, tricky edge cases, things likely to go wrong during
   development. Not business or architecture risks (those live upstream).
5. If the design leaves something genuinely ambiguous for implementation
   (e.g. exact framework conventions, folder structure standards), raise
   it in open_questions rather than guessing.
"""
def generate_dev_plan_json(design_data: dict, feedback: str | None = None) -> dict:
    """Takes a validated Design dict, returns a validated implementation plan dict."""
    design_as_text = json.dumps(design_data, indent=2)
    contents = f"Here is the technical design:\n\n{design_as_text}"
    if feedback:
        contents += f"\n\nRevision feedback from reviewer: {feedback}\nPlease regenerate the plan, incorporating this feedback."

    return generate_json(
        agent="planning", prompt_version=PLAN_PROMPT_VERSION,
        system=DEV_SYSTEM_PROMPT, contents=contents, schema=DEV_PLAN_SCHEMA, timeout_s=60,
    )


TEST_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "given": {"type": "string"},
                    "when": {"type": "string"},
                    "then": {"type": "string"}
                },
                "required": ["id", "given", "when", "then"]
            }
        }
    },
    "required": ["test_cases"]
}

TEST_CASE_SYSTEM_PROMPT = """You are a QA engineer writing acceptance test
cases from an approved technical design and the original feature request.

Rules:
1. Write test cases as Given/When/Then, covering the core behavior described
   in the request — not implementation details, not edge cases nobody asked
   for. 3-6 test cases is normal; don't pad the list.
2. Each test case must be something a human could actually verify by using
   the running app — not something that requires reading the code.
3. Give each test case a short, unique id (e.g. tc_1, tc_2).
"""


def generate_test_cases(user_request: str, design_data: dict, feedback: str | None = None) -> list[dict]:
    """Generates acceptance test cases from the original request + approved
    design. These get passed to QA so its review checks the code against
    concrete, specific criteria instead of general "does this look right."

    Honest limitation: this is still an LLM reading the code and reasoning
    about whether it satisfies each test case — not actually executing
    anything. It's a real upgrade in rigor over unstructured review, but it
    is not the same guarantee as tests that actually ran and passed."""
    design_summary = json.dumps(design_data, indent=2)
    contents = f"Original feature request:\n{user_request}\n\nApproved design:\n{design_summary}"
    if feedback:
        contents += f"\n\nRevision feedback: {feedback}\nRegenerate test cases incorporating this."

    data = generate_json(
        agent="test_cases", prompt_version=TESTCASE_PROMPT_VERSION,
        system=TEST_CASE_SYSTEM_PROMPT, contents=contents, schema=TEST_CASE_SCHEMA, timeout_s=60,
    )
    return data["test_cases"]


CODE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "is_new_file": {"type": "boolean"},
        "updated_content": {"type": "string"}
    },
    "required": ["path", "is_new_file", "updated_content"]
}

CODE_SYSTEM_PROMPT = """You are a senior software engineer making a real,
minimal, correct code change to an existing file, based on an approved
implementation plan.

Rules:
1. Return the COMPLETE new content of the file — not a diff, not a snippet.
2. Preserve all existing functionality and style — only add/modify what the
   plan specifically calls for in this file.
3. If this is a genuinely new file (is_new_file=true), write it consistent
   with the existing codebase's conventions shown to you.
4. Do not explain your changes in prose — return only the file content.
"""


def generate_code_for_file(file_plan: dict, current_content: str | None, design_summary: str, feedback: str | None = None, skill_content: str | None = None) -> dict:
    """Generates real, complete file content for one file from the Dev plan.
    feedback (e.g. from a failed QA pass) and skill_content (project
    conventions from a matched Skill) are both optional and, when present,
    appended to the prompt."""
    is_new = current_content is None
    contents = (
        f"Design context:\n{design_summary}\n\n"
        f"File to change: {file_plan['path']}\n"
        f"Purpose of this change: {file_plan['purpose']}\n"
        f"Should contain: {', '.join(file_plan['key_functions_or_classes'])}\n\n"
    )
    if is_new:
        contents += "This is a NEW file — write it from scratch, consistent with the codebase's existing style."
    else:
        contents += f"CURRENT file content:\n```\n{current_content}\n```\n\nModify this file per the plan above. Return the complete updated file."

    if skill_content:
        contents += f"\n\nFollow these project conventions:\n{skill_content}"

    if feedback:
        contents += f"\n\nIMPORTANT — fix these issues (raised by QA and/or the human reviewer):\n{feedback}"

    return generate_json(
        agent="coding", prompt_version=CODE_PROMPT_VERSION,
        system=CODE_SYSTEM_PROMPT, contents=contents, schema=CODE_SCHEMA, timeout_s=90,
    )


def generate_all_code(dev_plan: dict, design_data: dict, codebase_id: str, feedback: str | None = None, skill_content: str | None = None, previous_codegen_output: list[dict] | None = None) -> list[dict]:
    """Runs generate_code_for_file for every file in the plan, pulling real
    current content from the DB where it exists. Returns a list of results,
    one per file — a partial failure on one file doesn't stop the others.
    feedback and skill_content, if provided, are passed to every file.

    previous_codegen_output, if provided (i.e. this is a QA-retry, not the
    first attempt), takes priority over the original DB content as the
    "current content" shown to the model — so a retry edits its own last
    attempt instead of blindly regenerating from the original file with only
    a text description of what went wrong. Without this, retries have no
    memory of what they just wrote and can reintroduce bugs they already fixed."""
    from db import get_codebase_file

    previous_by_path = {}
    if previous_codegen_output:
        for f in previous_codegen_output:
            if f.get("status") == "ok" and f.get("updated_content") is not None:
                previous_by_path[f["path"]] = f["updated_content"]

    design_summary = f"{design_data['title']}: {design_data['architecture_overview']}"
    results = []

    for file_plan in dev_plan["files"]:
        try:
            safe_path = validate_repo_path(file_plan["path"])
        except UnsafePathError as e:
            results.append({"path": file_plan["path"], "status": "failed", "error": f"Unsafe path rejected: {e}"})
            continue
        file_plan = {**file_plan, "path": safe_path}

        original_content = get_codebase_file(codebase_id, safe_path) if codebase_id else None
        current_content = previous_by_path.get(safe_path, original_content)
        try:
            code_result = generate_code_for_file(
                file_plan, current_content, design_summary,
                feedback=feedback, skill_content=skill_content
            )
            # The approved plan decides WHICH file is written, never the model:
            # path/status are set after the spread so the model's own "path"
            # field cannot redirect the write (audit bug C6).
            results.append({**code_result, "path": safe_path, "status": "ok",
                            "is_new_file": original_content is None})
        except Exception as e:
            results.append({"path": safe_path, "status": "failed", "error": str(e)})

    return results