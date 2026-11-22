# Next Steps: Feature Roadmap

This is the order we'll build the remaining features in. Each step is small enough to finish, test and demo on its own, and each one builds on the step before it. What's already done is listed in [PROGRESS.md](PROGRESS.md).

**Where we are (2026-11-22):**

- The full pipeline runs: Requirements → Design → Plan → Code → QA (3 auto-retries) → PR. There's a human gate at each review step.
- LLM calls go through a gateway: Gemini → OpenAI → OpenRouter, with JSON validation and usage tracking. LangSmith tracing is ready.
- Workflow state lives in SQLite checkpoints. Runs survive a page refresh and a server restart, and there's a history list.
- Every stage produces a DOCX. Repo chat (RAG) works.
- 84 automated tests.

---

## Step 0: Housekeeping (before anything else)

- [ ] Move the secrets out of `.env.example` (keep them only in `.env`) and rotate the exposed OpenAI key, OAuth client secret and GitHub PAT.
- [ ] Do a first **real** deploy to a throwaway public repo: branch, commits, PR opened, no merge.
- [ ] Turn on LangSmith (`LANGSMITH_TRACING=true` plus an API key) and confirm one full run shows up as a single trace.
- [ ] Commit the work so far on a feature branch.

**Done when:** a real PR exists on the test repo, a real trace exists in LangSmith, and the full test suite passes.

## Step 1: Requirements Agent v2 (Phase 4)

- Add **non-functional requirements**, constraints, stakeholders and success metrics to the BRD schema.
- **Conversational edits:** "change FR-3 to…" updates the existing BRD instead of regenerating it from scratch. Show what changed.
- Store the **approved BRD as a versioned artifact** (new `artifacts` table). It becomes the only input to the next stage.
- Record who approved it and when (`approvals` table).

**Done when:** a rejected BRD comes back with only the requested change, the approved version is stored, and the DOCX matches it.

## Step 2: Architect Agent v2 (Phase 4)

- Design schema gains **API endpoints, database schema, integrations, security requirements**, and a **Mermaid architecture diagram**, rendered in the UI and embedded in the DOCX.
- **Reuse analysis:** when a repo is attached, list the existing components to reuse, using RAG retrieval.
- **Enforced traceability:** every Must-have requirement maps to a component. Gaps block approval or are shown to the reviewer.

**Done when:** the design screen shows a diagram, APIs and schema, and an unmapped requirement is flagged.

## Step 3: Planning Agent v2 (Phase 5)

- Turn the plan into **ordered tasks**, each with files (create / modify / reuse), dependencies, acceptance criteria and risk.
- Works for a GitHub repo, an uploaded ZIP, or a brand-new project.

**Done when:** the plan shows a task list in dependency order, and each task traces back to design components.

## Step 4: Coding Agent v2 (Phase 5)

- **One task at a time**, in plan order. Each task sees the files already generated for earlier tasks (this fixes most cross-file mismatches).
- **RAG-grounded context:** pull the relevant existing code for each task, not just the file being edited.
- Keep a **change history** (diff per file per attempt) so a reviewer can see exactly what each retry changed.
- Structured error results per task.

**Done when:** a multi-file feature passes QA with fewer retries, and diffs between attempts are visible.

## Step 5: Sandbox + real test execution (Phase 6)

This is the biggest quality jump: QA stops *reading* code and starts *running* it.

- Run the generated code in an **isolated Docker container**: no network, CPU/memory/time limits, read-only base image.
- The Coding agent also writes **unit tests**. QA runs them with pytest, plus integration/API tests where relevant.
- Add the remaining QA layers: **security scan** (bandit), **BRD-requirement traceability**, **architecture compliance**, **regression** (existing tests still pass).
- Rule: **a skipped required layer = FAIL**. The QA report DOCX shows every layer's result.

**Done when:** a deliberately broken feature fails on an actual test run, the retry fixes it, and the report shows real test output. *(Needs Docker Desktop.)*

## Step 6: RAG v2 (Phase 8)

- Fix the chunker: Python decorated functions and JS/TS `export` declarations, plus correct line numbers.
- **Hybrid search:** SQLite FTS5 (BM25) plus vectors, fused with RRF, then a **reranker** on the top results.
- **ZIP uploads** can be ingested too, not just GitHub repos.
- **Approved-artifacts knowledge base:** only approved BRD, design, plan, final code and QA reports go in, never drafts.
- Small retrieval eval set (questions with expected source files) to measure improvements.

**Done when:** exact identifier searches find the right function, and the eval score improves over dense-only search.

## Step 7: Workflow hardening (Phase 7)

- **Audit log** of every approval, rejection, deploy and login (who, what, when).
- Per-step **timeouts** and graph-level retry policies.
- **Streaming progress (SSE):** replace the canned activity animation with real events from the running step (e.g. "generating file 3 of 7", "QA: pyflakes done").
- Optionally, run long steps in a background worker so HTTP requests return immediately.

**Done when:** the UI shows live step progress, and the audit log answers "who approved deploy for run X".

## Step 8: Deployment hardening (Phase 9)

- **One atomic commit** for all files (Git Data API) instead of one commit per file.
- A PR description built from the BRD, the design summary and the QA report, with links to the documents.
- Optional draft PR, and labels.
- Still **no merge tool**, enforced by a test.

**Done when:** a deploy produces one clean commit and a PR that links its documents.

## Step 9: Frontend polish (Phase 10)

- Side-by-side **diff view** of generated vs. existing files.
- Document previews in the browser, not only downloads.
- The "Ask AI" button on each stage (chat about the current document, grounded on RAG).
- Usage panel: tokens and cost per run, from `llm_calls` and LangSmith.

## Step 10: Security, evals, observability (Phase 11)

- **Rate limiting** per user/IP (e.g. `slowapi`).
- **Encrypt GitHub tokens at rest.**
- **Prompt-injection defences:** clearly delimit untrusted repo or upload content in prompts, and add output policy checks on generated code (secrets, network calls, dangerous APIs).
- **Agent evals:** extend `eval.py` to every agent (BRD completeness, design traceability, plan coverage, QA accuracy on known-bad code), runnable in CI, with results in LangSmith datasets.
- Cost budgets per run, with an alert or stop when exceeded.

## Step 11: End-to-end tests and docs (Phase 12)

- A mocked E2E suite in CI, plus an opt-in **live** E2E (real LLM plus the test GitHub repo).
- Final README, architecture doc, setup and deployment guide, and an E2E test report.
- Optional: Dockerfile / docker-compose to run the whole thing in one command.

---

### Acceptance criteria (from the original brief)

These are only met once steps 0–11 are done.

| Criterion | Status |
|---|---|
| User can authenticate (GitHub OAuth) | ✅ done |
| Submit request → BRD → approve → design → approve → plan → approve | ✅ done (v2 schemas in steps 1–3) |
| Coding agent generates code | ✅ done (task-by-task in step 4) |
| QA runs tests in an isolated environment | ❌ step 5 |
| Failed QA triggers controlled retries (max 3) | ✅ done |
| QA report produced | ✅ DOCX (full layers in step 5) |
| User approves deploy → MCP creates branch + PR | ✅ built; real run pending (step 0) |
| No automatic merge possible | ✅ done and tested |
| Workflow state survives restarts | ✅ done |
| End-to-end tests demonstrate the full flow | 🟡 mocked E2E done; live E2E in step 11 |
