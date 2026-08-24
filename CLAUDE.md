# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal, personal **Agent Backend** running on the owner's own server. Phase 1 does exactly one thing: let the owner talk to an OpenAI-compatible-LLM-backed Agent over **Telegram**, with **persistent conversations** that survive restarts. It has **no tools** — it can only chat.

The design goal is that this is *not* a "Telegram chatbot". It is layered from day one so that a Tool/MCP loop can later be inserted between the Agent service and the LLM client without touching the Telegram layer:

```
Telegram Adapter → Agent Service → LLM Client → Persistent Conversation (SQLite)
```

## Status

**Phase 1 is complete, tested, and shipped** — current version **v1.1.1** (tagged; a GitHub Action builds & pushes the image to **ghcr.io** on every `v*` tag push). Everything below is done, mocked-tested (50 tests), and verified running:

- Telegram long-polling adapter: allow-list auth, `/start` `/new` `/help` `/status`, typing keep-alive, long-reply chunking, graceful error handling.
- Channel-agnostic `AgentService` with per-conversation `asyncio.Lock` (serialise one chat, parallelise across chats).
- `OpenAIClient` (OpenAI-compatible) with user-safe LLM-error translation.
- Persistent SQLite conversations that **survive restarts** (verified across a simulated restart).
- **Docker deployment** (`Dockerfile` + `docker-compose.yaml`, single shared `.env`).

Only the "Extending (phase 2, not yet)" section remains unbuilt. Two runtime bugs were found and fixed *after* the initial implementation (both now covered by tests): the nested event-loop crash on startup, and the missing `CallbackContext` shortcuts (see Gotchas).

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

## Architecture & invariants (read before editing)

- **`telegram/bot.py`** — the *only* module that knows about Telegram. Auth is an allow-list check (`_is_authorized`) done **in each handler** (this PTB build has no `Middleware` API). Unauthorised users are silently ignored — never reply to them. It calls `AgentService.process_message()` and never the OpenAI SDK. It also owns: `/start` `/new` `/help` `/status`, a `typing` keep-alive loop, and **long-reply chunking** (`split_into_chunks`, which must never lose content). The command list lives in `_COMMANDS` (single source of truth for the `/help` reply *and* the native Telegram command menu advertised via `set_my_commands` in `register_command_menu`). Startup hooks (command menu + DB init) are chained with `compose_startup_hooks`.
- **`agent/service.py`** — the channel-agnostic core (reusable for a future web/Discord/API). It holds a **per-conversation `asyncio.Lock`** (`conversation_lock`) so one chat is serialised while different chats run concurrently. Flow: acquire lock → load history → persist user turn → build context → call LLM → persist assistant turn → return text. LLM failures are translated to `AgentError` with a **generic, user-safe message** (no stack traces / keys / paths leak to Telegram).
- **`agent/context.py`** — `build_context(system, history, max_n)` = system prompt + most recent N **messages** (N is a *message* count, not tokens; `MAX_CONTEXT_MESSAGES`). This is the single place to swap in token-based context management later.
- **`llm/client.py`** — the *only* module that knows the OpenAI SDK. Wraps `AsyncOpenAI` in `complete() -> LLMResult` and translates all provider errors into one `LLMError` with a stable `category` (`timeout` / `http_error` / `connection` / `empty_response` / `error`). Empty/blank model content is treated as `empty_response`. `stream=True` is accepted but raises `NotImplementedError` (phase 1 is non-streaming).
- **`database/`** — `models.py` (ORM), `session.py` (engine/session factory + `init_db`), `repository.py` (the only layer that touches ORM; handlers never write SQL). One Telegram chat = one conversation, keyed by `telegram_chat_id`.

