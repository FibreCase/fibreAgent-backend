# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal, personal **Agent Backend** running on the owner's own server. It lets the owner talk to an OpenAI-compatible-LLM-backed Agent over **Telegram**, with **persistent conversations** that survive restarts, and (phase 2.1) the Agent can use **tools** — currently `get_current_time`, `echo`, and `system_info` — via an OpenAI-style tool-calling loop.

The design goal is that this is *not* a "Telegram chatbot". It is layered from day one, and the Tool loop it promised is now inserted between the Agent service and the LLM client without touching the Telegram layer:

```
Telegram Adapter → Agent Service → Tool Loop Runtime → LLM Client → Persistent Conversation (SQLite)
   (→ AgentMessage: text/image)      ↑ drives Tool Registry (get_current_time / echo / system_info)
```

## Status

**Phase 1 is complete, tested, and shipped** (tagged **v1.1.1**; a GitHub Action builds & pushes the image to **ghcr.io** on every `v*` tag push), **Phase 2.1 (Tool Calling Runtime) is implemented, mocked-tested, and released** (v1.2.0 / v1.2.1 / v1.2.2), and **Phase 2.1.x (Multimodal Input Foundation) is implemented and mocked-tested** (this session). Phase 1 delivers:

- Telegram long-polling adapter: allow-list auth, `/start` `/new` `/help` `/status`, typing keep-alive, **Markdown→HTML rendering** of model replies, long-reply chunking, graceful error handling.
- Channel-agnostic `AgentService` with per-conversation `asyncio.Lock` (serialise one chat, parallelise across chats).
- `OpenAIClient` (OpenAI-compatible) with user-safe LLM-error translation.
- Persistent SQLite conversations that **survive restarts** (verified across a simulated restart).
- **Docker deployment** (`Dockerfile` + `docker-compose.yaml`, single shared `.env`).

Phase 2.1 adds, on top of the same layered design, a **tool-calling runtime** (enabled by default via `ENABLE_TOOLS=true`; set it to `false` to fully degrade to the Phase-1 chat-only path — which is then byte-for-byte unchanged):

- A provider-/channel-agnostic `tools/` package: `Tool` interface, `ToolRegistry` (register / OpenAI-schema / execute-by-name), and three safe built-ins (`get_current_time`, `echo`, `system_info` — stdlib-only, no subprocess).
- `agent/tool_loop.py::run_tool_loop()`: call LLM → if `tool_calls`, run each via the registry and feed `tool` results back → repeat until a final text answer or `MAX_TOOL_ITERATIONS` is hit.
- `OpenAIClient.complete()` now accepts `tools=` and surfaces `tool_calls` on the result; `AgentService` drives the loop and persists **only** the user turn + final assistant turn (intermediate tool turns are not stored).
- Two new config knobs: `ENABLE_TOOLS` (degrades fully to Phase 1 when `false`) and `MAX_TOOL_ITERATIONS` (default `5`).

Phase 2.1.x (Multimodal Input Foundation) lets **Telegram photos** (with or without a caption) reach the LLM, on top of the same layers, without touching the tool runtime. It introduces a **channel-independent content model** that every future input channel (web UI, camera, …) will normalise into:

- A new `agent/messages.py`: `AgentMessage(contents, source, metadata)` with `TextContent` / `ImageContent` parts (the `ContentPart` union is shaped so `FileContent`/`AudioContent`/… slot in later).
- `telegram/media.py`: the *only* place that fetches Telegram media — `normalize_message(msg, max_bytes)` turns a photo into `AgentMessage([ImageContent, TextContent?])`, downloading bytes **in memory** via `PhotoSize.get_file()` → `File.download_as_bytearray()`, sniffing the MIME from magic bytes, and enforcing the size cap. Failures raise `MediaError` (user-safe), never crashing the backend.
- `llm/message_converter.py`: maps an `AgentMessage` to the OpenAI `content` field — a plain `str` when text-only (unchanged phase-1 shape), or a `list` of `{"type":"text"}` / `{"type":"image_url"}` parts (image as a base64 `data:` URL) when an image is present.
- `AgentService.process_message()` now accepts `str | AgentMessage`; `ChatMessage.content` (in *both* `agent/context.py` and `llm/client.py`) is widened to `str | list[dict]`. History stays plain strings, so an image rides only in the **current** turn and is **not persisted** (a deliberate phase limitation).
- New config knob `MAX_IMAGE_SIZE_MB` (default `10`); Telegram handler filter widened to `TEXT | PHOTO`. Tool Calling and multimodal are **independent** — `ENABLE_TOOLS=false` still processes images.

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

