# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal, personal **Agent Backend** running on the owner's own server. Phase 1 does exactly one thing: let the owner talk to an OpenAI-compatible-LLM-backed Agent over **Telegram**, with **persistent conversations** that survive restarts. It has **no tools** — it can only chat.

The design goal is that this is *not* a "Telegram chatbot". It is layered from day one so that a Tool/MCP loop can later be inserted between the Agent service and the LLM client without touching the Telegram layer:

```
Telegram Adapter → Agent Service → LLM Client → Persistent Conversation (SQLite)
```

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
- **OpenAI is a fork**: the installed `openai` uses `httpx2` (not plain `httpx`) as its HTTP layer. When mocking `client._client.chat.completions.create`, construct error responses with `httpx2.Response(...)` (see `tests/test_llm_client.py`).
- **SQLAlchemy**: sessions use `expire_on_commit=False`. Do **not** read lazy-loaded ORM attributes (e.g. `conversation.messages`) after the session closes — use the repository's scalar return types (`MessageRecord`). SQLite has FK enforcement turned **on** via a connect event (needed for cascade deletes on reset).
- **`conversations.id` uses `sqlite_autoincrement`** so a `/new` always yields a new, larger id (visible to the user). Don't remove it.
- `data/` and its `.db` are created automatically at startup (`create_engine` makes the parent dir). Tests use in-memory SQLite via `StaticPool` — keep them that way (no real files).

## Testing

`tests/conftest.py` provides an in-memory `repo` fixture and `FakeLLM` / `RecordingLLM` fakes. All 10 required behaviours are covered (db init, create conversation, save message, load history, reset, context builder, unauthorized user, LLM client, `process_message`, concurrency lock). **Never call the real LLM endpoint or Telegram in tests** — mock the SDK's `create` and the PTB handlers.

## Extending (phase 2, not yet)

To add tools/MCP: insert the loop in `AgentService.process_message` (handle `tool_calls` from the model, run tools, feed `tool` results back until a final text), pass `tools=` through in `OpenAIClient`, and add an `MCPClient`. `messages.role` already allows `tool`. Do **not** put Telegram-specific logic in the Agent service, and do not import the OpenAI SDK outside `llm/client.py`.
