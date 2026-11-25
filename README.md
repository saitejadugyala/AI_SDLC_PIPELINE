# AISDLC Pipeline Agent

**An AI-powered, human-in-the-loop Software Development Lifecycle (SDLC) automation platform.**

SDLC Pipeline Agent is a multi-agent system that transforms plain-English feature requests into structured requirements, technical designs, implementation plans, generated code, QA reviews, and GitHub pull requests.

Built with **Python, FastAPI, LangGraph, SQLite, LiteLLM, and multiple LLM providers**, the platform orchestrates specialized agents through a controlled workflow, with explicit human approval gates at every major stage.

The platform includes a centralized **LiteLLM-based LLM gateway** and supports **Google Gemini, Groq, OpenAI, and OpenRouter** for model integration and fallback routing. GitHub integration includes GitHub OAuth authentication, repository access, and MCP-based deployment through pull requests.

---

## 1. Core Capabilities

The pipeline accepts a feature request in natural language and optionally uses an existing codebase as implementation context.

It processes the request through six stages:

### 1. Requirements Analysis

Converts the feature request into a structured Business Requirements Document (BRD).

The generated document includes:
- Business objectives
- Functional requirements
- Assumptions and constraints
- Open questions
- Acceptance criteria

The generated requirements require human approval before proceeding.

### 2. Technical Design

Generates a technical design grounded in the existing codebase when one is provided.

The design includes:
- Proposed architecture and components
- Existing codebase analysis
- Required modifications and integrations
- Technical decisions
- Requirement-to-design traceability

Each design decision is linked to its corresponding requirement wherever applicable.

### 3. Development Planning

Transforms the approved technical design into a structured implementation plan.

For each proposed file, the plan identifies:
- File path
- Purpose and responsibility
- Expected implementation content
- Relationship with existing components

This stage produces an implementation plan, not source code. Separating planning from code generation allows the proposed changes to be reviewed before implementation begins.

### 4. Code Generation

Generates complete file contents based on the approved implementation plan.

Code generation is grounded in:
- Approved technical design
- Approved implementation plan
- Existing source files, where available
- Relevant codebase context retrieved through RAG

When modifying an existing project, the agent receives the current contents of relevant files to reduce inconsistencies and preserve existing implementation context.

### 5. Automated QA Review

QA runs automatically after code generation.

The current QA pipeline combines deterministic validation with AI-assisted code review.

**Deterministic checks**
- Python syntax validation using `ast.parse`
- Jinja2 template parsing
- Static linting using `pyflakes`

**AI-assisted review**
- Cross-file consistency analysis
- Implementation review against acceptance test cases
- Identification of potential defects and missing requirements

If QA fails, the generated changes are automatically returned to Codegen for correction.

The correction cycle is limited to a maximum of **three attempts**.

Important current limitations:
- Generated code is not executed.
- Runtime behaviour is not verified.
- Automated integration and end-to-end execution are not yet implemented.
- Sandboxed test execution is part of the planned roadmap.

The QA report explicitly identifies these limitations rather than presenting unexecuted code as verified software.

The final code and QA results require human approval.

**Deployment is blocked whenever QA has failed.** This restriction is enforced by both the backend and the LangGraph workflow.

### 6. GitHub Deployment

The deployment stage publishes approved generated changes through a GitHub pull request.

The deployment workflow:
1. Connects to the selected GitHub repository.
2. Creates a new feature branch from the repository's default branch.
3. Writes the approved generated files.
4. Opens a pull request containing the proposed changes.

Deployment is implemented through a dedicated GitHub MCP server.

**The system never merges pull requests automatically.** Final integration into the target branch remains under human control.

---

## 2. Human-in-the-Loop Workflow

Human approval is a fundamental part of the architecture.

The workflow pauses at the following checkpoints:

| Stage | Human approval |
|---|---|
| Requirements | Approve or reject the BRD |
| Design | Approve or reject the technical design |
| Development Plan | Approve or reject the implementation plan |
| Code Generation & QA | Review and approve generated changes and QA results |
| Deployment | Open a pull request; merging remains a human action |

