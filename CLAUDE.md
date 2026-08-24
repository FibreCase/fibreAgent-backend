# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal, personal **Agent Backend** running on the owner's own server. It lets the owner talk to an OpenAI-compatible-LLM-backed Agent over **Telegram**, with **persistent conversations** that survive restarts, and (phase 2.1) the Agent can use **tools** — currently `get_current_time`, `echo`, and `system_info` — via an OpenAI-style tool-calling loop.

The design goal is that this is *not* a "Telegram chatbot". It is layered from day one, and the Tool loop it promised is now inserted between the Agent service and the LLM client without touching the Telegram layer:

```
Telegram Adapter → Agent Service → Tool Loop Runtime → LLM Client → Persistent Conversation (SQLite)
                                   ↑ drives Tool Registry (get_current_time / echo / system_info)
```

## Status

**Phase 1 is complete, tested, and shipped** (tagged **v1.1.1**; a GitHub Action builds & pushes the image to **ghcr.io** on every `v*` tag push), and **Phase 2.1 (Tool Calling Runtime) is now implemented and mocked-tested** (83 tests; not yet tagged as a release). Phase 1 delivers:

- Telegram long-polling adapter: allow-list auth, `/start` `/new` `/help` `/status`, typing keep-alive, long-reply chunking, graceful error handling.
- Channel-agnostic `AgentService` with per-conversation `asyncio.Lock` (serialise one chat, parallelise across chats).
- `OpenAIClient` (OpenAI-compatible) with user-safe LLM-error translation.
- Persistent SQLite conversations that **survive restarts** (verified across a simulated restart).
- **Docker deployment** (`Dockerfile` + `docker-compose.yaml`, single shared `.env`).

Phase 2.1 adds, on top of the same layered design, a **tool-calling runtime** (enabled by default via `ENABLE_TOOLS=true`; set it to `false` to fully degrade to the Phase-1 chat-only path — which is then byte-for-byte unchanged):

- A provider-/channel-agnostic `tools/` package: `Tool` interface, `ToolRegistry` (register / OpenAI-schema / execute-by-name), and three safe built-ins (`get_current_time`, `echo`, `system_info` — stdlib-only, no subprocess).
- `agent/tool_loop.py::run_tool_loop()`: call LLM → if `tool_calls`, run each via the registry and feed `tool` results back → repeat until a final text answer or `MAX_TOOL_ITERATIONS` is hit.
- `OpenAIClient.complete()` now accepts `tools=` and surfaces `tool_calls` on the result; `AgentService` drives the loop and persists **only** the user turn + final assistant turn (intermediate tool turns are not stored).
- Two new config knobs: `ENABLE_TOOLS` (degrades fully to Phase 1 when `false`) and `MAX_TOOL_ITERATIONS` (default `5`).

Only the "Extending (phase 2.2+, not yet)" section remains unbuilt (MCP, etc.). Two Phase-1 runtime bugs were found and fixed *after* the initial implementation (both now covered by tests): the nested event-loop crash on startup, and the missing `CallbackContext` shortcuts (see Gotchas).

## Commands

Dependency/env management is **uv**. There is no top-level `app/`; the package is `src/fibrecase_agent_backend`.

```bash
uv sync                          # install runtime + dev deps into .venv
uv run pytest -q                 # full test suite (all mocked, never calls real LLM/Telegram)
uv run pytest tests/test_agent.py::test_same_conversation_is_serialised   # run one test
uv run python -m fibrecase_agent_backend   # run the backend (long polling)
uv run fibrecase-agent-backend             # same, via the console script
```

Run the backend from the **repo root** so it finds `.env` and `config/system_prompt.txt` (paths are relative to the working directory).

Tests run with `pytest-asyncio` in **auto** mode (async test functions need no decorator); config is in `pyproject.toml` `[tool.pytest.ini_options]`.

## Configuration & secrets

Everything external comes from env vars / `.env` (`config.py::load_config`). `cp .env.example .env` to start. **Secrets (`TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`) are env-only and must never be committed** — `.env` and `data/` are git-ignored.

Two non-obvious rules:
- **`OPENAI_BASE_URL` is the API *prefix*** (e.g. `https://<host>/v1`), **not** the full `.../chat/completions` URL. The OpenAI SDK appends `/chat/completions` itself. (Verified against a local HTTP server: with base `.../v1` it requests exactly `/v1/chat/completions`.) Setting it to the full URL is the #1 misconfiguration.
- `SYSTEM_PROMPT` (env) overrides `SYSTEM_PROMPT_PATH` (file) which overrides a built-in fallback. File is the intended default (`config/system_prompt.txt`).

