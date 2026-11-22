# Codebase Audit: AI_SDLC_PIPELINE

- **Date:** 2026-11-21
- **Commit audited:** `0719a2b` (main, 28 commits)
- **Scope:** Phase 1 only. Inspection and verification, with no application code changed.
- **Method:** I read every source file in full. Then I ran syntax, lint and dependency checks in a fresh Python 3.12 venv, plus behavioural probes against the real modules. The probes used **mocked** LLM calls and made no GitHub network calls.

Every finding is tagged with how it was checked:

- **[VERIFIED]**: reproduced by running code.
- **[CODE]**: confirmed by reading the code, but not executed.
- **[LIKELY]**: inferred, and still needs confirmation.

> **Update (Phases 2–3a):** C1–C6, C8, C9, C10, C11 and C12 are fixed and
> covered by regression tests, and C14 was retracted. See
> [PROGRESS.md](PROGRESS.md) for the fix-by-fix status. The findings below
> are kept as originally written.

---

## 0. Executive summary

The repository is a small, readable prototype of about 2,900 lines of Python and a 1,700-line single-page HTML UI. It is a real LangGraph pipeline with these parts:

- A typed state graph.
- Human-in-the-loop interrupts.
- SQLite checkpoints.
- Gemini structured outputs.
- GitHub OAuth.
- A purpose-built FastMCP GitHub server that opens PRs. There is no merge capability anywhere in the code.

The foundation is sound and worth keeping. However, several headline claims in the README are **not true at runtime**:

1. **QA automatic retries never happen.** The routing flag is silently dropped by LangGraph. [VERIFIED]
2. **A failed QA can be approved and deployed.** The QA gate is enforced only by hiding a button in the UI. [VERIFIED]
3. **If `SESSION_SECRET_KEY` is unset, any user's session can be forged.** The app signs cookies with the literal string `"None"`. [VERIFIED]
4. **Pipeline threads have no owner check.** Anyone who knows a `thread_id` can approve that thread's deploy, which runs with the owner's GitHub token. [CODE]
5. **The "Import from GitHub URL" UI calls an endpoint that doesn't exist.** It returns 404. [VERIFIED]
6. **QA does not execute any code.** "Test cases" are judged by an LLM reading the code. There is no sandbox.
7. **RAG is a standalone chat widget and is not used by any agent.** It is dense-only, with no BM25 and no reranking, and the AST chunker misses decorated Python functions and exported JS/TS declarations. [VERIFIED]

Against the target architecture in the brief, about 40% exists in some form. The largest gaps are:

- Sandboxed test execution.
- A multi-layer QA.
- An LLM gateway with token and cost tracking.
- Hybrid RAG connected to agents.
- Per-user workflow ownership and history.
- Audit logging.
- A real automated test suite. The current `test_*.py` files are manual scripts with no assertions.

---

## 1. Folder structure

```
AI_SDLC_PIPELINE/
├── README.md                  Project overview (some claims inaccurate, see §7)
├── ARCHITECTURE_RAG.md        Target RAG design (pgvector, BM25, rerank, KG) — mostly NOT built
├── requirements.txt           107 pinned packages (full freeze) + unpinned pyflakes
├── .gitignore                 .env, venv, app.db, checkpoints.db, outputs/
│
├── api.py                     FastAPI app: all HTTP endpoints, serves static/index.html
├── auth.py                    GitHub OAuth router, users table, session helpers
├── db.py                      SQLite helpers: codebase_files, codebase_context, brd_store
├── graph.py                   LangGraph StateGraph: nodes, approval gates, routing, deploy MCP client
│
├── agent.py                   Requirements Agent (BRD JSON + DOCX)
├── design_agent.py            Design/Architect Agent (design JSON + DOCX)
├── dev_agent.py               Planning Agent + test-case generator + Coding Agent
├── qa_agent.py                QA Agent: ast/Jinja2 syntax, pyflakes, LLM cross-file review
├── codebase_context.py        Zip extract (zip-slip safe) + folder scan
├── skills.py                  Deterministic "skill" detector (Flask only) → prompt conventions
├── skills/flask-conventions/SKILL.md
├── eval.py                    BRD-only heuristic eval (needs live Gemini)
│
├── rag/
│   ├── __init__.py
│   ├── ast_chunker.py         tree-sitter chunking (11 grammars) + char-window fallback
│   ├── github_import.py       GitHub tree/blob fetch → codebase context dict
│   └── rag_ingest.py          Embed (gemini-embedding-001), store JSON vectors in SQLite, cosine search, answer
│
├── mcp_server/
│   └── github_mcp_server.py   FastMCP stdio server: create_branch, write_file, open_pull_request
│
├── static/index.html          Entire frontend (vanilla JS, no build step)
├── docs/                      pipeline.svg, internal-flow.svg (+ this audit)
│
└── test_codegen.py, test_deploy_standalone.py, test_qa_semantic.py,
    test_qa_standalone.py, test_skills.py   Manual scripts — no asserts, not pytest
```

