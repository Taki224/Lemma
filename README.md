<div align="center">
  <img src="frontend/public/favicon.svg" alt="Lemma logo" width="64" height="64" />
  <h1>Lemma</h1>
  <p><strong>Self-hosted AI LaTeX workspace with sandboxed TeX Live compilation, grounded citations, and multi-pass manuscript review.</strong></p>
  <p>
    <a href="#quick-start">Quick Start</a> •
    <a href="#features">Features</a> •
    <a href="#configuration">Configuration</a> •
    <a href="#development--testing">Development &amp; Testing</a>
  </p>
</div>

---

Lemma is a single-tenant, self-hosted alternative to cloud AI LaTeX editors. It pairs a CodeMirror 6 LaTeX editor and `pdf.js` preview with an isolated TeX Live compiler, OpenRouter/local LLM integration, Zotero & scholarly grounding, and Git-backed version snapshots—all persisted to a single `/data` volume.

## Features

- **Editor & Live PDF Preview** — CodeMirror 6 with LaTeX syntax highlighting, citation autocomplete, document outline, bidirectional SyncTeX navigation, and KaTeX math rendering alongside a `pdf.js` viewer.
- **Sandboxed TeX Live Compilation** — Compiles projects with `latexmk`, `pdflatex`, and `biber` under a dedicated unprivileged `texrunner` user inside `bubblewrap` (with `uid_rlimit` fallback), enforced CPU/memory limits, parsed error logs, and a CTAN / `tlmgr --usermode` package helper.
- **Context-Aware AI Assistant** — Agentic chat backed by OpenRouter or local / self-hosted OpenAI-compatible servers (such as Ollama, vLLM, or LM Studio), proposing inline unified diffs you can accept or reject per hunk. Includes optional smart model routing, automatic chat naming, Zero Data Retention (ZDR) mode, and per-request / daily spend caps.
- **Grounded Citations & Overlap Check** — Connects to Zotero libraries and scholarly/Unpaywall APIs, indexes attached PDFs into searchable chunks, verifies citations, and checks draft text for n-gram overlap against sources.
- **Review Studio** — Runs structured manuscript text reviews and visual page inspections with upfront cost estimation.
- **Built-in LaTeX Tools** — Citation health checks, visual-to-LaTeX conversion (equations, tables, diagrams from images), selection quick actions, and project export.
- **Version History & GitHub Sync** — Automatic Git snapshots on successful compiles and accepted AI edits, plus a GitHub repository connector.
- **Self-Hosted Security** — Argon2-verified workspace password, HTTP-only session cookies with idle timeout, CSRF protection, and automatic secret redaction in logs.

## Architecture

| Layer | Stack |
| --- | --- |
| **Backend** | Python 3.11+ (3.12 in Docker), FastAPI, Uvicorn, SQLite, Pydantic, PyMuPDF, Argon2 |
| **Frontend** | React 19, TypeScript, Vite, CodeMirror 6, `pdfjs-dist`, KaTeX |
| **TeX & Sandbox** | Upstream TeX Live (`scheme-basic`, `latexmk`, `biber`, `synctex`), `bubblewrap`, Git |

## Quick Start

### Docker Compose (Recommended)

The prebuilt multi-architecture Docker image (`taki224/lemma:latest`, supporting `linux/amd64` and `linux/arm64` for macOS, Windows, Linux, and Raspberry Pi) bundles Node-built frontend assets, the FastAPI backend, upstream TeX Live, and the `bubblewrap` sandbox.

1. Copy the example environment file and set your password (and optional API keys):

   ```bash
   cp .env.example .env
   ```

2. Pull and start the container:

   ```bash
   docker compose up -d
   ```

   To update to the latest published image later, run `docker compose pull && docker compose up -d`. (If you prefer to build from source instead, run `docker build -t taki224/lemma:latest .`.)