Two tool-calling knobs (phase 2.1):
- **`ENABLE_TOOLS`** (default `true`): when `false`, `AgentService` skips the tool loop entirely and behaves exactly as Phase 1 (one LLM call, no `tools` advertised, nothing tool-related persisted). It is a hard, complete degradation switch.
- **`MAX_TOOL_ITERATIONS`** (default `5`): hard cap on LLM↔tool round-trips per message. Hitting it raises a `ToolLoopLimitError`, surfaced to the user as a generic, user-safe "too many tool calls" message (category `tool_limit`).

## Architecture & invariants (read before editing)

- **`telegram/bot.py`** — the *only* module that knows about Telegram. Auth is an allow-list check (`_is_authorized`) done **in each handler** (this PTB build has no `Middleware` API). Unauthorised users are silently ignored — never reply to them. It calls `AgentService.process_message()` and never the OpenAI SDK. It also owns: `/start` `/new` `/help` `/status`, a `typing` keep-alive loop, and **long-reply chunking** (`split_into_chunks`, which must never lose content). The command list lives in `_COMMANDS` (single source of truth for the `/help` reply *and* the native Telegram command menu advertised via `set_my_commands` in `register_command_menu`). Startup hooks (command menu + DB init) are chained with `compose_startup_hooks`.
- **`agent/service.py`** — the channel-agnostic core (reusable for a future web/Discord/API). It holds a **per-conversation `asyncio.Lock`** (`conversation_lock`) so one chat is serialised while different chats run concurrently. Flow: acquire lock → load history → persist user turn → build context → **run the tool loop** (when `enable_tools`) *or* a single LLM call (when disabled) → persist assistant turn → return text. LLM failures become `AgentError` with a **generic, user-safe message** (no stack traces / keys / paths leak to Telegram); a `ToolLoopLimitError` maps to the `tool_limit` message. **Only the user turn and the final assistant turn are persisted** — the intermediate `tool_calls` / `tool` turns are not stored, so the schema is unchanged.
- **`agent/tool_loop.py`** — the phase-2.1 piece inserted between the service and the LLM. `run_tool_loop(llm, messages, registry, max_iterations)` calls the LLM, and if the result has `tool_calls` it appends the assistant tool-call message, runs each tool via the **registry** (never hardcoded `if name == ...`), appends `tool` result messages, and calls the LLM again — until a message with no tool calls (the final answer) or the iteration limit is hit (→ `ToolLoopLimitError`). It depends only on an LLM that accepts `tools=` (a `Protocol`) and a `ToolRegistry`; it knows nothing about Telegram, the DB, or the OpenAI SDK.
- **`tools/`** — provider-/channel-agnostic. `base.Tool` is an ABC: `name`, `description`, JSON-schema `parameters`, `async execute(arguments) -> str`, and `spec()` (the inner `function` block). `registry.ToolRegistry` does `register`/`add`/`get`/`names`, `to_openai_schema()` (list of `{"type":"function","function":{...}}`), and `execute(name, arguments)` — dispatching by name and converting a tool's exception into a JSON `{"error": ...}` result (so one bad tool can't kill the loop; an *unknown* name raises `ToolNotFoundError`, which the loop catches and turns into an error string). `tools/builtin/` holds `get_current_time` (no args), `echo` (`{"message": str}`), and `system_info` (hostname/platform/Python version via stdlib `socket`+`platform`, **no subprocess**); `build_default_tools()` assembles them.
- **`agent/context.py`** — `build_context(system, history, max_n)` = system prompt + most recent N **messages** (N is a *message* count, not tokens; `MAX_CONTEXT_MESSAGES`). `ChatMessage` carries optional `tool_calls` / `tool_call_id` (both `None` for every Phase-1 message, so `to_dict()` is unchanged there). This is the single place to swap in token-based context management later.
- **`llm/client.py`** — the *only* module that knows the OpenAI SDK. Wraps `AsyncOpenAI` in `complete(messages, *, tools=None, ...) -> LLMResult`. When `tools` is passed it is forwarded to `chat.completions.create`; a model reply's `tool_calls` are normalised into the canonical dict shape and set on `LLMResult.tool_calls`. Provider failures become one `LLMError` with a stable `category` (`timeout` / `http_error` / `connection` / `empty_response` / `error`). **A response with tool calls but no text is *not* an `empty_response`** (only blank content *and* no tool calls is). `stream=True` is accepted but raises `NotImplementedError`. Note: this is the *wire* client; the tool *loop* lives in `agent/tool_loop.py`, not here.
- **`database/`** — `models.py` (ORM), `session.py` (engine/session factory + `init_db`), `repository.py` (the only layer that touches ORM; handlers never write SQL). One Telegram chat = one conversation, keyed by `telegram_chat_id`. `Message.role` already allows `tool` (ahead of phase 2), but tool turns are never written.