Runtime artefacts are created **relative to the current working directory** [VERIFIED]: `app.db`, `checkpoints.db`, `outputs/*.docx`, plus temp dirs under `/tmp/codebase_upload_*` that are never cleaned up.

Related but separate projects exist on the Desktop: `resume/aisdlc`, `Java/sdlc-pipeline-agent` and `Java/aisdlc-platform`. They were **not** audited and are not part of this repo.

---

## 2. Technology stack

| Layer | Technology | Notes |
|---|---|---|
| API | FastAPI 0.141, Uvicorn, Starlette `SessionMiddleware` | All endpoints sync; long LLM chains run inside HTTP requests |
| Orchestration | LangGraph 1.2.10, `SqliteSaver` | `StateGraph`, `interrupt()`, `Command(resume=…)` |
| LLM | `google-genai` 2.16, model `gemini-3.5-flash` (hardcoded ×6) | Structured JSON via `response_schema` |
| Embeddings | `gemini-embedding-001` | |
| Persistence | SQLite (`app.db`, `checkpoints.db`) | |
| RAG | tree-sitter 0.26 + tree-sitter-language-pack | Vectors as JSON text, cosine in pure Python |
| MCP | `fastmcp` 3.4.6 (stdio) | Server spawned per deploy as subprocess |
| Docs | `python-docx` | BRD + Design only |
| Auth | GitHub OAuth (manual flow with `requests`) | `Authlib` installed but unused |
| Frontend | Single static HTML/JS file | Not Streamlit |
| Installed but unused | `anthropic`, `sqlite-vec`, `langsmith`, `Authlib` | |

---

## 3. Architecture and agent flow (as built)

### 3.1 LangGraph topology (`graph.py:241-290`)

```
START → requirements → approval_requirements ─(reject)→ requirements
                                  └(approve)→ design → approval_design ─(reject)→ design
                                                              └(approve)→ dev → approval_dev ─(reject)→ dev
                                                                                     └(approve)→ codegen → approval_codegen ─(reject)→ codegen
                                                                                                                  └(approve)→ qa
qa ─(qa_auto_retry)→ codegen          ← NEVER TAKEN (bug C1)
   └→ approval_qa ─(reject)→ codegen
                  └(approve)→ deploy → END     ← reachable even when QA = fail (bug C2)
```

Note that there is an extra human gate (`approval_codegen`) *before* QA that is not in the target design. This makes the human review untested code.

### 3.2 End-to-end trace: user input → PR

1. **UI** `POST /pipeline/start` sends `{request_text, codebase_id?, github_repo?}`. Login is optional.
2. **api.py:128** creates a new `thread_id`. If a zip was uploaded, it loads the codebase from `app.db`. If a repo was picked, it fetches up to 150 files over the GitHub API and stores them under a new `codebase_id`.
3. **`graph.invoke`** runs `requirements_node` → `generate_brd_json`. The BRD sees only the first 50 file paths. The graph then hits `interrupt()`, and the HTTP response returns `{status: paused, stage, data}`.
4. **UI** `POST /pipeline/{thread_id}/resume` sends `{approved, feedback}`. A reject loops back to the same node with `feedback`.
5. **design_node** receives the BRD JSON plus the first 50 paths.
6. **dev_node** receives the design JSON. It runs a component traceability check, but only *prints* the result. It also generates Given/When/Then test cases once.
7. **codegen_node** runs once per planned file. It gets the plan entry, design title and overview only, the current file content from DB (or its own previous attempt on a retry), and the Flask skill text if detected. Each file is generated **independently**: no file sees the others.
8. **approval_codegen** lets the human review the code.
9. **qa_node** runs `ast.parse`/Jinja2 → pyflakes → one LLM review of all files plus the test cases. It then sets `qa_auto_retry`, which is dropped (C1).
10. **approval_qa** lets the human approve or reject. Approve works even if `overall_status == "fail"`.
11. **deploy_node** loads the user's OAuth token from `users`. It spawns `python mcp_server/github_mcp_server.py` with `GITHUB_PAT`/`GITHUB_REPO` env, then calls `create_branch` (off hardcoded `main`), `write_file` once per file (one commit each), and `open_pull_request` (into `main`).
12. **resume** renders the design DOCX and returns the final state, including the PR URL.

