# SDLC Pipeline Agent

A multi-agent pipeline that takes a plain-English feature request and carries
it through Requirements → Design → Dev Planning → Code Generation → QA →
Deploy, with a human approval gate at every stage.

Built with Python, FastAPI, LangGraph, SQLite, and the Gemini API (with
OpenAI and OpenRouter as fallback providers). Deploy integrates with GitHub over the
**Model Context Protocol (MCP)**.

> Project docs: [codebase audit](docs/CODEBASE_AUDIT.md) ·
> [architecture & refactoring plan](docs/ARCHITECTURE.md) ·
> [progress log & test results](docs/PROGRESS.md) ·
> [next steps roadmap](docs/NEXT_STEPS.md)

## What it actually does

You describe a feature (optionally pointing it at an existing codebase),
and each stage runs as a real agent call, not a mock:

1. **Requirements** — generates a structured BRD (business objectives,
   functional requirements, assumptions, open questions)
2. **Design** — produces a technical design grounded in the real codebase,
   with explicit traceability back to each requirement
3. **Dev** — an implementation plan (files, purpose, what each should
   contain) — not code yet, deliberately
4. **Codegen** — generates real, complete file content for every file in
   the plan, using the actual current contents of the codebase where they
   exist
5. **QA** — runs automatically after Codegen: deterministic syntax validation
   (`ast.parse` / Jinja2 parsing), pyflakes, then an LLM cross-file review
   against acceptance test cases. A failing QA is sent back to Codegen
   automatically, **at most 3 times**. Generated code is **not executed** yet
   (sandboxed test runs are planned), and the QA report says so explicitly.
6. **Deploy** — pushes the generated changes to a new branch and opens a
   real pull request on GitHub, via a purpose-built MCP server

Requirements, Design, Plan and the final Code + QA review each pause for
explicit human approval. Rejecting with feedback sends the stage back for
regeneration. **A failing QA can never be approved for deploy** — that is
enforced by the server and the workflow graph, not just the UI.

## Grounding against a real codebase

Codegen and Design aren't working blind. There are two ways to give the
pipeline real code to ground against:

- **Upload a zip** of the codebase directly
- **Pick a GitHub repo** you're logged into — the pipeline fetches its
  current files automatically via your GitHub token

For deeper Q&A grounding, repos can also be **RAG-ingested**: files are
chunked with a **tree-sitter AST-aware chunker** (`rag/ast_chunker.py`)
that splits on real function/class boundaries across 11 languages
(Python, JS/TS, Go, Java, Ruby, Rust, C/C++, PHP) rather than arbitrary
character windows — keeping a small function or class whole as one chunk,
and only recursing into a large one for smaller coherent units. Files in
unsupported languages, or that fail to parse, fall back to a
character-window splitter so nothing is silently dropped.

## Auth

Login is real GitHub OAuth (`auth.py`), not a shared token — each user's
own GitHub access token is stored per-user and used when *they* trigger
Deploy or RAG ingestion, scoped to `public_repo` only (no private repo
access; the repo picker lists public repos only). Session state is a signed
cookie; the OAuth flow includes CSRF state verification on the callback.
Every pipeline run belongs to the user who started it — other users get 404.

## Why Deploy only opens a PR, never merges

This is the one non-negotiable design decision in the whole system: the
GitHub MCP server exposes exactly three tools — `create_branch`,
`write_file`, `open_pull_request` — and no `merge` tool exists anywhere in
the code. No matter what the agent generates, or how confident it is, a
human has to click merge. This isn't a config flag that could be toggled
off under pressure — it's a capability that was never built.

## MCP integration

The Deploy stage is an MCP client. It spawns a standalone MCP server
(`mcp_server/github_mcp_server.py`, built on `fastmcp`) over stdio, passing
the logged-in user's token in that subprocess's environment only, and calls
its three tools in sequence. The server branches off the repo's default
branch, and re-validates every file path (no `..`, `.git/`,
`.github/workflows/`, `.env`) as the last boundary before GitHub.

## Status

See [docs/PROGRESS.md](docs/PROGRESS.md) for what is done, what was verified
and how (mocked vs. live), and what is next.

## Running it

```bash
python3.12 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # then fill it in — see comments in the file
./venv/bin/uvicorn api:app --reload
```

Then open **http://localhost:8000** (the UI is served by the API — opening
`static/index.html` as a file will not work). Log in with GitHub, describe a
request, optionally upload a codebase zip or pick a repo, and start the
pipeline.

Each review step offers a DOCX download (BRD, Design, Implementation Plan, QA Report); all
of them are listed again when the run completes. If you didn't pick a repo before
starting, you choose the (public) repo for the pull request at the final QA step.

Required configuration (details in `.env.example`):

| Variable | Why |
|---|---|
| `SESSION_SECRET_KEY` | **The app refuses to start without it** (≥32 random chars) |
| `GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | Login. Without them the app runs but login returns 503 |
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` (at least one) | LLM calls, tried in `LLM_PROVIDER_ORDER` (default Gemini → OpenAI → OpenRouter). Repo chat (RAG) embeddings use Gemini or OpenAI |

## Tests

```bash
./venv/bin/python -m pytest
```

The automated suite (`tests/`) needs no credentials and makes no network
calls: LLM providers and GitHub are **mocked**, and a guard fails any test
that tries to reach the network. The Deploy test runs the real MCP server
process over stdio against a local fake GitHub. The `test_*.py` files in the
repo root are **manual** scripts that call real Gemini/GitHub.

## Acknowledgements

This project builds on an original prototype by
[Sai Kiran Giragani (@SaiKiran-0704)](https://github.com/SaiKiran-0704/AI_SDLC_PIPELINE),
whose first version included the agent chain, the LangGraph approval flow,
GitHub OAuth, the GitHub MCP server and the tree-sitter RAG chunker.
Thank you!
