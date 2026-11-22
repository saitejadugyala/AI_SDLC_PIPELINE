"""Per-run DOCX documents: BRD, Design, Implementation Plan, QA Report.

Each document is rendered when its stage reaches the human review gate, so
the reviewer can download exactly what they're approving. A regeneration
after rejection overwrites the draft. Files live in OUTPUT_DIR/<thread_id>/
and are downloaded under a clean, human-readable name derived from the
document title, e.g. "Self_Service_Password_Reset_-_BRD.docx".
"""

import json
import os
import re
from datetime import datetime, timezone

from docx import Document

import config
from agent import render_docx as render_brd_docx
from design_agent import render_design_docx

# kind -> (pipeline stage label, filename suffix, human label)
DOC_KINDS = {
    "brd": ("Requirements (BRD)", "BRD", "Requirements (BRD)"),
    "design": ("Design", "Design", "Technical Design"),
    "plan": ("Development", "Implementation_Plan", "Implementation Plan"),
    "qa": ("QA (pre-deploy)", "QA_Report", "QA Report"),
}
STAGE_TO_KIND = {stage: kind for kind, (stage, _, _) in DOC_KINDS.items()}


def clean_filename(title: str | None, suffix: str) -> str:
    """'Self-Service Password Reset!' + 'BRD' -> 'Self-Service_Password_Reset_-_BRD.docx'"""
    base = re.sub(r"[^A-Za-z0-9 _-]+", "", (title or "").strip())
    base = re.sub(r"\s+", "_", base).strip("_-")[:80] or "Pipeline"
    return f"{base}_-_{suffix}.docx"