### 3.3 Module dependency map

```
api.py ──► db, agent, design_agent, graph, codebase_context, auth, rag.rag_ingest, rag.github_import
graph.py ──► agent, design_agent, dev_agent, (lazy) skills, qa_agent, auth, fastmcp.Client
            └─spawns subprocess──► mcp_server/github_mcp_server.py ──► GitHub REST
dev_agent ──► (lazy) db
rag.rag_ingest ──► rag.github_import, rag.ast_chunker
agent / design_agent / dev_agent / qa_agent / rag_ingest ──► google.genai (client built at IMPORT time)
auth ──► app.db (users)
static/index.html ──HTTP──► api.py
```

There are no circular imports. `graph → auth` and `dev_agent → db` are lazy imports.

---

## 4. Persistence

| Store | Table | Columns | Written by |
|---|---|---|---|
| `app.db` | `codebase_files` | `codebase_id, path, content` (PK both) | zip upload, repo auto-fetch |
| `app.db` | `codebase_context` | `codebase_id, context_json` | same (duplicates all file content again) |
| `app.db` | `brd_store` | `doc_id, brd_json` | legacy `/brd` only — **not** the pipeline |
| `app.db` | `users` | `id, github_id, username, avatar_url, access_token (plaintext), created_at, updated_at` | OAuth callback |
| `app.db` | `rag_chunks` | `repo, path, chunk_index, content, embedding(JSON), node_type, name, start/end_line` | RAG ingest |
| `checkpoints.db` | LangGraph tables | full `PipelineState` per step | `SqliteSaver` |

Gaps in this layer:

- There is no `projects`, `workflows`/`runs` (thread → user), `approvals`, `artifacts` or `audit_log` table.
- Approvals exist only as transient values inside checkpoints, with no record of who approved or when.
- There are no migrations.

Checkpoints do persist across restarts by design. However, the UI keeps `threadId` only in JS memory, so a page refresh or server restart orphans the run from the user's point of view.

---

## 5. API endpoints

| Method | Path | Auth | Used by UI | Notes |
|---|---|---|---|---|
| GET | `/` | – | – | serves `static/index.html` |
| GET | `/auth/github/login` | – | ✓ | sets `oauth_state` cookie |
| GET | `/auth/github/callback` | state check | ✓ | |
| POST | `/auth/logout` | – | ✓ | no CSRF token on POST |
| GET | `/auth/me` | session | ✓ | |
| GET | `/repos` | **required** | ✓ | lists owner repos incl. private |
| POST | `/pipeline/start` | optional | ✓ | anonymous LLM usage allowed |
| POST | `/pipeline/{thread_id}/resume` | **none** | ✓ | no ownership check (C4) |
| GET | `/pipeline/{thread_id}/download` | none | ✓ | design DOCX only |
| POST | `/codebase/upload` | none | ✓ | zip-slip safe; zip-bomb unsafe |
| POST | `/codebase/from-github` | — | ✓ | **does not exist → 404** (C5) |
| POST | `/rag/ingest` | required | ✓ | |
| GET | `/rag/status` | none | ✓ | |
| POST | `/rag/chat` | required | ✓ | not scoped to the user |
| POST | `/brd`, `/brd/{id}/design` | none | ✗ | legacy standalone endpoints |
| GET | `/brd/{id}/download`, `/brd/{id}/design/download` | none | ✗ | missing file → **500** [VERIFIED] |

## 6. MCP, RAG, evaluation and tests

**MCP server** (`mcp_server/github_mcp_server.py`):

- Exposes 3 tools; **no merge tool exists** [VERIFIED by grep]. This is a real strength.
- Weaknesses:
  - Base branch is hardcoded to `main` at lines 24 and 72, so it breaks on `master`/`develop` repos.
  - There are no HTTP timeouts.
  - `file_path` is not validated, so any path including `.github/workflows/*` or `../` is accepted.
  - Errors come back as raw exceptions.
  - The client launches `"python"` rather than `sys.executable` (`graph.py:126`).
  - The default `MCP_SERVER_PATH` is a hardcoded absolute path from another machine (`graph.py:119`).
  - Each file is a separate commit via the Contents API, which is not atomic.