### Gotchas that are easy to get wrong
- **This PTB build strips `CallbackContext` shortcuts**: `context.user` / `.chat` / `.message` (and `.user_id`/`.chat_id`/`.effective_user`) do **not** exist here — only `.application`, `.bot`, `.bot_data`, `.error`, `.user_data`, `.chat_data`, … Reading any of the stripped attributes raises `AttributeError` at runtime. Handlers therefore read the sender/chat/message from the **`Update` object** (`update.effective_user` / `.effective_chat` / `.effective_message`). This build also has **no `Middleware` API**, so auth is per-handler. `Chat`/`Update`/`User`/`Message` are frozen `TelegramObject`s (you can't set attributes on instances — patch `Chat.send_message` at the **class** level in tests).
- **OpenAI is a fork**: the installed `openai` uses `httpx2` (not plain `httpx`) as its HTTP layer. When mocking `client._client.chat.completions.create`, construct error responses with `httpx2.Response(...)` (see `tests/test_llm_client.py`).
- **SQLAlchemy**: sessions use `expire_on_commit=False`. Do **not** read lazy-loaded ORM attributes (e.g. `conversation.messages`) after the session closes — use the repository's scalar return types (`MessageRecord`). SQLite has FK enforcement turned **on** via a connect event (needed for cascade deletes on reset).
- **`conversations.id` uses `sqlite_autoincrement`** so a `/new` always yields a new, larger id (visible to the user). Don't remove it.
- **Tool logging never leaks secrets**: `tool_loop.py` logs only `tool requested: <name>` and `tool completed: <name> latency=<N>ms`. Never log the API key, Telegram token, `Authorization` header, or full message/tool-result bodies (the LLM client's `_safe()` deliberately strips request headers). Keep it that way when adding tools.
- **`ChatMessage` is defined twice** — one in `agent/context.py` (built for the agent, used by the tool loop) and one in `llm/client.py` (the wire type the client serialises). Both now carry the same optional `tool_calls`/`tool_call_id` fields and both `to_dict()` identically for a plain message. Keep them in sync if you change either.
- `data/` and its `.db` are created automatically at startup (`create_engine` makes the parent dir). Tests use in-memory SQLite via `StaticPool` — keep them that way (no real files).

## Testing

`tests/conftest.py` provides an in-memory `repo` fixture and `FakeLLM` / `RecordingLLM` fakes. **All 83 tests pass, all mocked** — nothing ever talks to the real LLM endpoint or Telegram (mock the SDK's `create` and the PTB handlers). Coverage:

- Phase 1: db init, create conversation, save message, load history, reset, context builder, unauthorized user, LLM client (incl. `httpx2` error construction), `process_message`, concurrency lock, Telegram handlers.
- Phase 2.1 tool runtime (7 required behaviours + extras), in `tests/test_tools.py`, `tests/test_tool_loop.py`, `tests/test_agent.py`, `tests/test_llm_client.py`:
  1. registry registration, 2. OpenAI schema generation, 3. tool execution (by name + unknown/error fallbacks), 4. normal chat without tools, 5. single tool call, 6. multiple tool calls (one turn + sequential rounds), 7. infinite-loop iteration cap. Plus: client `tools=` pass-through, `tool_calls` normalisation, the no-false-`empty_response` guarantee, and service-level "persist only user + final assistant" / `tool_limit` / "disabled stays a single call".

`FakeToolLLM` (in `test_tool_loop.py`) and `ScriptedToolLLM`/`AlwaysCallsToolLLM` (in `test_agent.py`) are small fakes that replay scripted `LLMResult`s — the loop is driven entirely off them.

## Deployment (Docker + CI) — done

- **`Dockerfile`**: `python:3.14-slim` + `uv` (pinned to the lockfile's generator version), builds from the committed `uv.lock` for exact locked deps. Runs as an unprivileged user; `/app/data` is the only writable path. **No `EXPOSE`** — the app is outbound-only (Telegram long polling + LLM API), there is nothing inbound. `UV_PROJECT_ENVIRONMENT=/opt/venv` pins the venv (uv's `sync` ignores `--python` for venv selection — verified).
- **`docker-compose.yaml`**: single service. Config from the **same `.env`** the local `uv run` uses (`env_file: .env`, git-ignored, never baked into the image). Persists SQLite via a **bind mount** `./data:/app/data` (shares the `data/` dir with the local run). `restart: unless-stopped`.
- **`.dockerignore`**: keeps `.env*`, `data/`, `.venv`, `tests/`, `.git`, etc. out of the image. **`README.md` is deliberately NOT ignored** — `uv sync` reads it (`pyproject` `readme = README.md`) and the build fails without it.
- **Host-uid mount**: the container runs as the host user (`user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`, from `.env`) so the bind-mounted `./data` keeps normal host permissions (owned by you, `755`) **and** is writable by the container — no `chown` needed. A bind mount takes the host dir's ownership over the image's `/app/data`, so a non-owning uid would hit `EACCES` on first DB create.
- **CI** (`.github/workflows/build-image.yml`): on every `v*` tag push, builds the image and pushes to **ghcr.io** (`ghcr.io/fibrecase/fibreagent-backend:<tag>` + `:<short-sha>`) using the built-in `GITHUB_TOKEN` with `packages: write`. Actions are on their Node-24 majors. Bump version in `pyproject.toml` + `src/fibrecase_agent_backend/__init__.py`, run `uv lock`, commit, `git tag -a vX.Y.Z`, `git push` + `git push origin vX.Y.Z`.

## Adding a tool (phase 2.1)

A tool is a `tools.base.Tool` subclass: set `name`, `description`, a JSON-schema
`parameters` dict, and implement `async execute(arguments) -> str`. Then register
it — either add it in `tools/builtin/__init__.py::build_default_tools()`, or pass
your own `ToolRegistry` into `AgentService` (which `main` builds from
`build_default_tools()` when `ENABLE_TOOLS=true`). That is the *entire* change:
the registry advertises it in the OpenAI schema and dispatches it by name.
**Do not** add `if name == "…"` branches anywhere — the registry is the single
dispatch point. Tool results must be short, human/model-readable strings; raise
on failure (the registry turns it into a JSON `{"error": ...}` the model sees).

## Current limitations (phase 2.1)

- **Three read-only built-ins only** (`get_current_time`, `echo`, `system_info`). No shell exec, file I/O, network scanning, SSH/Docker, or any state-changing/dangerous tool — by design.
- **Single-turn tool arguments** come from the model's `function.arguments` (parsed as JSON, forgiving of a dict or bad JSON). There is **no argument validation** against the declared schema before `execute`.
- **No permission/approval** step: the owner's allow-listed Telegram chat can already trigger any registered tool. With safe tools only this is fine; before adding anything state-changing, add a gate.
- **The tool loop has no per-tool timeout** (the LLM call has `OPENAI_TIMEOUT`, but a hung tool blocks the conversation lock).
- **Only `user` + final `assistant` turns are persisted**; the tool-call transcript is not stored, so it isn't replayable/auditable after the fact.
- Not implemented (out of scope for 2.1): MCP, RAG, Web Search, streaming, and the autonomous self-driven loop.

## Extending (phase 2.2+, not yet)

The runtime is now built and stable; the next additions should slot in as **Tool
Providers** behind the same `Tool`/`ToolRegistry` interface rather than touching
the service, LLM client, or Telegram layer:

- **MCP**: implement an `MCPClient` that fetches a remote server's tool list and
  wraps each as a `Tool` whose `execute()` forwards to the MCP call, then
  `registry.add(...)` them at startup. The loop is already tool-agnostic.
- **SSH / Docker / Pi**: same pattern — each a `Tool` (or a small provider that
  yields several), stdlib/subprocess kept *inside* the tool, never in the loop.
  **Gate any of these behind an explicit permission check first** (see limitations).
- **RAG / Web Search**: add as tools (a `search` tool) so the model decides when
  to use them; keep retrieval out of the loop.

Invariants that must hold for all of the above: no Telegram logic in the agent
service; no OpenAI-SDK imports outside `llm/client.py`; no secrets or message
bodies in logs; and `ENABLE_TOOLS=false` must remain a full Phase-1 degradation.