When a reviewer rejects a stage, feedback is provided to the corresponding agent for regeneration.

This creates a controlled development process in which agents can perform implementation work autonomously while humans retain decision-making authority.

**Workflow overview**

```text
       Feature Request
              |
              v
      Requirements Agent
              |
         Human Approval
              |
              v
         Design Agent
              |
         Human Approval
              |
              v
       Development Plan
              |
         Human Approval
              |
              v
        Code Generation
              |
              v
          QA Pipeline
              |
         +-----+-----+
         |           |
       PASS         FAIL
         |           |
         |      Retry Codegen
         |      (Max 3 attempts)
         |           |
         |       Run QA Again
         |           |
         +-----+-----+
              |
        Human Approval
              |
              v
        GitHub Deployment
              |
        Create New Branch
              |
          Write Files
              |
        Open Pull Request
              |
              v
         Human Merge
```

A failed QA result cannot be approved for deployment. This is enforced at the application and workflow levels, not merely through frontend controls.

---

## 3. Existing Codebase Integration

The platform supports two methods of providing an existing codebase.

### Option A: ZIP Upload

Users can upload a ZIP archive containing their existing project.

The pipeline uses the extracted source files as context for technical design and code generation.

### Option B: GitHub Repository

Users can authenticate through GitHub OAuth and select one of their accessible public repositories.

The application retrieves the repository's current files using the authenticated user's GitHub access token.

The selected repository can subsequently be used for deployment.

### Repository RAG Ingestion and Retrieval

The platform includes an implemented **Retrieval-Augmented Generation (RAG)** subsystem for grounding agents in an existing repository.

Repository source files are ingested, divided into meaningful chunks, embedded, and made available for retrieval so that downstream stages can use relevant codebase context instead of relying only on the feature request.

The ingestion pipeline uses a Tree-sitter-based AST-aware chunker implemented in:

`rag/ast_chunker.py`

Unlike conventional character-based chunking, AST-aware chunking uses source-code structure to identify meaningful boundaries.

Supported language grammars include:

- Python
- JavaScript
- TypeScript
- Go
- Java
- Ruby
- Rust
- C
- C++
- PHP

The chunking strategy:
1. Parses source files using Tree-sitter.
2. Identifies functions, classes, and other supported structural nodes.
3. Preserves small functions and classes as coherent chunks.
4. Recursively splits larger structures into smaller units when necessary.
5. Falls back to character-window chunking when a language is unsupported or parsing fails.

This approach preserves meaningful source-code context and avoids silently dropping files that cannot be parsed.

Retrieved repository context supports codebase analysis, technical design, and code generation.

---

## 4. Authentication and Security

Authentication is implemented using GitHub OAuth.

Each user authenticates through their own GitHub account. The application does not rely on a shared GitHub access token.

### Authentication design

- GitHub OAuth-based login.
- Per-user GitHub access-token storage.
- Signed session cookies.
- CSRF state verification during OAuth callbacks.
- Public repository access through the `public_repo` scope.
- Public repositories only; private repository access is not supported.

### User isolation

Every pipeline execution is associated with the user who initiated it.

Users cannot access another user's pipeline runs. Unauthorized access attempts return HTTP 404.

### Deployment security

The GitHub MCP server validates file paths immediately before writing them to GitHub.

Restricted paths include:
- Path traversal attempts (`..`)
- `.git/`
- `.github/workflows/`
- `.env`

This provides an additional validation boundary between generated code and GitHub operations.

---

## 5. MCP-Based GitHub Integration

The Deploy stage operates as an MCP client.

It launches a standalone GitHub MCP server implemented using `fastmcp`:

`mcp_server/github_mcp_server.py`

Communication takes place over standard input/output (stdio).

The authenticated user's GitHub token is passed to the MCP server through the subprocess environment rather than being included in normal tool arguments.

### Exposed MCP tools

The server exposes exactly three GitHub operations:

| Tool | Responsibility |
|---|---|
| `create_branch` | Creates a new feature branch |
| `write_file` | Writes generated content to the branch |
| `open_pull_request` | Opens a pull request |

### Why there is no merge operation

The absence of a merge operation is an intentional architectural security decision.

The MCP server does not expose a `merge` tool, and no automated merge functionality is implemented in the deployment workflow.

This means:
- Agents cannot merge their own changes.
- Generated code cannot bypass the pull-request review process.
- Human reviewers retain final control over repository integration.

This is a capability-level restriction rather than a configurable deployment setting.

---

## 6. Technology Stack

| Technology | Purpose |
|---|---|
| Python | Core application and agent implementation |
| FastAPI | Backend API and application server |
| LangGraph | Multi-agent workflow orchestration |
| SQLite | Persistent application data |
| LiteLLM | Centralized LLM gateway and provider integration |
| Google Gemini | Supported LLM provider |
| Groq | Supported LLM provider |
| OpenAI | Supported LLM provider |
| OpenRouter | Supported LLM provider and fallback option |
| RAG | Repository context retrieval and grounding |
| Tree-sitter | AST-aware source-code parsing |
| FastMCP | MCP server implementation |
| GitHub OAuth | User authentication |
| GitHub API | Repository operations and pull requests |
| Pytest | Automated testing |
| Pyflakes | Static code analysis |

---

## 7. LLM Gateway and Provider Configuration

The application uses **LiteLLM as a centralized LLM gateway**.

This provides a consistent interface for communicating with multiple LLM providers and centralizes provider configuration and fallback behaviour.

### Supported LLM providers

- Google Gemini
- Groq
- OpenAI
- OpenRouter

### Gateway responsibilities

- Centralized model communication.
- Multiple LLM provider integrations.
- Configurable provider routing.
- Fallback handling when a configured provider is unavailable or fails.
- Consistent LLM interaction across pipeline agents.

Provider order and fallback behaviour are controlled by the application's LLM configuration.

At least one valid provider API key must be configured for agent execution.

The RAG embedding subsystem supports Gemini or OpenAI embeddings, as configured by the application.

---

## 8. Project Structure

```text
SDLC_PIPELINE_AGENT/
│
├── api.py
├── auth.py
├── requirements-dev.txt
├── .env.example
│
├── agents/
│   ├── requirements/
│   ├── design/
│   ├── development/
│   ├── codegen/
│   └── qa/
│
├── workflow/
│   └── LangGraph workflow implementation
│
├── rag/
│   └── ast_chunker.py
│
├── mcp_server/
│   └── github_mcp_server.py
│
├── static/
│   └── index.html
│
├── docs/
│   ├── pipeline.svg
│   └── internal-flow.svg
│
├── tests/
│   └── Automated test suite
│
└── test_*.py
    └── Manual integration scripts
```

*The structure above illustrates the main architectural areas; refer to the repository for the exact current file organization.*

---

## 9. Running the Application

### Prerequisites

- Python 3.12
- GitHub OAuth application credentials
- At least one supported LLM provider API key

### Step 1: Clone the repository

```bash
git clone <repository-url>
cd SDLC_PIPELINE_AGENT
```

### Step 2: Create a virtual environment

```bash
python3.12 -m venv venv
```

### Step 3: Install dependencies

```bash
./venv/bin/pip install -r requirements-dev.txt
```

### Step 4: Configure environment variables

```bash
cp .env.example .env
```

Update `.env` with the required credentials.

### Step 5: Start the application

```bash
./venv/bin/uvicorn api:app --reload
```

### Step 6: Open the application

Navigate to:

`http://localhost:8001`

The frontend is served by FastAPI. Opening `static/index.html` directly in a browser is not supported.

### Step 7: Run the pipeline

1. Log in using GitHub OAuth.
2. Enter a feature request.
3. Optionally upload a ZIP archive or select a public GitHub repository.
4. Start the pipeline.
5. Review and approve each generated stage.
6. Review the generated code and QA report.
7. Select a public repository if one was not selected earlier.
8. Approve deployment to create a branch and open a pull request.