**RAG:**

- Built:
  - Repo ingestion via the GitHub API, capped at 150 files and 5 MB, and silently truncated.
  - tree-sitter chunking.
  - Gemini embeddings.
  - Brute-force cosine over all chunks for the repo.
  - A single LLM answer with source paths.
- Not built: BM25, hybrid fusion, reranking, a vector index, a knowledge graph, RAG eval, ZIP-upload ingestion into RAG, and ingestion of approved artifacts.
- **No agent uses RAG.**
- Chunker defects [VERIFIED]:
  - Python `@decorated` functions (every Flask route) become anonymous `text` chunks.
  - `export function`/`export class` in JS/TS likewise become `text`.
  - Char-fallback chunks all carry the same start/end line.
  - `covered_ranges` is unused, and `chunk_index` is not meaningful.

**Evaluation:** `eval.py` has 4 BRD heuristics, needs a live key, and has no other stages.

**Tests:** There is no pytest, no assertions, no CI and no mocks. Results of running each script:

| Script | Result |
|---|---|
| `test_skills.py` | runs, prints correct output |
| `test_deploy_standalone.py` | **crashes**: `TypeError: _run_deploy() missing 2 required positional arguments` [VERIFIED] |
| `test_qa_*.py`, `test_codegen.py` | need a real Gemini key; `test_codegen` also needs a specific local `codebase_id` (`33436a1b`) |

---

## 7. Implementation status

### 7.1 Working (verified or very high confidence)

- LangGraph graph compiles. Mocked run traverses all five approval stages and reaches deploy [VERIFIED].
- Reject-with-feedback regeneration for BRD, Design and Plan [CODE].
- QA syntax layer (`ast`, Jinja2) and pyflakes layer [VERIFIED: pyflakes flagged `UndefinedName` as critical].
- Zip-slip protection [VERIFIED: `../evil.py` rejected with 400].
- OAuth CSRF `state` check on callback [CODE].
- MCP branch → write → PR flow. The code is coherent, but **not executed in this audit** (requires a real token and repo).
- BRD and Design DOCX rendering [CODE].
- tree-sitter chunking of undecorated functions and classes [VERIFIED].
- All files compile; `pip check` is clean; no circular imports [VERIFIED].

### 7.2 Partially implemented

| Feature | What exists | What's missing |
|---|---|---|
| Requirements Agent | BRD schema, retries, DOCX | NFRs, constraints, conversational editing, DOCX from pipeline, approved-BRD persistence |
| Architect Agent | components + FR traceability, data model | APIs, DB schemas, integrations, security, diagrams, reuse analysis; traceability not enforced |
| Planning Agent | file list + component link | tasks, execution order, acceptance criteria, create/modify/reuse distinction, DOCX |
| Coding Agent | full-file generation, retry edits previous attempt | per-task execution, cross-file context, change history, path validation |
| QA Agent | 3 layers (syntax, lint, LLM review) | execution, sandbox, unit/integration/API tests, security scan, BRD/arch compliance, report, 3-retry cap |
| Persistence | checkpoints | workflow ownership, approvals, history, artefact store |
| Auth | OAuth login | authorization on resources, token encryption, secure cookie flags |

### 7.3 Broken

- C1: QA auto-retry.
- C2: The server-side QA gate.
- C5: `/codebase/from-github`.
- C9: Human feedback at the code-review gate is ignored.
- `test_deploy_standalone.py`.
- Legacy download endpoints return 500 on a missing file.
- The README says to "open `static/index.html` in a browser". Opened as `file://`, all relative `fetch` calls fail; it must be served from `/`.

### 7.4 Missing (vs target)

- Sandbox and execution environment.
- Real unit, integration and API tests in QA.
- QA report artefact.
- Centralised LLM gateway, OpenRouter, token and cost tracking, prompt versioning.
- Hybrid RAG (BM25 + rerank) wired into the agents; approved-artifact knowledge base.
- Workflow history, project entity, and audit log.
- Rate limiting.
- Prompt-injection defences.
- Streaming progress. The UI's "activity feed" is a canned animation, not real events.
- Timeouts and graceful error states in graph nodes.
- `.env.example`.
- pytest suite and CI.