def _run_dir(thread_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", thread_id):
        raise ValueError("invalid thread id")
    path = os.path.join(config.OUTPUT_DIR, thread_id)
    os.makedirs(path, exist_ok=True)
    return path


def doc_path(thread_id: str, kind: str) -> str:
    return os.path.join(_run_dir(thread_id), f"{kind}.docx")


def _meta_path(thread_id: str, kind: str) -> str:
    return os.path.join(_run_dir(thread_id), f"{kind}.json")


# ---------------------------------------------------------------- renderers for the new documents

def _bullets(doc, items):
    for item in items or []:
        doc.add_paragraph(str(item), style="List Bullet")


def _table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    for cell, h in zip(table.rows[0].cells, headers):
        cell.text = h
    for row in rows:
        for cell, value in zip(table.add_row().cells, row):
            cell.text = "" if value is None else str(value)


def render_plan_docx(plan: dict, output_path: str, test_cases: list | None = None):
    doc = Document()
    doc.add_heading(plan.get("title") or "Implementation Plan", level=0)
    doc.add_heading("Summary", level=1)
    doc.add_paragraph(plan.get("summary") or "")

    doc.add_heading("Files", level=1)
    _table(doc, ["Path", "Implements component", "Purpose", "Key functions / classes"], [
        (f.get("path"), f.get("implements_component"), f.get("purpose"),
         ", ".join(f.get("key_functions_or_classes") or []))
        for f in plan.get("files") or []
    ])
    doc.add_heading("Dependencies", level=1)
    _bullets(doc, plan.get("dependencies"))
    doc.add_heading("Implementation Risks", level=1)
    _bullets(doc, plan.get("implementation_risks"))
    if test_cases:
        doc.add_heading("Acceptance Test Cases", level=1)
        _table(doc, ["ID", "Given", "When", "Then"],
               [(t.get("id"), t.get("given"), t.get("when"), t.get("then")) for t in test_cases])
    doc.add_heading("Open Questions", level=1)
    _bullets(doc, plan.get("open_questions"))
    doc.save(output_path)


def render_qa_docx(qa: dict, output_path: str, title: str | None = None, deploy_target: str | None = None):
    doc = Document()
    doc.add_heading(f"QA Report — {title}" if title else "QA Report", level=0)

    passed = qa.get("overall_status") == "pass"
    doc.add_heading("Verdict", level=1)
    doc.add_paragraph(("PASS" if passed else "FAIL") + f" — {len(qa.get('issues') or [])} issue(s) found")
    _table(doc, ["Item", "Value"], [
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
        ("Automatic fix attempts used", f"{qa.get('retries_used', 0)} of {qa.get('max_retries', config.MAX_QA_RETRIES)}"),
        ("Files checked", len(qa.get("files_checked") or [])),
        ("Deploy target", deploy_target or "not selected"),
    ])

    execution = qa.get("execution") or {}
    doc.add_heading("What was checked", level=1)
    _bullets(doc, [
        "Syntax: Python (ast.parse) and Jinja2/HTML templates",
        "Static analysis: pyflakes (undefined names are critical)",
        "LLM cross-file review against the approved design",
        "Acceptance test cases (LLM-judged, see below)",
    ])
    if not execution.get("performed", False):
        doc.add_paragraph(execution.get("note") or "Generated code was not executed.", style="Intense Quote")

    doc.add_heading("Issues", level=1)
    issues = qa.get("issues") or []
    if issues:
        _table(doc, ["File", "Severity", "Description"],
               [(i.get("file"), i.get("severity"), i.get("description")) for i in issues])
    else:
        doc.add_paragraph("No issues found.")

    doc.add_heading("Acceptance Test Cases", level=1)
    tcs = qa.get("test_case_results") or []
    if tcs:
        _table(doc, ["Test case", "Satisfied", "Notes"],
               [(t.get("test_case_id"), "yes" if t.get("satisfied") else "NO", t.get("notes")) for t in tcs])
    else:
        doc.add_paragraph("No acceptance test cases were evaluated.")

    doc.add_heading("Files", level=1)
    files = qa.get("generated_files") or []
    if files:
        _table(doc, ["Path", "Change", "Status"], [
            (f.get("path"), "new file" if f.get("is_new_file") else "modified", f.get("status"))
            for f in files
        ])
    else:
        _bullets(doc, qa.get("files_checked"))
    doc.save(output_path)


# ---------------------------------------------------------------- public API

def write_document(thread_id: str, kind: str, data: dict, *, extra: dict | None = None) -> dict:
    """Renders one document for a run and returns its descriptor."""
    extra = extra or {}
    path = doc_path(thread_id, kind)
    if kind == "brd":
        render_brd_docx(data, path)
        title = data.get("title")
    elif kind == "design":
        render_design_docx(data, path)
        title = data.get("title")
    elif kind == "plan":
        render_plan_docx(data, path, test_cases=extra.get("test_cases"))
        title = data.get("title")
    elif kind == "qa":
        title = extra.get("title")
        render_qa_docx(data, path, title=title, deploy_target=extra.get("deploy_target"))
    else:
        raise ValueError(f"unknown document kind: {kind}")

    meta = {"kind": kind, "label": DOC_KINDS[kind][2], "title": title,
            "filename": clean_filename(title, DOC_KINDS[kind][1]),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(_meta_path(thread_id, kind), "w") as f:
        json.dump(meta, f)
    return describe(thread_id, meta)


def describe(thread_id: str, meta: dict) -> dict:
    return {**meta, "url": f"/pipeline/{thread_id}/documents/{meta['kind']}"}


def get_document(thread_id: str, kind: str) -> tuple[str, str] | None:
    """(path, download filename) or None."""
    if kind not in DOC_KINDS:
        return None
    path, meta_path = doc_path(thread_id, kind), _meta_path(thread_id, kind)
    if not (os.path.isfile(path) and os.path.isfile(meta_path)):
        return None
    with open(meta_path) as f:
        return path, json.load(f)["filename"]


def list_documents(thread_id: str) -> list[dict]:
    out = []
    for kind in DOC_KINDS:
        meta_path = _meta_path(thread_id, kind)
        if os.path.isfile(meta_path) and os.path.isfile(doc_path(thread_id, kind)):
            with open(meta_path) as f:
                out.append(describe(thread_id, json.load(f)))
    return out


def write_for_stage(thread_id: str, stage: str, data: dict, state: dict) -> dict | None:
    """Called when the workflow pauses at `stage`: render that stage's document."""
    kind = STAGE_TO_KIND.get(stage)
    if kind is None:
        return None
    if kind == "plan":
        return write_document(thread_id, kind, data, extra={"test_cases": state.get("test_cases")})
    if kind == "qa":
        title = (state.get("brd") or {}).get("title") or (state.get("design") or {}).get("title")
        return write_document(thread_id, kind, data, extra={"title": title, "deploy_target": state.get("github_repo")})
    return write_document(thread_id, kind, data)