### Gotchas that are easy to get wrong
- **This PTB build strips `CallbackContext` shortcuts**: `context.user` / `.chat` / `.message` (and `.user_id`/`.chat_id`/`.effective_user`) do **not** exist here — only `.application`, `.bot`, `.bot_data`, `.error`, `.user_data`, `.chat_data`, … Reading any of the stripped attributes raises `AttributeError` at runtime. Handlers therefore read the sender/chat/message from the **`Update` object** (`update.effective_user` / `.effective_chat` / `.effective_message`). This build also has **no `Middleware` API**, so auth is per-handler. `Chat`/`Update`/`User`/`Message` are frozen `TelegramObject`s (you can't set attributes on instances — patch `Chat.send_message` at the **class** level in tests).
- **OpenAI is a fork**: the installed `openai` uses `httpx2` (not plain `httpx`) as its HTTP layer. When mocking `client._client.chat.completions.create`, construct error responses with `httpx2.Response(...)` (see `tests/test_llm_client.py`).
- **SQLAlchemy**: sessions use `expire_on_commit=False`. Do **not** read lazy-loaded ORM attributes (e.g. `conversation.messages`) after the session closes — use the repository's scalar return types (`MessageRecord`). SQLite has FK enforcement turned **on** via a connect event (needed for cascade deletes on reset).
- **`conversations.id` uses `sqlite_autoincrement`** so a `/new` always yields a new, larger id (visible to the user). Don't remove it.
- `data/` and its `.db` are created automatically at startup (`create_engine` makes the parent dir). Tests use in-memory SQLite via `StaticPool` — keep them that way (no real files).

## Testing

`tests/conftest.py` provides an in-memory `repo` fixture and `FakeLLM` / `RecordingLLM` fakes. All 10 required behaviours are covered (db init, create conversation, save message, load history, reset, context builder, unauthorized user, LLM client, `process_message`, concurrency lock). **Never call the real LLM endpoint or Telegram in tests** — mock the SDK's `create` and the PTB handlers.

## Deployment (Docker + CI) — done

- **`Dockerfile`**: `python:3.14-slim` + `uv` (pinned to the lockfile's generator version), builds from the committed `uv.lock` for exact locked deps. Runs as an unprivileged user; `/app/data` is the only writable path. **No `EXPOSE`** — the app is outbound-only (Telegram long polling + LLM API), there is nothing inbound. `UV_PROJECT_ENVIRONMENT=/opt/venv` pins the venv (uv's `sync` ignores `--python` for venv selection — verified).
- **`docker-compose.yaml`**: single service. Config from the **same `.env`** the local `uv run` uses (`env_file: .env`, git-ignored, never baked into the image). Persists SQLite via a **bind mount** `./data:/app/data` (shares the `data/` dir with the local run). `restart: unless-stopped`.
- **`.dockerignore`**: keeps `.env*`, `data/`, `.venv`, `tests/`, `.git`, etc. out of the image. **`README.md` is deliberately NOT ignored** — `uv sync` reads it (`pyproject` `readme = README.md`) and the build fails without it.
- **Host-uid mount**: the container runs as the host user (`user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`, from `.env`) so the bind-mounted `./data` keeps normal host permissions (owned by you, `755`) **and** is writable by the container — no `chown` needed. A bind mount takes the host dir's ownership over the image's `/app/data`, so a non-owning uid would hit `EACCES` on first DB create.
- **CI** (`.github/workflows/build-image.yml`): on every `v*` tag push, builds the image and pushes to **ghcr.io** (`ghcr.io/fibrecase/fibreagent-backend:<tag>` + `:<short-sha>`) using the built-in `GITHUB_TOKEN` with `packages: write`. Actions are on their Node-24 majors. Bump version in `pyproject.toml` + `src/fibrecase_agent_backend/__init__.py`, run `uv lock`, commit, `git tag -a vX.Y.Z`, `git push` + `git push origin vX.Y.Z`.

## Extending (phase 2, not yet)

To add tools/MCP: insert the loop in `AgentService.process_message` (handle `tool_calls` from the model, run tools, feed `tool` results back until a final text), pass `tools=` through in `OpenAIClient`, and add an `MCPClient`. `messages.role` already allows `tool`. Do **not** put Telegram-specific logic in the Agent service, and do not import the OpenAI SDK outside `llm/client.py`.