---

## 8. Critical bugs (ranked)

| # | Severity | Location | Finding | Evidence |
|---|---|---|---|---|
| C1 | Critical | `graph.py:109,277` | `qa_node` returns `qa_auto_retry`, which is **not a key of `PipelineState`**, so LangGraph drops it. The router always goes to `approval_qa`. Codegen ran exactly once on a failing QA. `qa_retry_count` still increments to 1, so the UI's "N auto-fix attempts" is false. | [VERIFIED] mocked run |
| C2 | Critical | `graph.py:282-284` | Approving at `approval_qa` routes to `deploy` regardless of `overall_status`. Only the UI hides the button, and the completion page even renders "Fail — approved anyway". | [VERIFIED] mocked run reached deploy with QA=fail |
| C3 | Critical (security) | `api.py:29` | `SessionMiddleware(secret_key=os.getenv(...))`. When unset, Starlette signs with `str(None)` = `"None"`. A forged cookie `{"user_id": 1}` was accepted, which means account takeover and PRs opened with the victim's token. | [VERIFIED] forged cookie |
| C4 | Critical (security) | `api.py:184` | `/pipeline/{thread_id}/resume` has no authentication or ownership check. Deploy uses the `user_id` stored in the thread, so whoever holds a thread_id can approve and deploy as its owner. | [CODE] |
| C5 | High | `static/index.html:916` | UI calls `POST /codebase/from-github`, which doesn't exist. | [VERIFIED] 404 |
| C6 | High (security) | `dev_agent.py:243` | `{"path": plan_path, **code_result}`: the LLM's returned `path` **overrides** the planned path. Together with no path validation in MCP `write_file`, model output (or injected repo content) can choose any file to write in the PR. | [CODE] |
| C7 | High | `qa_agent.py` | No code is executed. "Test cases" are LLM opinions, yet a "pass" unlocks deploy. This violates "do not mark passed if required tests were skipped". | [CODE] |
| C8 | High | `api.py:175,192` | Only `RuntimeError` is caught. `generate_test_cases`, `generate_semantic_review` and MCP errors raise other types with no retry, which gives a 500 and leaves the thread stuck mid-node. | [CODE]; 500 [VERIFIED] for bad thread |
| C9 | High | `graph.py:84-95, 250` | Rejecting at `approval_codegen` stores feedback in `feedback`, but `codegen_node` reads only `qa_feedback`. Human code-review feedback is silently ignored. This is the same class of bug the last commit fixed for the QA gate. | [CODE] |
| C10 | Medium | `codebase_context.py`, `api.py:214` | No uncompressed-size or file-count limit. A 58 KB zip expanding to 60 MB was accepted. Temp dirs are never deleted. The whole upload is read into memory before the size check. | [VERIFIED] |
| C11 | Medium | `agent.py:10` etc. | Gemini clients are built at import time, and `init_auth_db` raises without OAuth vars. The app cannot even be imported for tests without real credentials. | [VERIFIED] |
| C12 | Medium | `mcp_server/...:24,72` | Base branch hardcoded to `main`. | [CODE] |
| C13 | Medium | `rag/ast_chunker.py:140` | Decorated defs and exported declarations are not AST-chunked; fallback line numbers are wrong. | [VERIFIED] |
| ~~C14~~ | — | `graph.py:288` | ~~Shared `sqlite3` connection is a concurrency risk.~~ **Retracted:** `SqliteSaver` serializes all access with its own `threading.Lock`, so this is safe. | checked in Phase 2 |
| C15 | Low | `dev_agent.py:230` | Codegen sees only design title and overview. No BRD, components, data model or sibling files are given to it, which is the root cause of the cross-file mismatches QA then flags. | [CODE] |

## 9. Security findings