3. Open [http://localhost:8080](http://localhost:8080) and sign in with `WORKSPACE_PASSWORD` (default: `password`).

> [!IMPORTANT]
> Change `WORKSPACE_PASSWORD` in `.env` before exposing Lemma on a network. All projects, SQLite state, configuration, and user-mode TeX packages are persisted in `./data` (mounted at `/data`).

### Local Development

Prerequisites: Python 3.11+ (or [`uv`](https://docs.astral.sh/uv/)), Node.js with `npm`, and a local TeX Live installation if you want to compile `.tex` files locally.

```bash
cp .env.example .env
npm run dev
```

This runs `./run-local.sh dev`, which creates `.venv` if needed, installs backend and frontend dependencies, and starts:

- **Frontend (Vite HMR):** [http://localhost:3000](http://localhost:3000)
- **Backend (FastAPI auto-reload):** [http://127.0.0.1:8080](http://127.0.0.1:8080)

> [!TIP]
> You can also run `./run-local.sh start` (or `npm start`) to build the frontend and serve the production bundle directly from FastAPI on port `8080`.

## Configuration

Settings can be provided via `.env` / `.env.local` or container environment variables. Non-secret settings and API keys configured through the UI are persisted under `$DATA_DIR/config/` (`settings.yaml` and `secrets.yaml`).

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_DIR` | `/data` (`./data` locally) | Directory for projects, SQLite DB, config, caches, and backups |
| `WORKSPACE_PASSWORD` | `password` | Workspace login password (or Argon2 hash) |
| `OPENROUTER_API_KEY` | — | OpenRouter API key for AI chat, model catalog, and Review Studio |
| `LOCAL_MODEL_BASE_URL` | `http://localhost:11434/v1` | Base URL for a local / self-hosted OpenAI-compatible model server (e.g., Ollama, vLLM, LM Studio) |
| `LOCAL_API_KEY` | — | Optional API key for the local / self-hosted model server |
| `UNPAYWALL_EMAIL` | — | Optional email for Unpaywall open-access PDF lookups |
| `COMPILE_TIMEOUT_SECONDS` | `60` | Maximum seconds per LaTeX compilation |
| `COMPILE_MEMORY_LIMIT_MB` | `1024` | Memory limit (MB) for sandboxed TeX processes |
| `REQUEST_SPEND_CAP_USD` | `1.50` | Maximum estimated spend per AI request |
| `DAILY_SPEND_CAP_USD` | `10.00` | Maximum daily AI spend |
| `ZDR_ENABLED` | `true` | Enforce Zero Data Retention routing on OpenRouter requests |

<details>
<summary>Additional environment overrides</summary>

`SESSION_SECRET`, `SESSION_IDLE_TIMEOUT_SECONDS`, `TRUSTED_PROXY_IPS`, `MODEL_ALLOWLIST`, `REVIEW_CONFIRM_USD`, `OVERLAP_NGRAM_WORDS`, `CANDIDATE_TTL_DAYS`, `SNAPSHOT_COMPACT_THRESHOLD`, `ZOTERO_PUSH_ENABLED`, `SMART_ROUTING_ENABLED`, `SMART_ROUTING_MODEL`, `CHAT_NAMING_ENABLED`, and `CHAT_NAMING_MODEL`.

</details>

## Development & Testing

```bash
# Run both backend and frontend test suites (skips TeX Live integration tests)
npm test

# Run individually
npm run test:backend
npm run test:frontend

# Run backend tests that require a local TeX Live installation
cd backend && pytest -m requires_tex
```

## Project Structure

```text
.
├── backend/
│   ├── app/
│   │   ├── ai/            # OpenRouter & local LLM clients, chat loop, project index, routing
│   │   ├── auth/          # Password verification, sessions, CSRF, secret redaction
│   │   ├── github/        # GitHub repository connector
│   │   ├── grounding/     # PDF indexer, scholarly/Unpaywall client, citation & overlap checks
│   │   ├── history/       # Git snapshot service
│   │   ├── latex/         # Sandboxed compiler, SyncTeX, log parser, outline, CTAN helper
│   │   ├── projects/      # Project storage & file management
│   │   ├── review_studio/ # Manuscript text review & visual page inspector
│   │   ├── tools/         # Citation health check, visual-to-LaTeX, quick actions, export
│   │   └── zotero/        # Zotero client & candidate source staging
│   └── tests/             # Pytest suite
├── frontend/
│   ├── public/            # Static assets (favicon.svg)
│   └── src/               # React 19 UI, CodeMirror 6 extensions, pdf.js viewer
├── Dockerfile             # Multi-stage Node 22 + Python 3.12 + TeX Live + bubblewrap image
├── docker-compose.yml     # Single-container deployment with /data volume
└── run-local.sh           # Local dev, build, and test runner
```