One multimodal-input knob (phase 2.1.x):
- **`MAX_IMAGE_SIZE_MB`** (default `10`): a Telegram photo larger than this is refused with a user-safe "图片过大" message (category `image_too_large`) before anything reaches the LLM. The cap is enforced in `telegram/media.py` (the adapter), the single gatekeeper.

## Architecture & invariants (read before editing)

- **`telegram/bot.py`** — the *only* module that knows about Telegram. Auth is an allow-list check (`_is_authorized`) done **in each handler** (this PTB build has no `Middleware` API). Unauthorised users are silently ignored — never reply to them. It calls `AgentService.process_message()` and never the OpenAI SDK. `handle_message` now handles **text *and* photos**: it normalises the update into an `AgentMessage` via `telegram/media.py::normalize_message` (text → one `TextContent`; photo → `ImageContent` + optional caption `TextContent`), catches `MediaError` and replies with its user-safe message (never crashes), and passes the `AgentMessage` to the service. The message filter is `filters.TEXT | filters.PHOTO` (commands still excluded). It also owns: `/start` `/new` `/help` `/status`, a `typing` keep-alive loop, and **sending model replies as HTML** (`_send_long` renders the reply via `telegram/markdown.py` and sends each chunk with `parse_mode=HTML`; on a Telegram 400 "can't parse" it re-sends that chunk as **plain text** so a reply is never lost — `split_into_chunks` is still the plain-text fallback splitter and must never lose content). The command list lives in `_COMMANDS` (single source of truth for the `/help` reply *and* the native Telegram command menu advertised via `set_my_commands` in `register_command_menu`). Startup hooks (command menu + DB init) are chained with `compose_startup_hooks`.
- **`telegram/media.py`** — the *only* module that fetches Telegram media (phase 2.1.x). `normalize_message(msg, max_bytes)` → `AgentMessage`. `extract_image_message` takes the **largest** `message.photo[-1]` rendition, downloads it **in memory** via `PhotoSize.get_file()` → `File.download_as_bytearray()` (no temp file), then validates: size (→ `MediaError` `image_too_large` if over `max_bytes`) and MIME (magic-byte sniff for `image/jpeg`/`image/png`/`image/webp`, else `MediaError` `unsupported_mime`). A download failure is a `MediaError` (`download_failed`). It logs `message_id`/`content_type`/`mime_type`/`size_bytes` **only** — never the bytes, base64, or any secret. The Telegram `file_id`/`PhotoSize` never leave this module.
- **`telegram/markdown.py`** — converts the model's Markdown to Telegram's HTML subset for display (Telegram does not render Markdown). `to_telegram_html(text)` handles bold (`**`/`__`), italic (`*`/`_`), strikethrough (`~~`), inline code (`` ` ``, kept **verbatim** — emphasis/links/strikethrough are never applied inside it), fenced code blocks (``` ``` ```), links (`[x](https://…)`) and headings (`#`), and escapes `& < >`. A single `_` between word chars (e.g. `snake_case`, `config/system_prompt.txt`) stays literal. `to_telegram_html_chunks(text, limit)` splits into `HtmlChunk(text, html)` pieces that are **tag-balanced per chunk** — it splits *source* into blocks (a fenced code block is one atomic block; text splits at blank lines) *before* rendering, so a chunk never starts mid-`<pre>`/`**`. A fenced block larger than the limit stays its own chunk (sent as plain text on 400). Commands (`/help`/`/status`) stay plain text; only model replies go through this.
- **`agent/messages.py`** — the channel-independent content model (phase 2.1.x): `AgentMessage(contents: list[ContentPart], source, metadata)` with `TextContent(text)` and `ImageContent(data: bytes, mime_type, filename?)`. `ContentPart` is a `Union` so future `FileContent`/`AudioContent`/… extend it without touching the agent or converter. `AgentMessage.text` returns the joined text (what gets **persisted**); `has_image()` / `is_empty()` are the small helpers the service uses. The agent layer depends on these types, **never** on Telegram `Message`/`PhotoSize`/`file_id`.
- **`agent/service.py`** — the channel-agnostic core (reusable for a future web/Discord/API). It holds a **per-conversation `asyncio.Lock`** (`conversation_lock`) so one chat is serialised while different chats run concurrently. `process_message(conversation_id, user_message)` accepts a **`str` or an `AgentMessage`** (a bare string is normalised to a single `TextContent`; empty text short-circuits, exactly as before). Flow: acquire lock → load history → persist the user turn **text** → build context → **run the tool loop** (when `enable_tools`) *or* a single LLM call (when disabled) → persist assistant turn → return text. LLM failures become `AgentError` with a **generic, user-safe message** (no stack traces / keys / paths leak to Telegram); a `ToolLoopLimitError` maps to the `tool_limit` message. **Only the user turn and the final assistant turn are persisted** — the intermediate `tool_calls` / `tool` turns are not stored, and an **image is not persisted** either (it rides only in the current request; a deliberate phase limitation, not a bug).
- **`agent/tool_loop.py`** — the phase-2.1 piece inserted between the service and the LLM. `run_tool_loop(llm, messages, registry, max_iterations)` calls the LLM, and if the result has `tool_calls` it appends the assistant tool-call message, runs each tool via the **registry** (never hardcoded `if name == ...`), appends `tool` result messages, and calls the LLM again — until a message with no tool calls (the final answer) or the iteration limit is hit (→ `ToolLoopLimitError`). It depends only on an LLM that accepts `tools=` (a `Protocol`) and a `ToolRegistry`; it knows nothing about Telegram, the DB, or the OpenAI SDK.
- **`tools/`** — provider-/channel-agnostic. `base.Tool` is an ABC: `name`, `description`, JSON-schema `parameters`, `async execute(arguments) -> str`, and `spec()` (the inner `function` block). `registry.ToolRegistry` does `register`/`add`/`get`/`names`, `to_openai_schema()` (list of `{"type":"function","function":{...}}`), and `execute(name, arguments)` — dispatching by name and converting a tool's exception into a JSON `{"error": ...}` result (so one bad tool can't kill the loop; an *unknown* name raises `ToolNotFoundError`, which the loop catches and turns into an error string). `tools/builtin/` holds `get_current_time` (no args), `echo` (`{"message": str}`), and `system_info` (hostname/platform/Python version via stdlib `socket`+`platform`, **no subprocess**); `build_default_tools()` assembles them.
- **`agent/context.py`** — `build_context(system, history, max_n)` = system prompt + most recent N **messages** (N is a *message* count, not tokens; `MAX_CONTEXT_MESSAGES`). `ChatMessage.content` is **`str | list[dict]`**: a plain `str` for every persisted/history message, or a `list` of OpenAI content parts for the *current* multimodal turn (built by `llm/message_converter.py`). It also carries optional `tool_calls` / `tool_call_id` (both `None` for every Phase-1 message, so `to_dict()` is unchanged there). This is the single place to swap in token-based context management later.
- **`llm/client.py`** — the *only* module that knows the OpenAI SDK. Wraps `AsyncOpenAI` in `complete(messages, *, tools=None, ...) -> LLMResult`. When `tools` is passed it is forwarded to `chat.completions.create`; a model reply's `tool_calls` are normalised into the canonical dict shape and set on `LLMResult.tool_calls`. Provider failures become one `LLMError` with a stable `category` (`timeout` / `http_error` / `connection` / `empty_response` / `error`). **A response with tool calls but no text is *not* an `empty_response`** (only blank content *and* no tool calls is). `stream=True` is accepted but raises `NotImplementedError`. The wire `ChatMessage.content` is `str | list[dict]` (the `list` case is the multimodal current turn from `agent_message_to_openai_content`); both serialise straight through `to_dict()`. Note: this is the *wire* client; the tool *loop* lives in `agent/tool_loop.py`, and the `AgentMessage` → OpenAI `content` mapping lives in `llm/message_converter.py` (a pure function, no SDK import).
- **`database/`** — `models.py` (ORM), `session.py` (engine/session factory + `init_db`), `repository.py` (the only layer that touches ORM; handlers never write SQL). One Telegram chat = one conversation, keyed by `telegram_chat_id`. `Message.role` already allows `tool` (ahead of phase 2), but tool turns are never written.