| Area | Status |
|---|---|
| Session secret | **Forgeable when unset (C3)**; no `https_only`/`same_site` hardening configured |
| Authorization | None on threads, RAG chat/status, uploads, legacy endpoints (C4) |
| OAuth CSRF | `state` cookie checked ✓; cookie lacks `secure`/`samesite` explicit flags |
| Token storage | GitHub access tokens stored **plaintext** in `users.access_token` |
| Scope | `public_repo` requested, but `/repos` lists private repos too (selectable, then fails) [LIKELY] |
| Prompt injection | Repo files, uploads, RAG chunks and user feedback are concatenated straight into prompts; no delimiting, no instruction hierarchy, no output policy. Generated code goes to GitHub |
| Upload validation | zip-slip ✓; zip-bomb ✗; extension filter only |
| Sandbox | None; nothing executes yet (but will need isolation once tests run) |
| Tool boundaries | MCP `write_file` accepts any path; no allow/deny list (e.g. `.github/workflows`, secrets files) |
| Rate limiting | None (anonymous `/pipeline/start` burns LLM credits) |
| Audit log | None; approvals not attributed to a user |
| Secrets | Loaded from env/.env ✓; no `.env.example`; README claim "pipeline never touches GitHub credentials" is inaccurate (graph passes the user token to the subprocess env) |
| Logging | User request text logged verbatim (minor PII concern) |
| No-merge guarantee | ✓ No merge tool exists anywhere [VERIFIED] |

## 10. Dependencies

- `requirements.txt` is a full `pip freeze` of 107 pins. It installs cleanly on Python 3.12 and `pip check` passes [VERIFIED].
- `pyflakes` is unpinned.
- `anthropic`, `sqlite-vec`, `langsmith` and `Authlib` are installed but unused.
- There is no `pytest`, `httpx` test client pin or dev-requirements split.
- Missing for the target: `rank-bm25` (or SQLite FTS5, which is built in), a reranker, `pytest`/`pytest-asyncio`, a rate limiter (e.g. `slowapi`), and Docker for the sandbox (system dependency). `streamlit` is only needed if the Streamlit UI is chosen.
- The FastAPI test client emits a deprecation warning recommending `httpx2`. This is cosmetic.

---

## 11. Recommended changes

These are guiding principles. They keep what works and fix the foundations before adding features.

1. **Keep:** FastAPI, LangGraph graph shape, SQLite checkpoints, the per-agent modules, the MCP server's 3-tool, no-merge design, and the tree-sitter chunker.
2. **Restructure incrementally into a package** (`app/agents`, `app/workflow`, `app/llm`, `app/rag`, `app/qa`, `app/security`, `app/storage`). Keep thin compatibility shims so nothing breaks mid-refactor.
3. **Centralise LLM calls** in one gateway with lazy client creation, provider routing (Gemini + OpenRouter), retries and fallback, timeouts, token and cost accounting, and versioned prompts. This also fixes C11 and makes everything mockable.
4. **Fix the graph:** add the missing state keys, enforce the QA gate server-side, cap retries at 3, unify feedback routing, and catch all node errors into an `error` state. Decide whether to keep `approval_codegen` (see decisions).
5. **Add a workflow/approvals/audit schema**, with thread ownership checks on every endpoint and a history listing.
6. **Build a real QA sandbox:** a Docker container with no network, CPU and memory limits, a read-only base and a timeout. Generated tests run there under pytest. Syntax, lint, security (`bandit`), traceability and architecture-compliance layers produce a structured, persisted QA report. Skipped required layers mean `fail`.
7. **Harden security:** require the session secret, add cookie flags, encrypt tokens at rest, rate-limit, cap zip sizes, validate paths in codegen and MCP, add prompt-injection delimiting, and redact logs.
8. **Upgrade RAG in place:** fix the chunker, add SQLite FTS5 BM25 alongside the existing vectors, add RRF fusion and an optional reranker, support ZIP ingestion, make agents consume retrieval, and ingest only approved artefacts.
9. **Build a real test suite** with pytest, mocked LLM and GitHub (clearly labelled), and one opt-in live integration test gated on env vars.

## 12. Decisions needed from you before Phase 2

1. **Frontend:** The brief says "Streamlit *or improve the existing interface*". There is a working 1,700-line HTML UI. My recommendation is to **keep and extend the existing FastAPI + HTML UI**, adding SSE streaming, history and auth, rather than rewrite in Streamlit. A rewrite would duplicate the API layer and lose existing work. Streamlit is the alternative.
2. **Code-review gate before QA (`approval_codegen`):** The target flow has Coding → Sandbox → QA → Human approval, with no human gate before QA. My recommendation is to **remove the pre-QA gate**, show the code at the post-QA gate instead, and keep "reject with feedback" there.
3. **QA retry cap:** It is currently 5 (and broken). Your spec says 3, and I'll use **3** unless you say otherwise.
4. **Sandbox technology:** My recommendation is **Docker**: `--network none`, resource limits, ephemeral container. It needs Docker Desktop installed on this Mac. The fallback is a subprocess with restricted env and timeouts, which is weaker and would be labelled as such.
5. **Vector store:** `ARCHITECTURE_RAG.md` proposes pgvector. My recommendation is to **stay on SQLite for now** (FTS5 for BM25, plus the existing vectors or `sqlite-vec`, which is already installed). That respects "SQLite for initial persistence" and "no unnecessary infrastructure". pgvector comes later, if needed.
6. **LLM providers:** Gemini stays the default and OpenRouter is added behind the gateway. Please confirm which keys you have.
7. **OAuth scope:** keep `public_repo` only, or move to `repo` to support private repos? The latter is a bigger blast radius.

