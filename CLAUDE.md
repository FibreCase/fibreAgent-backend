# CLAUDE.md

## What this is

A minimal, personal **Agent Backend** running on the owner's own server. It lets the
owner talk to an OpenAI-compatible-LLM-backed Agent over **Telegram**, with
**persistent conversations** that survive restarts, and an Agent that can use
**tools** via an OpenAI-style tool-calling loop.

The design goal is that this is *not* a "Telegram chatbot". It is layered from day
one, and the Tool loop is inserted between the Agent service and the LLM client
without touching the Telegram layer:

```
Telegram Adapter → Agent Service → Tool Loop Runtime → LLM Client → Persistent Conversation (SQLite)
   (→ AgentMessage: text/image)      ↑ drives Tool Registry (get_current_time / echo / system_info / [exec] / [file] / mcp_… / infra_…)
```

### Where to read

This file is the short, always-loaded guide: what the project is, plus the **rules and
conventions** you must follow when editing it. For the **full technical detail** —
the phase-by-phase history, per-module internals, the gotchas, the complete config
knob reference, the test-coverage map, and the per-feature limitations — see
**[`docs/developer-reference.md`](docs/developer-reference.md)**. The Chinese
user-facing docs in [`docs/`](docs/) describe the *product* (`status.md`, `tools.md`,
`configuration.md`, …); this file describes the *developer's rules*.

A current-state snapshot is in [`docs/status.md`](docs/status.md); the module/layering
picture is in [`docs/architecture.md`](docs/architecture.md).

---

## Commands

Dependency/env management is **uv**. There is no top-level `app/`; the package is
`src/fibrecase_agent_backend`.

```bash
uv sync                          # install runtime + dev deps into .venv
uv run pytest -q                 # full test suite (all mocked, never calls real LLM/Telegram)
uv run pytest tests/test_agent.py::test_same_conversation_is_serialised   # run one test
uv run python -m fibrecase_agent_backend   # run the backend (long polling)
uv run fibrecase-agent-backend             # same, via the console script
```

Run the backend from the **repo root** so it finds `.env` and
`config/system_prompt.txt` (paths are relative to the working directory).

Tests run with `pytest-asyncio` in **auto** mode (async test functions need no
decorator); config is in `pyproject.toml` `[tool.pytest.ini_options]`.

---

## Working style

- **Edit directly in the main checkout — do not create git worktrees for this
  project.** The project is small; a worktree buys isolation it doesn't need and only
  adds rebase/merge bookkeeping. Make changes and run tests from the repo root
  (`/Users/fibrecase/Code/agent-backend`). This overrides the default "isolate each
  task in a worktree" behaviour for this repo.

---

## Configuration & secrets

Everything external comes from env vars / `.env` (`config.py::load_config`).
`cp .env.example .env` to start. **Secrets (`TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`)
are env-only and must never be committed** — `.env` and `data/` are git-ignored.

Two rules that trip people up:

- **`OPENAI_BASE_URL` is the API *prefix*** (e.g. `https://<host>/v1`), **not** the
  full `.../chat/completions` URL. The OpenAI SDK appends `/chat/completions` itself.
  Setting it to the full URL is the #1 misconfiguration.
- `SYSTEM_PROMPT` (env) overrides `SYSTEM_PROMPT_PATH` (file) which overrides a
  built-in fallback. File is the intended default (`config/system_prompt.txt`).