### Gotchas that are easy to get wrong
- **This PTB build strips `CallbackContext` shortcuts**: `context.user` / `.chat` / `.message` (and `.user_id`/`.chat_id`/`.effective_user`) do **not** exist here — only `.application`, `.bot`, `.bot_data`, `.error`, `.user_data`, `.chat_data`, … Reading any of the stripped attributes raises `AttributeError` at runtime. Handlers therefore read the sender/chat/message from the **`Update` object** (`update.effective_user` / `.effective_chat` / `.effective_message`). This build also has **no `Middleware` API**, so auth is per-handler. `Chat`/`Update`/`User`/`Message` are frozen `TelegramObject`s (you can't set attributes on instances — patch `Chat.send_message` at the **class** level in tests).
- **OpenAI is a fork**: the installed `openai` uses `httpx2` (not plain `httpx`) as its HTTP layer. When mocking `client._client.chat.completions.create`, construct error responses with `httpx2.Response(...)` (see `tests/test_llm_client.py`).
- **This PTB `PhotoSize` has no `download_as_bytearray`** — only `get_file()` (→ a `telegram.File`, which *does* have `download_as_bytearray()`/`download_to_memory()`). In `telegram/media.py` the flow is `await photo[-1].get_file()` then `await file.download_as_bytearray()`. In tests, patch `PhotoSize.get_file` to return a fake `File` (see `_patch_download` in `tests/test_multimodal.py`); you cannot patch `download_as_bytearray` on `PhotoSize` directly (it isn't there).
- **A Telegram photo's text lives in `message.caption`, not `message.text`** (which is `None` for a bare photo). The handler filter is `TEXT | PHOTO`, and `telegram/media.py::normalize_message` reads the caption into a `TextContent`. An image-only message therefore persists an empty-string user turn — that is correct, not a bug (see limitations: images are not persisted).
- **SQLAlchemy**: sessions use `expire_on_commit=False`. Do **not** read lazy-loaded ORM attributes (e.g. `conversation.messages`) after the session closes — use the repository's scalar return types (`MessageRecord`). SQLite has FK enforcement turned **on** via a connect event (needed for cascade deletes on reset).
- **`conversations.id` uses `sqlite_autoincrement`** so a `/new` always yields a new, larger id (visible to the user). Don't remove it.
- **Logging never leaks secrets or media**: `tool_loop.py` logs only `tool requested: <name>` / `tool completed: <name> latency=<N>ms`; `telegram/media.py` logs only `message_id`/`mime_type`/`size_bytes`. Never log the API key, Telegram token, `Authorization` header, full message/tool-result bodies, **image bytes, or base64 image data** (the LLM client's `_safe()` deliberately strips request headers). Keep it that way when adding tools or media types.
- **`ChatMessage` is defined twice** — one in `agent/context.py` (built for the agent, used by the tool loop) and one in `llm/client.py` (the wire type the client serialises). Both now carry the same optional `tool_calls`/`tool_call_id` fields and both `to_dict()` identically for a plain message. Keep them in sync if you change either.
- `data/` and its `.db` are created automatically at startup (`create_engine` makes the parent dir). Tests use in-memory SQLite via `StaticPool` — keep them that way (no real files).

## Testing

`tests/conftest.py` provides an in-memory `repo` fixture and `FakeLLM` / `RecordingLLM` fakes. **All 123 tests pass, all mocked** — nothing ever talks to the real LLM endpoint or Telegram (mock the SDK's `create`, the PTB handlers, and `PhotoSize.get_file`). Coverage:

- Phase 1: db init, create conversation, save message, load history, reset, context builder, unauthorized user, LLM client (incl. `httpx2` error construction), `process_message`, concurrency lock, Telegram handlers.
- **Markdown → Telegram HTML** (`tests/test_telegram_markdown.py`): bold/italic/strikethrough/inline-code/fenced-code/links/headings, entity escaping (`& < >`), code-spans kept verbatim (no emphasis injected), snake_case left literal, chunk tag-balance invariant, plus handler-level "reply sent with `parse_mode=HTML`" and "400 → plain-text fallback so a reply is never lost".
- Phase 2.1 tool runtime (7 required behaviours + extras), in `tests/test_tools.py`, `tests/test_tool_loop.py`, `tests/test_agent.py`, `tests/test_llm_client.py`:
  1. registry registration, 2. OpenAI schema generation, 3. tool execution (by name + unknown/error fallbacks), 4. normal chat without tools, 5. single tool call, 6. multiple tool calls (one turn + sequential rounds), 7. infinite-loop iteration cap. Plus: client `tools=` pass-through, `tool_calls` normalisation, the no-false-`empty_response` guarantee, and service-level "persist only user + final assistant" / `tool_limit` / "disabled stays a single call".
- **Multimodal input (phase 2.1.x)** in `tests/test_multimodal.py` — the 9 required behaviours: (1) plain text → `TextContent` with unchanged phase-1 path, (2) Unicode emoji preserved verbatim, (3) photo → downloaded bytes → `ImageContent` (download mocked), (4) photo + caption → `ImageContent` + `TextContent`, (5) OpenAI conversion (right MIME, base64 round-trips to the bytes, text, and part order), (6) image + tool calling end-to-end, (7) `ENABLE_TOOLS=false` still delivers the image (no tools sent), (8) oversize image refused before the LLM, (9) memory-only lifecycle (no temp files) with the LLM failure surfaced user-safe.

`FakeToolLLM` (in `test_tool_loop.py`) and `ScriptedToolLLM`/`AlwaysCallsToolLLM` (in `test_agent.py`) and `ScriptedRecordingLLM` (in `test_multimodal.py`) are small fakes that replay scripted `LLMResult`s (raising an `Exception` entry instead of returning it) — the loop and multimodal service paths are driven entirely off them.

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

## Multimodal input limitations (phase 2.1.x)

- **Photos only.** `message.photo` is handled. Documents, stickers, video, audio, and GIFs are still dropped at the adapter (out of scope this phase — they are the `FileContent`/`AudioContent`/… the `ContentPart` union is already shaped to hold).
- **Images are not persisted.** Only the image *caption* (text) is stored; the image bytes are held in memory for the single request that uses them. After a restart the model cannot see an earlier image again. This is a deliberate scope limit, **not** a bug — a persistent *Attachment Storage* phase would change this.
- **MIME is sniffed, not validated deeply.** We accept `image/jpeg` / `image/png` / `image/webp` (by magic bytes, with a declared-type fallback); anything else is refused user-safely. No image *processing* (resize, downscale, EXIF strip).
- **One size gate.** `MAX_IMAGE_SIZE_MB` is enforced in the adapter before the bytes are handed on; the service does not re-check. The LLM endpoint must accept the base64 `data:` URL payload for the chosen MIME.
- **The backend never guesses model capability.** It sends a standard OpenAI multimodal request and lets the endpoint reject (an `http_error`, surfaced user-safely) if the model can't see. No `if model == ...` capability table.

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

**Multimodal extensions** build on the phase-2.1.x `AgentMessage` / `ContentPart`
foundation — add a new `ContentPart` subtype + a `telegram/media.py` branch for
the source + a renderer in `llm/message_converter.py`, and nothing else changes
(agent, tool loop, service all stay put):

- **File / Audio / Video / Sticker**: a `FileContent`/`AudioContent`/… plus a
  `Message` branch (e.g. `message.document` / `message.audio`) that downloads it
  in memory, and an OpenAI mapping for that part type.
- **Persistent Attachment Storage**: today images are in-memory-only and not
  persisted. To make history+images survive a restart, add an attachment store
  (e.g. a `data/attachments/` with content-addressed files + a `message_id →
  attachment` table) and have the *history* rehydration re-attach stored media —
  that is a separate, deliberate phase; do not bolt it onto this one.

Invariants that must hold for all of the above: no Telegram logic in the agent
service; no OpenAI-SDK imports outside `llm/client.py`; no secrets, message
bodies, **or image bytes** in logs; `ENABLE_TOOLS=false` remains a full Phase-1
degradation; and the backend never encodes model capabilities by model name.