## 13. Prioritised roadmap

| Phase | Work | Key files |
|---|---|---|
| **2 – Architecture & refactor plan** | Agree on the decisions in §12; define package layout, state schema, DB schema | new `docs/ARCHITECTURE.md` |
| **3 – Env & deps** | `.env.example`, config module (fail fast on missing secrets, test mode), lazy clients, dev requirements, pytest scaffold + mocks | `config.py`, `requirements*.txt`, `tests/` |
| **3a – Critical fixes (first!)** | C1, C2, C3, C4, C5, C6, C8, C9 with regression tests | `graph.py`, `api.py`, `dev_agent.py`, `index.html` |
| **4 – Requirements + Architect** | richer schemas (NFRs, constraints, APIs, DB, security, mermaid diagrams), enforced traceability, DOCX in-pipeline | `agent.py`, `design_agent.py` |
| **5 – Planning + Coding** | task-level plan w/ order & acceptance criteria; per-task codegen with sibling-file context, path validation, change history | `dev_agent.py` |
| **6 – Sandbox + QA** | Docker runner, 10 layers, structured report, 3 retries | `qa/` |
| **7 – Workflow & state** | workflows/approvals/audit tables, ownership, history, timeouts, error state, resumability, SSE events | `graph.py`, `storage/` |
| **8 – RAG** | chunker fixes, FTS5 BM25 + RRF + rerank, ZIP ingestion, agent integration, approved-artefact KB | `rag/` |
| **9 – MCP deploy** | default-branch detection, path allowlist, timeouts, structured errors, single-commit option; still no merge | `mcp_server/` |
| **10 – Frontend** | history, live status, previews/downloads for all artefacts, QA report, PR link | `static/index.html` |
| **11 – Security, evals, observability** | rate limits, token encryption, injection defences, audit log, per-agent metrics/cost, eval suite | across |
| **12 – E2E & docs** | mocked E2E + opt-in live GitHub E2E, README, setup guide, test report | `tests/`, `docs/` |

## 14. Configuration required (you will need to supply)

| Variable | Needed for | How to obtain |
|---|---|---|
| `GEMINI_API_KEY` | all agents, embeddings | https://aistudio.google.com/apikey |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | login | GitHub → Settings → Developer settings → OAuth Apps → New. Callback: `http://localhost:8000/auth/github/callback` |
| `SESSION_SECRET_KEY` | sessions | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `APP_BASE_URL` | OAuth redirect | `http://localhost:8000` locally |
| `MCP_SERVER_PATH` | deploy (until fixed) | absolute path to `mcp_server/github_mcp_server.py` |
| `OPENROUTER_API_KEY` (optional) | fallback provider | https://openrouter.ai/keys |
| A throwaway public GitHub repo | live deploy test | so the E2E PR test never touches a real project |

## Appendix: commands run during this audit

```
git clone <original prototype repository>
python3.12 -m venv venv && pip install -r requirements.txt        # OK
python -m compileall . -x venv                                     # OK
python -m pyflakes *.py rag mcp_server                             # 4 unused imports
pip check                                                          # No broken requirements
python -c "import agent"   (no key)                                # ValueError: No API key
python -c "import api"     (no OAuth vars)                         # RuntimeError from init_auth_db
probe_graph.py  (MOCKED LLM)                                       # C1, C2 reproduced
probe_misc.py   (TestClient, no network)                           # C5, C10, C13, 500s reproduced
session forgery probe                                              # C3 reproduced
python test_skills.py                                              # runs
python test_deploy_standalone.py                                   # TypeError
python test_qa_standalone.py (dummy key)                           # 400 invalid API key (needs real key)
```

The probe scripts live outside the repo (session scratchpad) and were not committed. No application code was modified.