Config parsing is **strict and fail-fast at startup**: a bad value is a `ConfigError`,
never a silent drop. The complete, authoritative list of every knob — its default,
its validation, and the cross-knob invariants — is in the
**[Configuration knob reference](docs/developer-reference.md#configuration-knob-reference)**
in `docs/developer-reference.md`. The high-level user-facing reference (Chinese) is in
[`docs/configuration.md`](docs/configuration.md).

---

## Architecture & invariants (read before editing)

The module map and dependency boundaries (the "who may import what" rules) are in
[`docs/architecture.md`](docs/architecture.md). The one-paragraph per-module
internals are in the
**[Per-module reference](docs/developer-reference.md#per-module-reference)**. The rules
below are the non-negotiables; the gotchas in
**[Gotchas that are easy to get wrong](docs/developer-reference.md#gotchas-that-are-easy-to-get-wrong)**
are the details that are *easy to violate*.

### Layering

- **Telegram layer never calls the OpenAI SDK** — it only calls
  `AgentService.process_message()`.
- **Agent Service is channel-agnostic** — a future Web UI / Discord / HTTP API reuses
  the same `AgentService`.
- **Only `llm/client.py` knows the OpenAI protocol**; only `database/` knows ORM/SQL.
- `attachments/`, `memory/`, `mcp/`, `mcp/auth/`, and `infrastructure/` contain **no**
  Telegram / OpenAI SDK / ORM imports (verify before editing). `mcp/` and
  `infrastructure/` may use their own SDKs (MCP SDK / AsyncSSH) but keep them
  inside; `infrastructure/` must **lazy-import** `asyncssh`.

### Invariants (must hold for any change)

- No Telegram logic in the agent service; no OpenAI-SDK imports outside
  `llm/client.py`.
- **Logs and the audit table never carry secrets, message bodies, tool arguments,
  tool results, image bytes/base64, MCP endpoints/tokens, OAuth tokens/codes/secrets/
  states, or infra hosts/paths/commands.** The tool-security paths log only the tool
  name, the stable event/result code, a short irreversible scope hash, and the
  exception *class* (never the exception text). The audit table stores only
  `scope_hash` + tool name + event type + stable code + latency. The full rule is in
  the gotchas.
- `attachments/` and `memory/` stay free of Telegram / OpenAI SDK / ORM imports.
- Every by-id memory read/delete is filtered by `scope + id` in SQL; every OAuth
  credential read is filtered by `telegram_user_id` (a foreign lookup is
  indistinguishable from missing — no existence leak).
- **Approval is a Telegram callback, not a tool.** Never add an `approve`/`confirm`
  tool — the model must never be able to grant itself approval.
- **Pre-execution audit is fail-closed** (a write failure means the tool does not
  run); terminal audit is best-effort (a write failure is logged, the tool is never
  re-executed).
- **Rehydration is plan-scoped**: only a *selected* turn's blobs are read from disk;
  a dropped/downgraded/out-of-range turn's image is never read.
- **`storage_key` is never user input** — blob paths derive only from the SHA-256.
- **`/new` and restarts never touch OAuth credentials, pending states, or memories.**
- **OAuth: the pending state is the only authority on the callback's target** (never
  trust callback query params); a **refresh failure keeps the credential** (never
  deletes it); the **per-user token rides a ContextVar**, not a loop argument; the
  **Google branch lives in exactly one place**
  (`main.py::_build_provider`); the **callback server is the only inbound listener**.
- `ENABLE_TOOLS=false` remains a full Phase-1 degradation.
- The backend **never encodes model capabilities by model name**.

---

## Adding a tool

A tool is a `tools.base.Tool` subclass: set `name`, `description`, a JSON-schema
`parameters` dict, a `default_permission` (`ToolPermission`), and implement
`async execute(arguments) -> str`. Then register it — add it in
`tools/builtin/__init__.py::build_default_tools()`, or pass your own `ToolRegistry`
into `AgentService` (which `main` builds from `build_default_tools()` when
`ENABLE_TOOLS=true`). That is the *entire* change: the registry advertises it in the
OpenAI schema and dispatches it by name.

Rules:

- **Do not** add `if name == "…"` branches anywhere — the registry is the single
  dispatch point.
- Tool results must be short, human/model-readable strings; **raise** on failure
  (the registry turns it into a stable JSON `{"error": ...}` the model sees).
- A new tool **defaults to `ask`** (the owner gets a one-time Approve/Deny before it
  runs). Only a safe read-only tool declares
  `default_permission = ToolPermission.ALLOW`. A *local* tool's declared default is
  final — `MCP_PERMISSIONS_FILE` overrides **MCP tools only**, never built-ins.
- Optional hooks you may override:
  - `approval_summary(arguments)` — the line under the approval card's
    `What it does:` label. Describe the tool's **purpose**; **never echo**
    `arguments` (the card shows the (schema-validated) arguments separately).
  - `approval_detail(arguments) -> str | None` — a **faithful, plain-text** argument
    view that, when non-`None`, **replaces** the generic `Arguments:` JSON under an
    `Action:` label. Show the real values verbatim (never a lossy paraphrase); the
    provider HTML-escapes + length-bounds + drops it on finalisation. (`file_edit`
    shows a git-diff, `exec` shows a `$ …` bash block.) Default `None` → generic JSON.
  - `approval_language(arguments) -> str | None` — a **fixed** Pygments language name
    (`diff` / `bash` / `json` / …) so Telegram syntax-highlights the block. It must be
    a fixed vocabulary declared by the tool, **never** derived from argument content
    (the provider sanitises to `[A-Za-z0-9_-]`, caps at 24 chars, lowercases, drops
    empty). Default `None` → unlabelled (the generic `Arguments:` block is always
    labelled `json`).
- **Do not** add an `approve`/`confirm` *tool* to grant approval — that would let the
  model grant itself access.

Every call is JSON-Schema-validated, wrapped in a per-tool timeout, policy-gated
(allow/ask/deny), and audited automatically — there is nothing extra to wire. See the
**[Per-module reference → `tools/`](docs/developer-reference.md#per-module-reference)**
and the `exec` / `file` sections for worked examples of the two opt-in state-changing
built-ins.

---

## Testing

`tests/conftest.py` provides an in-memory `repo` fixture and `FakeLLM` /
`RecordingLLM` fakes. **The whole suite is mocked** — nothing ever talks to the real
LLM endpoint, Telegram, an MCP server, an OAuth provider, or an SSH target. Tests use
in-memory SQLite via `StaticPool` (no real files); blob-store tests write to
`tmp_path`, never the repo's `data/`.

Conventions:

- **All mocked, no network / no subprocess / no real SSH.** Mock the SDK's `create`,
  the PTB handlers, `PhotoSize.get_file`, the LLM, the MCP SDK transport/session/
  `stdio_client`, the OAuth provider HTTP + `httpx2` transport via `MockTransport`,
  and `asyncssh` (a stub injected into `sys.modules` or `_connect` replaced with a
  fake).
- **Assert the privacy invariants in the same tests that exercise the feature** —
  caplog / logger-record assertions that no secret, argument, result, token, host, or
  body leaks.
- The per-phase behaviour-coverage map (which file proves which required behaviour) is
  in the **[Test coverage map](docs/developer-reference.md#test-coverage-map)**.

---

## Deployment (Docker + CI)

- **`Dockerfile`**: `python:3.14-slim` + `uv`, builds from the committed `uv.lock`.
  Runs as an unprivileged user; `/app/data` is the only writable path. **No
  `EXPOSE`** — the app is outbound-only by default; the only conditional inbound
  listener (the OAuth callback) is published by a default-**commented** `ports:`
  entry in `docker-compose.yaml`, never baked into the image.
- **`docker-compose.yaml`**: single service, config from the **same `.env`** the local
  `uv run` uses. Persists SQLite via a **bind mount** `./data:/app/data`.
  **Publishes NO ports by default.** The OAuth callback port is written but
  **commented out** — **uncomment it only when OAuth is on** (`OAUTH_CALLBACK_BASE_URL`
  set); a static `ports:` would open an inbound port for every deployment.
- **Host-uid mount**: the container runs as the host user (from `.env`) so the
  bind-mounted `./data` keeps host permissions and is writable — no `chown` needed.
- **CI** (`.github/workflows/build-image.yml`): on every `v*` tag push, builds the
  image and pushes to **ghcr.io** using the built-in `GITHUB_TOKEN` with
  `packages: write`.

The mechanics (why the venv path is pinned, why the port is inline-and-commented, the
exact ghcr.io image tags) are in the
**[Deployment internals](docs/developer-reference.md#deployment-internals)**.

Release flow: bump version in `pyproject.toml` +
`src/fibrecase_agent_backend/__init__.py`, run `uv lock`, commit,
`git tag -a vX.Y.Z`, `git push` + `git push origin vX.Y.Z`.

---

## Documentation & release conventions

Two standing rules for how the docs and releases stay in sync. These apply to every
change, not just the current phase.

- **Phase/task content belongs in `TASK.md`, not the README.** The README is a stable,
  user-facing description of the *current* state — how to configure, run, extend, and
  deploy. It must **not** carry per-task or per-phase narrative: no "Phase 2.2
  adds…", no per-phase changelog, and no hand-acceptance / manual test walkthroughs
  (e.g. a "手工验收测试" checklist) — those are one-off task deliverables that live in
  `TASK.md` (git-ignored and temporary). The README keeps a **single** concise
  "当前开发状态" section stating what it does and doesn't do *today*; when a phase
  ships, fold its durable facts in and drop the phase framing rather than stacking
  another phase paragraph. The same principle applies here: `CLAUDE.md` stays the
  short rules guide, and the long technical detail lives in
  `docs/developer-reference.md`.
- **Every version bump updates `README.md`.** The release flow is not just the version
  string: land the feature work *and* the matching README changes in one feature
  commit, then the version bump (`pyproject.toml` +
  `src/fibrecase_agent_backend/__init__.py` + `uv.lock`) in a second commit. If the
  README does not reflect what is being released, update it in the same release — the
  two commits above are the unit of a release, and the README must be current in it.
