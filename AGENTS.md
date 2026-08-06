# AGENTS.md

## Cursor Cloud specific instructions

This section captures durable, non-obvious context for running CFOperator in the
Cursor Cloud VM. Standard architecture/deploy docs live in `README.md`,
`.github/copilot-instructions.md`, `docs/DEPLOYMENT.md`, and
`docs/event-runtime-quickstart.md` — read those for the full picture.

### Services in this repo

- `agent` (chat UI + OODA loop) — Flask/Waitress web console on `:8083`. Entry:
  `python -m agent`. Needs Postgres (knowledge base) and an LLM (Ollama) to be
  fully functional.
- `event_runtime` (alert ingest → triage → audit) — HTTP service on `:8080`.
  Entry: `python -m event_runtime --host 0.0.0.0 --port 8080`. Zero external
  deps in portable mode.
- `mcp_server` / `bridge` — sibling processes that reuse the same image
  (`python -m mcp_server`, `python -m bridge`); not needed to exercise the core.
- Go siblings: `cfassist-go/` and `llm-gateway/` build with `go build` (see their
  Makefiles). `llm-gateway` is NOT in the agent's runtime path.

### Python environment

- Dependencies live in a repo-local venv at `.venv` (the system Python 3.12 is
  externally managed). Activate with `source .venv/bin/activate`. The update
  script creates/refreshes `.venv`.
- `pytest` and `pre-commit` are dev-only tools (not in `requirements.txt`); the
  update script installs them into `.venv`.
- GOTCHA: the `agent` package uses bare intra-package imports (e.g.
  `from knowledge_base import ...`), so it MUST run with
  `PYTHONPATH="$PWD/agent:$PWD"`. Running `python -m agent` without this fails
  with `ModuleNotFoundError: No module named 'knowledge_base'`. (The production
  Dockerfile sets the same `PYTHONPATH`.) `event_runtime` does NOT need this.

### Running tests

- The suite CANNOT be collected as one flat `pytest` run — several trees ship
  same-named top-level modules and rely on their own dir being on `sys.path`.
  Run it exactly as `.github/workflows/tests.yml` does: one `pytest` invocation
  per directory with `PYTHONPATH=<dir>:<repo-root>` (except `observability` and
  `auth`, which run with the repo root only). Copy the loop from that workflow.
- `test_tool_calling.py` is intentionally excluded (needs a live LLM).

### Lint

- The only configured hook is gitleaks (secret scanning) via
  `.pre-commit-config.yaml`. Run `pre-commit run --all-files`. The first run
  downloads the gitleaks hook (needs network).

### Local dev config (gitignored, already present on this VM)

- `config.yaml` and `.env` are gitignored dev files. This VM already has minimal
  local-boot versions. If they are missing, recreate them: copy
  `config.yaml.example` → `config.yaml` (point `database.host` at `127.0.0.1`,
  set `observability.containers: []` since there is no docker daemon, and set
  `llm.primary.model` to a locally-pulled Ollama model), and copy
  `.env.example` → `.env` (see the required keys below).
- Required `.env` keys for local boot: `POSTGRES_*` (db `cfoperator`, user
  `cfoperator`, password `cfoperator`), `SENTINEL_BUFFER_DIR` pointing at a
  writable path (the default `/data/buffer` is not writable — e.g.
  `/workspace/.cfoperator/buffer`), and `CFOP_AUTH_DISABLED=true` to bypass the
  `:8083` console login in local dev (documented in `web_auth.py`).

### PostgreSQL (knowledge base)

- Installed via apt (`postgresql-16` + `postgresql-16-pgvector`). systemd is not
  running in the VM, so start the cluster manually:
  `sudo pg_ctlcluster 16 main start`.
- Role/db: `cfoperator` / `cfoperator` (password `cfoperator`), with the
  `vector` extension enabled. The agent creates its own tables on startup
  (resilient — it also runs degraded with a local JSONL buffer if Postgres is
  down).

### LLM (Ollama)

- There is NO GPU here. Ollama is installed and runs CPU-only. Start it with
  `OLLAMA_HOST=127.0.0.1:11434 ollama serve` (again, no systemd). Models pulled
  for dev: `llama3.2:3b` (chat/tool-calling) and `nomic-embed-text` (embeddings).
- CPU inference is slow: a single chat turn (with the large system prompt + tool
  definitions) takes roughly 45–95s. This is expected, not a hang. In
  production the agent points `OLLAMA_URL` at a GPU host instead.

### Quick start (order matters)

1. `sudo pg_ctlcluster 16 main start`
2. `OLLAMA_HOST=127.0.0.1:11434 ollama serve` (background)
3. Agent: `PYTHONPATH="$PWD/agent:$PWD" CONFIG_PATH=config.yaml python -m agent`
   → console at `http://localhost:8083` (`/api/health` is the liveness check).
4. Event runtime: `CONFIG_PATH=config.yaml python -m event_runtime --host 0.0.0.0 --port 8080`
   → `http://localhost:8080/health`; exercise it with
   `curl -X POST 'http://localhost:8080/alert?mode=sync' -H 'Content-Type: application/json' -d '{"source":"manual","severity":"warning","summary":"test"}'`
   then `curl http://localhost:8080/history?limit=5`.
