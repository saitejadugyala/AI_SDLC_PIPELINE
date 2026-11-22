"""Workflow metadata: which user owns which LangGraph thread, and where it is.

Kept separate from the LangGraph checkpoints (which hold the full state) so
ownership checks and history listings never need to deserialize workflow
state, and so access control doesn't depend on checkpoint internals.
"""

import sqlite3

import config

STATUSES = {"running", "awaiting_approval", "failed", "completed"}


def _conn():
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_workflows_db() -> None:
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            thread_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            request_text TEXT NOT NULL,
            github_repo TEXT,
            codebase_id TEXT,
            status TEXT NOT NULL,
            current_stage TEXT,
            last_error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflows_user ON workflows(user_id, created_at)")
    conn.commit()
    conn.close()


def create_workflow(thread_id: str, user_id: int, request_text: str, github_repo: str | None, codebase_id: str | None) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO workflows (thread_id, user_id, request_text, github_repo, codebase_id, status)
           VALUES (?, ?, ?, ?, ?, 'running')""",
        (thread_id, user_id, request_text, github_repo, codebase_id),
    )
    conn.commit()
    conn.close()


def update_workflow(thread_id: str, *, status: str, current_stage: str | None = None, last_error: str | None = None) -> None:
    assert status in STATUSES, status
    conn = _conn()
    conn.execute(
        """UPDATE workflows SET status = ?, current_stage = COALESCE(?, current_stage), last_error = ?,
           updated_at = datetime('now') WHERE thread_id = ?""",
        (status, current_stage, last_error, thread_id),
    )
    conn.commit()
    conn.close()


def set_workflow_repo(thread_id: str, github_repo: str) -> None:
    conn = _conn()
    conn.execute("UPDATE workflows SET github_repo = ?, updated_at = datetime('now') WHERE thread_id = ?",
                 (github_repo, thread_id))
    conn.commit()
    conn.close()


def get_workflow(thread_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM workflows WHERE thread_id = ?", (thread_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_workflows(user_id: int, limit: int = 50) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM workflows WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