Each review stage provides a DOCX download for its corresponding artifact:
- Business Requirements Document
- Technical Design
- Implementation Plan
- QA Report

The generated documents are also listed when the pipeline run completes.

---

## 10. Environment Variables

Configure the following values in `.env`.

| Variable | Description |
|---|---|
| `SESSION_SECRET_KEY` | Required signing key for application sessions. The application refuses to start without it. Must contain at least 32 random characters. |
| `GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth application client ID. |
| `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth application client secret. |
| `GEMINI_API_KEY` | Gemini API authentication. |
| `GROQ_API_KEY` | Groq API authentication. |
| `OPENAI_API_KEY` | OpenAI API authentication. |
| `OPENROUTER_API_KEY` | OpenRouter API authentication. |
| `LLM_PROVIDER_ORDER` | Configures the provider routing and fallback order used by the LiteLLM gateway. |

GitHub OAuth credentials are required for authentication and GitHub operations. Without them, the application can start, but login returns HTTP 503.

At least one LLM provider API key must be configured for agent execution.

See `.env.example` for the complete configuration and additional settings.

---

## 11. Testing

The project includes an automated test suite and separate manual integration scripts.

### Automated tests

Run:

```bash
./venv/bin/python -m pytest
```

The automated test suite:
- Does not require real API credentials.
- Uses mocked LLM providers.
- Uses a mocked GitHub integration.
- Prevents unintended network access through a network-call guard.
- Tests the GitHub MCP server as a real subprocess over stdio.
- Uses a local fake GitHub implementation for deployment tests.

This allows the core workflow and deployment behaviour to be tested without external services.

### Manual integration tests

The `test_*.py` scripts located in the repository root are manual integration scripts.

Unlike the automated test suite, these scripts can make real API calls to LLM providers and GitHub and may require valid credentials and network access.

---

## 12. Current Implementation Status

The project distinguishes between implemented functionality, verified behaviour, and planned capabilities.

| Capability | Current status |
|---|---|
| Requirements generation | Implemented |
| Technical design generation | Implemented |
| Development planning | Implemented |
| Code generation | Implemented |
| Human approval and rejection | Implemented |
| Deterministic QA checks | Implemented |
| LLM-based cross-file QA review | Implemented |
| Automatic Codegen-to-QA retry | Implemented, maximum 3 attempts |
| QA deployment blocking | Implemented |
| ZIP-based codebase grounding | Implemented |
| GitHub repository integration | Implemented |
| Repository RAG ingestion and retrieval | Implemented |
| AST-aware RAG chunking | Implemented |
| LiteLLM-based centralized LLM gateway | Implemented |
| Gemini integration | Implemented |
| Groq integration | Implemented |
| OpenAI integration | Implemented |
| OpenRouter integration and fallback | Implemented |
| GitHub OAuth authentication | Implemented |
| MCP-based GitHub deployment | Implemented |
| Automated pull-request creation | Implemented |
| Automated pull-request merging | Intentionally not implemented |
| Sandboxed generated-code execution | Planned |
| Automated runtime and integration testing | Planned |

**Implementation status does not automatically mean that every behaviour has been independently verified.**

For verification details, test outcomes, and known limitations, refer to the automated test suite and relevant implementation documentation.

---

## 13. Roadmap

The next development milestones are focused on improving code quality, execution safety, and the reliability of generated software.

Planned areas include:

- Isolated sandbox execution for generated code.
- Automated unit and integration test execution.
- Multi-language QA support.
- AI-assisted test generation.
- Improved static analysis and security checks.
- More advanced codebase retrieval and contextual reasoning.
- Improved observability and agent execution tracing.

---

## Design Philosophy

The objective of this project is not simply to generate code using AI.

It is to build a controlled software engineering workflow in which specialized agents collaborate across the SDLC, deterministic checks validate their outputs, and humans retain authority over critical decisions.

**AI proposes. Deterministic systems validate. Humans approve.**
