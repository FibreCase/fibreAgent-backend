# Developer reference

This is the long-form, English technical reference for the codebase. It holds the
per-module internals, the per-phase history, the "gotchas that are easy to get
wrong", the full config-knob reference, the test-coverage map, and the detailed
feature limitations that would otherwise bloat `CLAUDE.md`.

`CLAUDE.md` is the short, always-loaded guide (what this is + the rules and
conventions you must follow). Read `CLAUDE.md` first; come here when you need the
mechanics. The Chinese, user-facing docs (`status.md`, `tools.md`, `configuration.md`,
etc.) describe the *product*; this file describes the *implementation*.

Contents:

- [Status (phase by phase)](#status-phase-by-phase)
- [Per-module reference](#per-module-reference)
- [Gotchas that are easy to get wrong](#gotchas-that-are-easy-to-get-wrong)
- [Configuration knob reference](#configuration-knob-reference)
- [Test coverage map](#test-coverage-map)
- [Feature limitations](#feature-limitations)
- [Extending (not yet)](#extending-not-yet)
- [Deployment internals](#deployment-internals)

---

## Status (phase by phase)

Phase 1 is complete, tested, and shipped (tagged **v1.1.1**; a GitHub Action builds
& pushes the image to **ghcr.io** on every `v*` tag push). Every phase since is
implemented, mocked-tested, and released:

- **Phase 2.1 (Tool Calling Runtime)** — v1.2.0 / v1.2.1 / v1.2.2
- **Phase 2.2 (Multimodal Input)** — v1.3.0
- **Phase 2.3 (Persistent Image Attachment Storage)** — v1.4.0
- **Phase 2.4 (Attachment-Aware Context Management)** — v1.5.0
- **Phase 2.5 (Explicit Long-Term Memory)** — v1.6.0
- **Phase 3 (Tool Security)** — v1.7.0
- **Phase 4 (Remote MCP Tool Provider)** — v1.8.0
- **Phase 4.x (User-Level OAuth for MCP)** — implemented and mocked-tested on top of v1.8.0 (release pending)
- **Phase 5.1 (Read-Only Infrastructure Observation via SSH)** — implemented and mocked-tested on top of that (release pending)
- An **opt-in `exec` shell tool** (default off, always `ask`, with a static catastrophic-command backstop) — implemented and tested on top of that
- An **opt-in `file` toolset** (default off; `file_read`/`file_ls` read-only `allow`, the other nine tools always `ask`; confined to `FILE_WORKDIR` against `../` and symlink escape, precise replace + whole-file write/append + narrow non-overwriting file/directory operations) — implemented and tested on top of that
- **Streaming replies** (Bot API 10.0 `sendMessageDraft`; `ENABLE_STREAMING`, default on) — a live draft-preview in a private chat's compose box that animates as the model generates, with the full reply still delivered as a normal message; group/channel chats and a disabled knob degrade to the classic "typing…" + chunked reply. The Bot API 10.3 Stop button is a later phase (stop stays on `/stop`). Implemented and mocked-tested on top of that.
- **Phase 10 (Multi-Channel: QQ C2C plain text + commands)** — the first non-Telegram channel. A plain-text **C2C (private-chat) send/receive** adapter over the official `botpy` SDK, off by default (on when `QQ_APP_ID` is set). It normalises an incoming QQ C2C message into `AgentMessage(source="qq")`, calls the same channel-agnostic `AgentService.process_message`, and delivers the reply over the QQ websocket — so tool calling, the tool-security gate, context budgeting, and long-term memory all work unchanged. On top of plain send/receive it offers the **same slash-command surface** as the Telegram bot (core set + read-only `/mcp_status` + `/user_status`), **reply-quoting** of the user's message (`message_reference`, first chunk only), a **native command panel** (best-effort, idempotent create-or-update), a **global C2C custom menu** (`PUT /v2/menu`, best-effort, idempotent by replace), and **tool approval via QQ button cards** — a `qq/approval.py` broker (botpy-free) that, for a QQ-scoped `ask` request, sends an *active* C2C Markdown message with a `keyboard` of callback buttons, resolves the `INTERACTION_CREATE` the click produces (acked within 3 s), and is selected per-request by a scope-prefix `QQScopedApprovalRouter` co-resident with the Telegram broker in the one `AgentService`. Command *replies* render in **Chinese** (the Telegram adapter renders the same commands in English); delivery type follows the reply's shape — **simple** receipts as plain text (`msg_type=0`), **structured** displays as Markdown (`msg_type=2`). Conversations are keyed by a deterministic synthetic id in a reserved range disjoint from (and below) the schedule range. `qq/bot.py` is the only module that imports `botpy`; `qq/commands.py` and `qq/approval.py` are pure / botpy-free. **Limitations:** C2C plain text only (no group/guild, no images, no streaming draft), no `/start` (no QQ concept) and no `/mcp` / `/mcp auth` (OAuth is Telegram-bound). `deny` tools are still rejected on a QQ turn (`ask` is approvable via the button card). Implemented and mocked-tested on top of that.

### Phase 1

Telegram long-polling adapter (allow-list auth, `/start` `/new` `/help` `/status`,
typing keep-alive, Markdown→HTML rendering of model replies, long-reply chunking,
graceful error handling); channel-agnostic `AgentService` with per-conversation
`asyncio.Lock` (serialise one chat, parallelise across chats); `OpenAIClient`
(OpenAI-compatible) with user-safe LLM-error translation; persistent SQLite
conversations that survive restarts (verified across a simulated restart); Docker
deployment (`Dockerfile` + `docker-compose.yaml`, single shared `.env`).

### Phase 2.1 — Tool Calling Runtime

Enabled by default via `ENABLE_TOOLS=true`; set it to `false` to fully degrade to
the Phase-1 chat-only path (which is then byte-for-byte unchanged).

- A provider-/channel-agnostic `tools/` package: `Tool` interface, `ToolRegistry`
  (register / OpenAI-schema / execute-by-name), and three safe built-ins
  (`get_current_time`, `echo`, `system_info` — stdlib-only, no subprocess).
- `agent/tool_loop.py::run_tool_loop()`: call LLM → if `tool_calls`, run each via the
  registry and feed `tool` results back → repeat until a final text answer or
  `MAX_TOOL_ITERATIONS` is hit.
- `OpenAIClient.complete()` now accepts `tools=` and surfaces `tool_calls` on the
  result; `AgentService` drives the loop and persists **only** the user turn + final
  assistant turn (intermediate tool turns are not stored).
- Two new config knobs: `ENABLE_TOOLS` (degrades fully to Phase 1 when `false`) and
  `MAX_TOOL_ITERATIONS` (default `20`).

### Phase 2.2 — Multimodal Input Foundation

Lets **Telegram photos** (with or without a caption) reach the LLM, on the same
layers, without touching the tool runtime. It introduces a **channel-independent
content model** that every future input channel (web UI, camera, …) will normalise
into:

- A new `agent/messages.py`: `AgentMessage(contents, source, metadata)` with
  `TextContent` / `ImageContent` parts (the `ContentPart` union is shaped so
  `FileContent`/`AudioContent`/… slot in later).
- `telegram/media.py`: the *only* place that fetches Telegram media —
  `normalize_message(msg, max_bytes)` turns a photo into `AgentMessage([ImageContent, TextContent?])`,
  downloading bytes **in memory** via `PhotoSize.get_file()` → `File.download_as_bytearray()`,
  sniffing the MIME from magic bytes, and enforcing the size cap. Failures raise
  `MediaError` (user-safe), never crashing the backend.
- `llm/message_converter.py`: maps an `AgentMessage` to the OpenAI `content` field —
  a plain `str` when text-only (unchanged phase-1 shape), or a `list` of
  `{"type":"text"}` / `{"type":"image_url"}` parts (image as a base64 `data:` URL)
  when an image is present.
- `AgentService.process_message()` now accepts `str | AgentMessage`;
  `ChatMessage.content` (in *both* `agent/context.py` and `llm/client.py`) is widened
  to `str | list[dict]`. At this point an image rides only in the **current** turn
  (phase 2.3 persists it).
- New config knob `MAX_IMAGE_SIZE_MB` (default `10`). Tool Calling and multimodal are
  **independent** — `ENABLE_TOOLS=false` still processes images.

### Phase 2.3 — Persistent Image Attachment Storage

Makes those Telegram photos **survive restarts**: instead of living only in the
in-memory request, each image is written to a content-addressed blob store and its
metadata to a new `attachments` table, and **history** images inside the
`MAX_CONTEXT_MESSAGES` window are re-attached into the OpenAI context on later turns.

- A new channel-/protocol-/ORM-free `attachments/` package: `AttachmentStore` —
  content-addressed by the **SHA-256** of the raw bytes at `<root>/<digest[:2]>/<digest>`,
  **atomic** writes (temp file in the same dir + `fsync` + rename), **deduplicated**
  (identical bytes = one blob), and **path-traversal-safe** (digests must be exactly
  64 lowercase hex or are rejected). Reads of a missing/corrupt blob raise distinct,
  user-safe exceptions.
- A new `attachments` table (ORM) + `Message.attachments` relationship: one row per
  attachment, `message_id` FK → `messages.id` (`ON DELETE CASCADE`, indexed), `sha256`
  (indexed, shared across references), `storage_key`, `content_type` (`image`),
  `mime_type`, `size_bytes`, `filename`, `position` (stable in-message order),
  `created_at`. **No bytes / base64 / `file_id` / caption** are stored — only metadata.
- `AgentService` now persists image blobs + metadata when a message carries
  `ImageContent` (with compensation: an orphaned just-created blob is removed if the
  metadata write fails), and **rehydrates** in-window history images from the store in
  their original part order (missing/corrupt blob → the image part is skipped, its
  text is kept, a safe warning logged). The current turn still uses in-memory bytes;
  **only the window is truncated *before* rehydration**, so an out-of-window image is
  never read from disk or sent.
- `reset()` (`/new`) **reclaims** blobs: after dropping the conversation it deletes
  only blobs no longer referenced by *any* attachment, so a dedup'd blob shared with
  another conversation is kept. A failed/missing-blob delete is logged and never
  blocks the new conversation.
- New config knob `ATTACHMENT_STORAGE_PATH` (default `./data/attachments`, covered by
  Docker's `./data` bind mount).

### Phase 2.4 — Attachment-Aware Context Management

Adds a **conservative, model-agnostic estimated-token budget** on top of the
message-count window, so a very long message or many images can't overflow the real
context window of any OpenAI-compatible endpoint.

- `agent/context.py` is the single owner of context selection and stays **pure Python**
  (no Telegram, OpenAI SDK, SQLAlchemy, filesystem, or `AttachmentStore`). It gains a
  deterministic estimator (`estimate_text_cost` / `estimate_parts_cost` /
  `message_cost`: per-message envelope = 4 units, CJK codepoint = 1, contiguous ASCII =
  ceil(n/4), other Unicode = 1, each retained image = `CONTEXT_IMAGE_ESTIMATED_TOKENS`)
  and `plan_context()`, which — **before any attachment blob is read** — selects
  complete conversation turns (a `user` message + its following `assistant` messages;
  anomalous rows group safely) so the request fits **both** `MAX_CONTEXT_MESSAGES`
  and the token budget.
- Selection priority is fixed: system always kept; the **current user request always
  kept and its images never downgraded**; history as complete turns newest-first; a
  turn whose full (image) form won't fit is **downgraded to text-only** (its images
  are dropped and *never read from disk* — the caption/reply stay); if the text-only
  form also won't fit, stop (never reach past a newer turn); output is chronological
  with system first.
- `AgentService` builds lightweight `TurnCandidate`s (role/text/message id/attachment
  metadata — **no bytes**), calls the planner, and only **after** planning rehydrates
  the *selected* attachments. Downgraded / unselected turns go out as plain text or
  are omitted; their blobs are never read. When `system + current` alone exceed the
  budget it raises a user-safe `AgentError(category="context_limit")` and **does not
  call the LLM** (the user's text and already-persisted image are kept, matching
  other failure paths).
- Two new config knobs: `MAX_CONTEXT_ESTIMATED_TOKENS` (default `200000`) and
  `CONTEXT_IMAGE_ESTIMATED_TOKENS` (default `2000`). The estimate is explicitly
  **not** a provider billing token count and there is no model-specific tokenizer or
  capability table — if the endpoint still reports context-length overflow, the
  existing safe `http_error` path handles it.

### Phase 2.5 — Explicit Long-Term Memory

Adds a **minimal, controllable, cross-`/new`/restart** long-term memory on top of the
already-budgeted context. The owner *explicitly* saves discrete facts via
`/remember`; subsequent normal text messages deterministically retrieve relevant
memories (pure lexical search — no embeddings, no external service, no FTS5, no new
third-party deps) and inject them as a clearly-marked, non-instructional
"user-provided reference material" message. It is an **auditable SQLite memory
foundation** — *not* RAG/vector DB, *not* model auto-extraction.

- A new channel-/protocol-/ORM-free `memory/` package (`memory/text.py`):
  `normalize_text` (casefold + trim + collapse whitespace), `extract_terms`
  (CJK single-codepoint terms + contiguous ASCII word tokens, ASCII tokens <2 chars
  dropped, dedup), `rank_memories` (deterministic scoring: full normalized-query
  **substring hit** > **unique term-overlap count** > newer `updated_at` > larger
  `id`; same-scope only; a zero-score candidate is never returned), and
  `build_memory_reference_text` (a fixed backend-authored Chinese wrapper —
  `MEMORY_REFERENCE_HEADER` — plus one `- [memory #id] content` bullet per memory,
  raw content shown **verbatim**). `hash_scope` is the single implementation of the
  short, irreversible scope fingerprint used for safe logging.
- A new `memories` table (ORM) + `MemoryRecord` detached dataclass: `id`, `scope`
  (opaque channel-agnostic principal key, e.g. `telegram:<user_id>`, **indexed**),
  `content` (verbatim), `normalized_content` (search form), `created_at`,
  `updated_at`, `last_retrieved_at`. The repository gains `add_memory` /
  `list_memories` / `get_memory` / `delete_memory` / `clear_memories` /
  `count_memories` / `list_memories_for_search` / `mark_memories_retrieved`;
  **every by-id read/delete is filtered by `scope + id` in SQL** (a foreign id is
  indistinguishable from a missing one — no existence leak). `init_db`
  (`create_all`) adds the table to a fresh DB *and* to a pre-2.5 DB that only has
  `conversations`/`messages`/`attachments` — no data loss, no manual wipe, no Alembic.
- The Phase-2.4 planner is extended: `plan_context` gains `memories=` +
  `max_memory_estimated_tokens`, and `ContextPlan` gains `selected_memories` +
  `memory_cost`. The injected reference is a **single** `user`-role message (a
  `user` message, *not* a second `system` message — many OpenAI-compatible endpoints
  400 on two system messages, which is what broke memory-bearing turns), whose
  whole-message cost is committed to the token budget **before** history selection (it
  is scaffold, not conversation history, so it does *not* consume the message cap); a
  memory that would exceed the sub-budget (or total budget) is **skipped, never
  truncated**, and lower-scored ones are still tried. With no selected memories the
  plan is **byte-for-byte the phase-2.4 plan**.
- `AgentService.process_message(..., *, memory_scope: str | None = None)`: when a scope
  is supplied **and** the message has a non-empty text query, it retrieves + ranks
  memories (no LLM) and injects the reference message **right after the main system
  prompt, before history**. Only memories actually injected are stamped
  `last_retrieved_at` (a best-effort, non-fatal batch write before the LLM call — if
  it fails, the turn still goes out). A retrieval DB failure raises
  `memory_error` and aborts **before** the LLM is called. New commands:
  `/remember <content>` (trim + reject empty/too-long + per-scope limit), `/memories`,
  `/forget <id>` (foreign/missing → `memory_not_found`), `/forget all CONFIRM`
  (destructive clear requires the exact `CONFIRM` token). New error categories:
  `memory_invalid`, `memory_limit`, `memory_error`, `memory_not_found`,
  `memory_clear_confirmation` (English, matching the English commands).
- Four new config knobs: `MAX_MEMORIES_PER_SCOPE` (default `200`),
  `MAX_MEMORY_CHARS` (default `1000`), `MAX_RETRIEVED_MEMORIES` (default `5`), and
  `MAX_MEMORY_ESTIMATED_TOKENS` (default `3000` — a phase-2.4 estimated-unit
  **sub-budget** that must be `<= MAX_CONTEXT_ESTIMATED_TOKENS`, else `ConfigError`).

### Phase 3 — Tool Security

Puts a **strict, audited execution boundary in front of every tool call** — the
read-only built-ins `get_current_time` and `echo` still run exactly as before
(they declare `allow`), while `system_info` (also read-only, but deliberately
declared `ask` to exercise the approval flow) now prompts for approval — in every
case a call passes a fixed gate before it can touch anything:
**parse → registered? → policy → JSON-Schema validation → pre-execution audit
(fail-closed) → (if `ask`) one-time human approval → `asyncio.wait_for(execute,
timeout)` → terminal audit**.

- A `tools/policy.py` (`ToolPermission = allow | ask | deny`, `build_policy`,
  `advertised_names`) decides, per tool, whether a call may run freely, needs a human
  `Approve`/`Deny`, or is refused outright (a `deny` tool is **withheld from the
  OpenAI schema** so the model never even sees it, and is refused + audited if the
  model calls it anyway). Base default is `ask`; `get_current_time` and `echo`
  declare `allow` while `system_info` is deliberately `ask` (to exercise the approval
  flow). The only override source is `MCP_PERMISSIONS_FILE` (a dedicated JSON file,
  **MCP tools only** — the built-ins always ride their declared defaults and are never
  in it); see the [Configuration knob reference](#configuration-knob-reference) for
  its semantics and strict parsing.
- `tools/registry.py` now **schema-validates** with the maintained `jsonschema`
  library (not hand-rolled): `register()` rejects an invalid declared schema at
  register time, and `validate_arguments()` runs before `execute()` so a malformed /
  non-object / missing-required / wrong-type / extra-property `function.arguments` is
  refused with the stable `invalid_arguments` result and **never executed**. An
  invalid tool schema is a `ValueError` at `register()`.
- `tools/approval.py` is the channel-agnostic, stdlib-only **approval contract**
  (`ApprovalDecision`, `ApprovalRequest`, `ToolApprovalProvider`). Two channel-aware
  implementations plug into it, and `main.py` injects a single router as the
  service's `approval_provider`:
  - `telegram/approval.py::TelegramApprovalBroker` — the Telegram implementation. It
    presents Approve/Deny inline buttons in the *original* chat, binds each pending
    request to the exact **(principal, chat)** pair (principal compared by an
    irreversible `hash_scope` fingerprint — the raw user id is never held), enforces
    **one-time** consumption + expiry, awaits the decision on an `asyncio.Future`
    under `wait_for` (no busy-poll, no blocking), and cancels all pending approvals
    on shutdown. A foreign/other-chat/foreign-user click, a repeat click, a stale
    button, or a lapsed deadline **voids** the pending request immediately (its wait
    resolves `EXPIRED`) so the tool is never executed and the waiter unblocks.
  - `qq/approval.py::QQApprovalBroker` — the QQ implementation (botpy-free; the
    `client` is injected at `bind_client`). Same one-time / principal-bound /
    bounded-wait / drop-on-restart semantics, but the UI is a QQ **button card**: an
    *active* C2C Markdown message (no `msg_id`, so no passive-window limit or
    dedup collision) carrying a `keyboard` of two `action.type=1` callback buttons.
    The click arrives as an `INTERACTION_CREATE` (Intent bit `1<<26`, event `type=11`)
    dispatched to `client.on_interaction_create` → `handle_interaction`, which checks
    the clicker's openid against the request's principal via `hash_scope` and **acks
    within 3 s** via `PUT /interactions/{id}` (`code` 0/1) or the client spins.
    `qq/approval.py::QQScopedApprovalRouter` holds both brokers and dispatches by
    scope prefix (`qq:` → QQ broker, else Telegram) so ONE `AgentService` serves both
    channels.
- `tools/audit.py` + `database/audit.py` (`RepositoryToolAuditor`) + a new
  **`tool_audit_events`** append-only table record the lifecycle — `requested` /
  `denied` / `validation_failed` / `approval_requested` /
  `approval_approved|denied|expired` / `started` / `completed` / `timed_out` /
  `failed` — each with a **stable, non-echoing code** (`unknown_tool`,
  `invalid_arguments`, `tool_denied`, `approval_denied`, `approval_expired`,
  `tool_timeout`, `tool_execution_failed`, `audit_unavailable`, …). **Pre-execution**
  audit is a **fail-closed** gate: if the `requested` record can't be written, the
  tool does **not** run (`audit_unavailable`). **Terminal** audit failures are logged
  but never re-execute the tool. The table stores only `scope_hash` + tool name +
  event type + stable code + nullable call-id/iteration/latency — **never**
  arguments, results, exception text, or the raw scope/user id. `init_db`
  (`create_all`) adds the table to a fresh DB *and* to a pre-3 DB — no data loss,
  no wipe, no Alembic.
- `agent/tool_loop.py` runs each call through the gate in that exact order; a
  per-tool `asyncio.wait_for(..., tool_timeout_seconds)` cancels a hung tool
  (`tool_timeout`) instead of holding the conversation lock. The model always gets a
  stable, non-echoing JSON error result on any failure (no `if name == …` anywhere —
  the registry is still the only dispatch point).
- New command `/tool_audit [limit]` (default 20, clamped 1–50): a read-only,
  **scope-isolated** view of *your own* recent tool activity — it renders event
  id/time/tool/event/code/latency as HTML and shows a safe empty state; unauthorised
  senders get nothing; it never shows arguments, results, or the raw scope/user id.
- New config knobs: `TOOL_APPROVAL_TIMEOUT_SECONDS` (default `60`),
  `TOOL_TIMEOUT_SECONDS` (default `30`). (Per-tool permission overrides were later
  moved to the dedicated `MCP_PERMISSIONS_FILE`.) New error category:
  `tool_audit_error` (user-safe).

### Phase 4 — Remote MCP Tool Provider

Adds a **Model Context Protocol (MCP) tool provider** over **Streamable HTTP +
stdio**. At **startup** it connects to operator-configured MCP servers — **remote
Streamable HTTP** endpoints, or a **local stdio process** the backend spawns (a
`command` with `args`/`env`/`cwd`, no shell) and talks to over its stdin/stdout —
discovers their tools via `tools/list`, wraps each as a **namespaced local `Tool`**
(`mcp_<server>__<remote>`, default `ask`), and registers them into the **same**
`ToolRegistry` — so a model call to an MCP tool passes through the **entire existing
Phase-3 gate** (policy → JSON-Schema validation → fail-closed pre-audit → optional
one-time Telegram approval → `asyncio.wait_for` timeout → terminal audit). **MCP is
a Tool Provider, not a new execution path**: it does not touch the Agent runtime, the
service, the LLM client, or the Telegram message path — it only adds ordinary tools
to the registry the loop already drives.

- A new `mcp/` package (channel-/protocol-/DB-/OpenAI-SDK-free — it depends only on
  the MCP SDK and its HTTP client / stdio transport). `mcp/wrapper.py::McpTool` is a
  first-class `Tool`: `local_tool_name(server, remote)` →
  `mcp_<server>__<remote>` (the `mcp_` prefix + `__` separator make the two parts
  unambiguous, and because both the server name and the remote name come from
  `[A-Za-z0-9_-]`, the local name is itself a legal `[A-Za-z0-9_-]+` registry/policy/audit
  tool name; the remote segment is capped at 90 chars so the local name stays within
  the `String(128)` `tool_name` column). `McpTool.default_permission = ask`
  **unconditionally** (a remote tool is never auto-allowed just because the server
  claims it is read-only; the owner may still pin one `allow`/`deny` by namespaced
  name via `MCP_PERMISSIONS_FILE`). `parameters` maps the remote `input_schema`
  verbatim (defaulting to `{"type":"object","properties":{}}`) so the *existing*
  registry gate schema-validates it **before** any network request. `description` is
  a fixed, non-instructional `(🌐Remote)` marker prefix + the remote description (the
  server/remote *names* live in the local tool name, not here); the server's
  **instructions are never** surfaced. `approval_summary` returns just the tool's
  `description` (its **purpose**) — **never the (remote) arguments**; the
  (schema-validated) arguments are shown separately on the approval card as a
  readable-JSON `Arguments:` block (omitted for an argument-free call).
- `mcp/manager.py::McpManager` is built **only** when `ENABLE_TOOLS=true` *and* at
  least one server is configured (otherwise it does not exist and no MCP connection /
  stdio process is ever opened). In `_post_init` (after DB init) it `start()`s: per
  server it picks the transport — **http** (build an http client with the bearer
  header / OAuth auth → Streamable HTTP transport) or **stdio** (spawn the
  operator-configured process via `stdio_client(StdioServerParameters(command, args,
  env, cwd))`) — both yield the same `(read, write)` streams → `ClientSession` →
  `initialize()` [wait_for timeout] → `tools/list()` [wait_for timeout] → wrap each
  into an `McpTool` (atomic validation). Then it `add`s those tools to the registry
  (after the built-ins, so built-ins still sort first). Because `tool_loop.py`
  re-derives the advertised schema from `registry.names()` **every message** and the
  policy re-resolves **every call**, the MCP tools added at startup are
  **automatically** advertised and gated — no policy rebuild. **Fault isolation**:
  one server failing to connect (a stdio command that can't spawn counts) /
  initialize / list is marked `unavailable` (with a stable code) and skipped; **all
  other servers and the built-ins still start** — the bot **never** fails to start
  because an optional MCP server is down (or a stdio process won't spawn). **Atomic
  discovery**: a server's tools are all-or-nothing — any illegal tool name, illegal
  `input_schema`, or name collision (with a registered name or a sibling) drops the
  **whole server** (`mcp_invalid_tool`). **No reconnect**: a healthy session that
  later drops (a stdio process that exits counts) makes `call_tool` raise, mapped to
  `mcp_unavailable`; the next **process start** re-discovers. A stdio child process's
  full teardown is handled by the SDK's `stdio_client` context (close stdin → poll →
  SIGTERM/SIGKILL the process group); the manager just unwinds its `AsyncExitStack`
  as it already does.
- `execute()` does one thing: forward `arguments` to the connected session's
  `call_tool` and map the response to a **bounded, non-echoing** string. It does
  **not** do auth/approval/param-validation/timeout/audit itself — those are all in
  the Phase-3 tool loop, applied identically to every registered tool (MCP or
  built-in). Multi-part **text** content is joined with newlines in order (that is
  what the next LLM turn sees); everything else maps to a stable, non-echoing code:
  `mcp_unavailable` (transport/protocol/session error — the log records only the
  **exception class** and tool name, **never** the exception body, which may carry an
  endpoint or token), `mcp_tool_error` (remote `is_error`), `mcp_unsupported_result`
  (no `is_error` / non-text block / empty), `mcp_result_too_large` (text over
  `MAX_MCP_TOOL_RESULT_CHARS` — **not truncated, no prefix echoed**). It never raises
  a bare remote exception; if it did, the loop's `tool_execution_failed` is the
  backstop.
- **Startup-only, strictly-validated config** (`config.py`): the server list is read
  from `MCP_SERVERS_FILE` (a standalone JSON **array** file, the preferred source)
  when that is set, else the inline `MCP_SERVERS` (a JSON **array** string) — same
  validation either way. See the [Configuration knob reference](#configuration-knob-reference).
- New command `/mcp_status` (allow-list auth first): a **read-only** view of
  configured server status — each server's name, `available`/`unavailable`,
  discovered tool count, and the total available tool count. It does **not** connect
  / refresh / call the LLM or MCP; with no servers (or `ENABLE_TOOLS=false`) it shows
  "MCP: disabled"; it **never** shows URL / host / token / headers / tool description
  / schema / server instructions / failure detail. No new DB table — MCP calls are
  audited through the existing `tool_audit_events` table like any other tool.

### Phase 4.x — User-Level OAuth for MCP

Lets a **Telegram user** obtain a third-party OAuth credential (first consumer: Google
Calendar MCP) through Telegram, bound to that **user** — never to a conversation or
chat — so subsequent MCP tool calls to that server automatically carry *their own*
token. The infrastructure is **provider-agnostic**: the authorization-code flow,
state handling, storage, callback, refresh, and commands all speak to an
`OAuthProvider` abstraction; the **only** provider-name branch in the entire codebase
is the single registry in `main.py` (`_build_provider` — `google` reads
`GOOGLE_OAUTH_CLIENT_ID`/`SECRET`/`SCOPES` from env *there only*; a new provider =
implement the ABC + one line in that registry). OAuth does **not** enter the LLM
client, the `AgentService` core, the tool-loop semantics, the conversation store, or
Telegram history — it adds a credential layer under the existing MCP client.

- `mcp/auth/` (new package; channel/protocol-free — depends on httpx2 + the OAuth
  provider abstraction only): `provider.py` (`OAuthProvider` ABC —
  `authorization_url(redirect_uri, state)`, `exchange_code(code, redirect_uri)`,
  `refresh_token(refresh_token)` — plus `GoogleOAuthProvider`; provider HTTP errors
  become `OAuthProviderError` with **fixed, non-echoing** messages — the provider body
  is never surfaced or logged); `manager.py` (`OAuthManager`: `initiate` validates
  server→provider mapping + builds the `state`, `complete_authorization` runs the
  callback outcome logic, `valid_access_token` does the lazy refresh,
  `oauth_status`/`authenticated` are the token-free classifiers); `models.py`
  (records + stable `OAuthError` codes); `principal.py` (the `active_principal`
  ContextVar + `telegram_user_id_from_scope`); `oauth_auth.py` (`McpOAuthAuth`, the
  `httpx2.Auth` hook); `server.py` (the minimal callback HTTP app).
- **State**: `secrets.token_urlsafe(32)`, stored in the
  `oauth_authorization_states` table bound to `(telegram_user_id, chat_id, provider,
  mcp_server)` with an expiry (`OAUTH_STATE_TTL_SECONDS`, default 600). **Single-use**:
  the callback *consumes* the state (select+delete in one unit of work) **before**
  exchanging the code — a replay, an unknown state, an expired state, a state without
  a `code`, or a denied redirect all terminate with a fixed, safe outcome and **never**
  re-exchange. The credential's target triple comes **only** from the stored pending
  record — forged `telegram_user_id`/`provider`/`mcp_server` query parameters on the
  callback cannot redirect the credential (spec §28 wrong-user/provider/server).
- **Credential storage** (`database/oauth.py` implements the `OAuthStorage` ABC over
  two new tables, added by `create_all` to fresh **and** existing DBs):
  `oauth_credentials` — unique `(telegram_user_id, provider, mcp_server)`, i.e.
  **one active credential per triple**; re-authorization **upserts** (never
  duplicates); **user isolation in SQL** (a foreign user's lookup is indistinguishable
  from a missing one); `oauth_authorization_states` — the single-use pending states.
  `/new` (`reset_conversation`) **never** touches either table (regression-tested),
  and credentials **survive restarts** (file-backed SQLite; tested across a real
  engine re-open).
- **Auto token refresh**: `OAuthManager.valid_access_token(user, server)` returns the
  stored access token while valid (a token expiring within the last **60 s** still
  counts as valid — `_EXPIRY_SKEW`), otherwise refreshes via the provider: a
  **rotated** refresh token is persisted, a missing one **keeps the old**; on refresh
  **failure the credential is NOT deleted** — the status becomes "expired, reconnect"
  and re-`/mcp auth` is the only recovery path. A per-`(user, server)` lock makes
  concurrent callers refresh **exactly once**.
- **MCP client integration (the minimal point)**: an `MCP_SERVERS` entry may carry
  `"authentication": {"type": "oauth", "provider": "google"}` — **mutually exclusive**
  with `bearer_token_env` (both set → startup `ConfigError`; `oauth` requires a
  non-empty `provider`). `McpManager` takes an optional `oauth_auth_factory`; for an
  oauth server the http client is built with `auth=McpOAuthAuth` instead of a bearer
  header. `McpOAuthAuth` (`httpx2.Auth`) reads the `active_principal` ContextVar (set
  by the tool loop around `tool.execute()` to `telegram:<user_id>`, reset in
  `finally`), resolves the numeric user id, asks the manager for a valid token, and
  sets `Authorization: Bearer <token>` — **no principal (startup handshake) → no
  header**; an unresolvable/failing lookup sends the request **without** a header
  (never crashes the loop, never logs the token or user id). If the factory is absent
  for an oauth server the server is marked `unavailable` (stable **log-only** code
  `mcp_oauth_not_configured` — `status()` still exposes only
  `name`/`available`/`tool_count`) and never connects. Bearer/no-auth servers are
  byte-for-byte unchanged.
- **Callback server**: a minimal starlette app (starlette/uvicorn are transitive deps
  of the MCP SDK — **zero new dependencies**) with exactly one route,
  `GET /oauth/callback`, plus a fixed 404 for everything else. It runs **as a task
  inside PTB's own event loop** — started in `main._post_init` only when OAuth is
  configured, stopped first in `_post_shutdown`. Outcomes (success / denied / invalid
  / expired / error) reply with **fixed HTML text** and notify the user in their
  original chat via the injected async `notifier` (a notifier failure never changes
  the outcome). **Never logged**: access/refresh token, authorization code, client
  secret, the full callback URL (it carries `state`/`code` in the query) — tests assert
  the logger records contain none of them.
- **Telegram surface**: the command is `/mcp` (one command, argument-dispatched — there
  is **no** separate `mcp_auth` command): bare/other-argument `/mcp` is the
  **read-only** status view (server availability + the caller's **own** per-server
  OAuth state — `connected` / `authentication required` / `expired` / `not
  configured`; a status-lookup failure degrades to "required"; **never** another
  user's state; no URLs/tokens); `/mcp auth <server>` starts the flow and replies with
  an **inline URL button** (`InlineKeyboardButton` — the user never copies a URL) + an
  expiry note. Unauthorised senders are silent; an `initiate` crash is a user-safe
  "try again" without detail.
- **Config**: `OAUTH_CALLBACK_BASE_URL` (empty = OAuth **off**: no providers built, no
  manager, no callback listener; non-empty must be a **bare origin** — absolute
  `http(s)://` + host, no userinfo/path/query/fragment/trailing slash — else startup
  `ConfigError`), `OAUTH_CALLBACK_PORT` (default `8090`, `1..65535`),
  `OAUTH_STATE_TTL_SECONDS` (default `600`, `> 0`). The Google client id/secret/scopes
  are **not** `Config` fields at all — they exist only as env vars read inside
  `_build_provider`; a missing id/secret simply means "google provider not configured"
  (not an error; oauth servers then degrade to unavailable with the log-only code).
  OAuth is activated only when a callback base is set **and** at least one server
  declares `auth_type == "oauth"` **and** at least one provider builds.
- **Invariant**: OAuth is **user-level only** — no group/shared/global credentials, no
  multi-account or account switching, no Web UI/dashboard. The only listener the
  backend ever opens is the OAuth callback port, and only while OAuth is configured.

### Phase 5.1 — Read-Only Infrastructure Observation via SSH

Adds an **`infrastructure/` Tool Provider** (the same pattern as MCP — a provider that
yields ordinary tools, never a new execution path) over **host-key-pinned, key-only
AsyncSSH**. At **startup** it builds, for each operator-configured SSH target,
**three fixed, argument-free, read-only tools** —
`infra_<target>__host_status` / `__disk_status` / `__service_status` (host /
configured-mount disk / configured systemd-service status) — and registers them into
the **same** `ToolRegistry`, so each rides the **entire existing Phase-3 gate**
(policy → JSON-Schema validation → fail-closed pre-audit → optional one-time approval
(routed to the channel's broker) → `asyncio.wait_for` timeout → terminal audit). The target is **Linux +
systemd**.

- A new `infrastructure/` package (channel-/protocol-/DB-/OpenAI-SDK-free — it may use
  AsyncSSH + stdlib; `asyncssh` is **lazy-imported** inside `_connect` so an empty
  target list or `ENABLE_TOOLS=false` never imports it). `provider.py` exposes
  `local_tool_name(target, observation)` → `infra_<target>__<observation>`, three
  fixed remote-command **templates** (`_HOST_COMMAND` reads `/proc/uptime` /
  `/proc/loadavg` / `/proc/meminfo`; `_disk_command(mounts)` loops
  `df -kP -- "$_m" | awk '{ if ($2 ~ /^[0-9]+$/) print $2" "$3" "$4" "$5 }'` — the
  **portable POSIX** `df` (`-k` 1K blocks, `-P` one line per filesystem, both GNU *and*
  BusyBox `df`; the GNU-only `--noheadings`/`--output` are deliberately avoided so the
  command works on a Raspberry Pi / minimal image), the `awk` filter dropping the
  header + any wrapped continuation; `_service_command(services)` loops
  `systemctl show -p ActiveState --value -- "$_s"`), and three strict stdout parsers
  (`_parse_host` / `_parse_disk` / `_parse_service`) that raise a private `_ParseError`
  on any missing / duplicated / extra field, illegal number, or wrong record set. The
  **only** interpolated values are the startup-validated `mounts` / `services`, each
  shell-quoted via `_shquote` (single-quoted, `'` → `'\''`); the model can never name
  a host / path / service / command (the tools are argument-free and `execute` does
  `del arguments`).
- `InfraTool` is a first-class `Tool` with `default_permission = allow` — these
  observations are strictly **read-only** (fixed, argument-free commands over a
  host-key-pinned, key-only connection that can only read host / disk / service status
  and change nothing), so like the `get_current_time` / `echo` built-ins they run
  **without** a per-call approval; the tool is *not* an MCP tool, so it is **not**
  seeded into `MCP_PERMISSIONS_FILE` (an operator may still pin one `deny` by its
  namespaced name, overridable only via a direct `build_policy({...})` for tests).
  `approval_summary` returns a fixed, secret-free purpose line (`Read the
  {host|disk|service} status of infrastructure target '<name>' (read-only).`) —
  **never** the host / username / mount / service. `execute()` opens a **short-lived**
  connection via `_connect(target, connect_timeout_seconds)` (lazy `import asyncssh`;
  `asyncssh.connect(host, port, username=…, client_keys=[private_key_path],
  known_hosts=known_hosts_path, agent_path="", public_key_auth=True, password_auth=False,
  kbdint_auth=False, connect_timeout=…)` — host-key **pinned** to the explicit
  `known_hosts` file, **key-only**, SSH agent off, no password/keyboard-interactive,
  `known_hosts`/`client_keys` **never `None`**), runs the fixed command, and closes the
  connection in a `finally` (closed even if cancelled). It renders the parsed data as
  compact JSON (`_render`) bounded by `MAX_INFRA_TOOL_RESULT_CHARS`; **any** failure —
  connect/auth/host-key failure, non-zero exit, non-empty stderr,
  malformed/empty/oversized output — maps to one of three stable, non-echoing codes:
  `infra_unavailable` / `infra_invalid_response` / `infra_result_too_large`. It never
  raises a bare asyncssh exception (the loop's `tool_execution_failed` is the
  backstop), and the target's host, private-key path, known-hosts path, username, mount
  paths, command, stdout/stderr are **never** returned to the model, logged, or audited
  (the warning log carries only the tool name + stable code + the exception **class**
  `type(exc).__name__`, never the message). `build_infra_tools(targets, *,
  connect_timeout_seconds, max_result_chars)` yields `targets × 3` tools in stable
  order.
- `main.py` builds `self.infra_tools = build_infra_tools(config.infra_ssh_targets, …)`
  **only** when `config.enable_tools and config.infra_ssh_targets` (else `[]` — no
  provider, no SSH connection ever, `asyncssh` never imported). In `_post_init`
  (after the MCP `registry.add`) it `registry.add(*self.infra_tools)` **atomically**
  (any `ValueError` → `ConfigError` naming the colliding tool), and logs the infra tool
  count. The MCP permissions-file reconcile still passes **only**
  `[t.name for t in mcp_manager.tools()]` — infra tools are never seeded there.
- **Startup performs no SSH / network probe**: building the tools is pure string work;
  the private-key and known-hosts **files** are only checked to *exist* at config-load
  (a botched secret mount fails fast), their contents never read into config or logs.
  SSH happens **only** inside a run tool call (the read-only tools default to `allow`,
  so an approved call is no longer required — but an operator pinning one
  `ask`/`deny` still gates it as usual).
- **`/infra_status`** (`telegram/bot.py`, allow-list auth first): a **read-only** view
  of the *configured* targets — each target's name + its three tool names +
  "configured (3 tools, read-only)", plus the total. It does **not** connect, probe
  reachability, or call the LLM; it states it shows nothing about reachability; with
  no targets (or `ENABLE_TOOLS=false`) it shows "Infrastructure: disabled". It
  **never** shows host / port / username / key path / known-hosts path / mount path /
  service / command — target *name* + the three tool *names* only.
  `("infra_status", "Show configured infra targets")` is in `_COMMANDS`.
  **`/user_status`** is an allow-listed read-only command (behind `_is_authorized`,
  like `/status`) that returns **the caller's own** `user_id` + `chat_id` so they can
  fill a schedule's `receiver.telegram`; unauthorised senders are ignored and the
  values are user-facing in-chat but **never** logged.
- **Config** (strict, fail-fast at startup, `config.py`): the target list is a JSON
  **array** (empty = off), read from the **default file
  `config/infra_ssh_targets.json`** when present, an explicit
  `INFRA_SSH_TARGETS_FILE` when set (winning over the default and the inline
  `INFRA_SSH_TARGETS`), or the inline `INFRA_SSH_TARGETS` when no file is present (a
  set-but-missing/blank `INFRA_SSH_TARGETS_FILE` and a present-but-blank default file
  are both a `ConfigError` — see the [Configuration knob reference](#configuration-knob-reference)).

### The `exec` shell tool (opt-in, on top of Phase 3)

The more **general** of the two **state-changing** built-ins: it runs a single
`/bin/sh -c <command>` (full shell — pipes, redirection, `&&`) and returns
`{exit_code, stdout, stderr}`. It is **off by default** (`ENABLE_EXEC_TOOL=false`),
so the default deployment stays subprocess-free, and it always declares `ask` —
every call needs a one-time human Approve. Its `approval_summary` is a fixed,
argument-free purpose line that never echoes the command; it additionally overrides
the optional `approval_detail` hook so the approval card shows the command as a
**bash command block** — the exact command verbatim under a `$` prompt, a multi-line
command's newlines preserved — **in place of** the generic `Arguments:` JSON (rendered
under an `Action:` label, and labelled `bash` via the `approval_language` hook so
Telegram highlights it as shell rather than guessing) — the detail is plain text (the
provider HTML-escapes + length-bounds it, wraps it in `<pre><code>`, and drops it on
card finalisation exactly like the JSON block). Defence in depth, all *inside*
`execute` (the tool loop is untouched):

1. A **static backstop** — a new pure module `tools/exec_policy.py`
   (`CORE_DENY_PATTERNS`, `compile_denylist`, `check_exec_policy`) vetoes a small set
   of catastrophic command shapes (recursive `rm` of `/`/`$HOME`,
   `--no-preserve-root`, fork bombs, `curl`/`wget | sh`, `dd`/`mkfs`/raw block-device
   writes, `shutdown`/`reboot`/`halt`/`init 0/6`, `chmod 777 /`) *before* any spawn —
   **even after the owner approves** (the guard against mis-approval).
2. **Full shell via an argument vector** — `create_subprocess_exec("/bin/sh", "-c",
   command, …)`, never `shell=True`, so there is no second shell to escape into.
3. **Cancellation-safe process-group kill** — `start_new_session=True` puts `sh -c`
   and all descendants in one process group, and on the loop's
   `TOOL_TIMEOUT_SECONDS` timeout (which cancels the coroutine) or a turn shutdown the
   whole group is `SIGKILL`'d (no orphaned children).
4. **Output bounding** — stdout / stderr are tail-truncated to
   `MAX_EXEC_TOOL_RESULT_CHARS` with a fixed `[N chars … truncated]` marker (a
   deliberate departure from the MCP/infra cap→error idiom, because this is the direct
   result of a command the owner already approved).

A **non-zero exit is a successful run** (returned as JSON so the model can reason
about it), not an error. The only exec-specific result codes are
`exec_policy_deny` and `exec_spawn_failed`, both **returned** (not raised) so the
specific code reaches the model. **The command and its stdout/stderr go to the model
only — never logged, never audited** (the audit table structurally stores only
name/code/latency + hashed scope). Four config knobs (`config.py`):
`ENABLE_EXEC_TOOL` (default `false`), `MAX_EXEC_TOOL_RESULT_CHARS` (default `8000`,
`>= 1`), `EXEC_WORKDIR` (fixed CWD, must be an existing directory, `None` = process
cwd), and `EXEC_POLICY_DENY_PATTERNS` (JSON array, add-only — the operator may add
catastrophic-command regexes but never remove the core list; a bad regex is a startup
`ConfigError`). The numeric / workdir knobs are validated **only when enabled**; the
deny patterns are always parse-validated (fail-closed). `main.py` registers `exec` in
`build_default_tools(...)` **only when** `ENABLE_TOOLS` and `ENABLE_EXEC_TOOL` are
both true.

### The `file` toolset (opt-in, on top of Phase 3, in `tools/builtin/file.py`)

The second, **narrower** of the two state-changing local capabilities: where `exec`
is "run an arbitrary command", the `file` toolset lets the model do file/directory
operations — read, list, precise-edit, write, append, move, copy, create, delete,
touch — *without writing any shell*. It is **eleven tools** (a shared `_FileTool`
base holds the confinement root + helpers; each is a first-class `Tool`,
`additionalProperties: false`): `file_read` (UTF-8 file content, tail-truncated) and
`file_ls` (directory entries, dirs `/`-suffixed) both declare **`allow`** (read-only,
no per-call approval); `file_edit` (swap the **unique** `old_string` for
`new_string`, or every occurrence with `replace_all`; `new_string` may be empty =
delete that span), `file_write` (create a file or replace its **entire** content —
shell `>`), `file_append` (append content, creating the file if absent — shell
`>>`), `file_mv` (move/rename a file or dir; target must not exist), `file_cp` (copy
a file or directory tree; dirs need `recursive=true`; target must not exist),
`file_rm` (delete a **regular file** only — never a directory), `file_mkdir` (create
a dir; `parents=true` makes intermediates), `file_rmdir` (delete an **empty**
directory only), and `file_touch` (create an empty file or update mtime) all declare
**`ask`** — every call needs a one-time human Approve. Single-path tools use
`required=["path"]`; `file_mv`/`file_cp` use `required=["source","target"]`;
`file_edit` uses `required=["path","old_string","new_string"]` with
`old_string`/`new_string` bounded by `maxLength`; `file_write`/`file_append` use
`required=["path","content"]` with `content` bounded by `maxLength`. It is **off by
default** (`ENABLE_FILE_TOOL=false`), so the default deployment stays write-free.
`file_edit`, `file_write`, and `file_append` override the optional `approval_detail`
hook so the approval card shows the change as a **faithful `Action:` block rendered
in git-diff style** — a `📄 File:` / `🔁 Operation:` header, then
`--- a/<path>` / `+++ b/<path>`, with `file_edit` showing each `old_string` line
`-`-prefixed and each `new_string` line `+`-prefixed (newlines preserved; an empty
`new_string` is a pure deletion with no `+` lines), `file_write` showing every
`content` line `+`-prefixed (a pure addition — the existing content is discarded,
matching `>`), and `file_append` showing the `content` to be added `+`-prefixed
under a `+++ b/<path>` header (the existing content is *not* dumped — the owner can
read it with `file_read`) — **in place of** the generic `Arguments:` JSON, and
labelled `diff` via the `approval_language` hook so Telegram highlights it as a diff
rather than guessing (the detail is plain text; the provider HTML-escapes +
length-bounds it, wraps it in `<pre><code>`, and drops it on card finalisation
exactly like the JSON block). The other tools override only `approval_summary` (a
fixed, argument-free purpose line) and ride the generic `Arguments:` JSON block.
Defence in depth, all *inside* each `execute` (the tool loop is untouched):

1. **Path confinement — the core safety property** — `_resolve` resolves every
   `path` / `source` / `target` (collapsing `..` *and* following **symlinks**) and
   refuses anything that does not land inside `FILE_WORKDIR` *before any I/O*
   (`file_path_escape`), so a `../` escape, an out-of-root absolute path, or a symlink
   pointing out of the root is **never read or written even after the owner approves**
   (this covers `file_write`/`file_append` too, so a shell-`>`-style write cannot
   target a file outside the root).
2. **Narrow verbs, no tree clobber** — `file_rm` never deletes a directory
   (`file_is_directory`), `file_rmdir` only an empty one (`file_not_empty`), and
   `file_mv`/`file_cp` refuse an existing target (`file_already_exists`) — so the model
   cannot delete or rename a whole tree. (`file_write`/`file_append` *do* replace or
   extend a single file's content, which is exactly their `>` / `>>` purpose — but they
   can only ever name one file inside the root.)
3. **Atomic write** — `file_edit` and `file_write` write new content to a same-directory
   `tempfile.mkstemp` + `fsync` + `os.replace`, so a mid-write crash never leaves a
   half-written file (the attachment-store / permissions-file idiom). `file_append`
   reads the existing content (if any) and atomically writes the concatenation, so an
   append is crash-safe too (old content or full new content, never a half-appended
   tail).
4. **Bounding** — a `file_read` result is tail-truncated to `MAX_FILE_READ_CHARS` with
   the `[N chars … truncated]` marker (truncation, not an error); `file_ls` entries are
   capped at `MAX_FILE_LIST_ENTRIES` (extra entries dropped, a `truncated` flag set);
   `file_edit`'s `old_string`/`new_string` are bounded by `MAX_FILE_STRING_CHARS`
   (also the schema `maxLength`); `file_write`'s/`file_append`'s `content` is bounded
   by `MAX_FILE_CONTENT_CHARS` (also the schema `maxLength`), and `file_append`
   additionally enforces `MAX_FILE_CONTENT_CHARS` on the **resulting** file size
   (`existing + content` → `file_result_too_large`) so a large pre-existing file cannot
   be grown past the cap.

`FILE_WORKDIR` is **required** when the toolset is enabled (must be an existing
directory, else a startup `ConfigError`) — deliberately stricter than the optional
`EXEC_WORKDIR`, because a confinement root is the toolset's security premise. The
only file-specific result codes are `file_path_escape`, `file_not_found`,
`file_not_a_file`, `file_not_a_directory`, `file_is_directory`, `file_read_failed`,
`file_invalid_path`, `file_invalid_args`, `file_not_replaced`, `file_not_unique`,
`file_write_failed`, `file_result_too_large`, `file_not_empty`, `file_already_exists`,
and `file_fs_failed`, all **returned** (not raised) so the specific code reaches the
model. **The path, file content, and old/new strings go to the model only — never
logged, never audited.** Six config knobs (`config.py`): `ENABLE_FILE_TOOL` (default
`false`), `FILE_WORKDIR` (required existing dir when enabled), `MAX_FILE_STRING_CHARS`
(default `2000`, `>= 1`, also the schema `maxLength`), `MAX_FILE_READ_CHARS` (default
`8000`, `>= 1`), `MAX_FILE_LIST_ENTRIES` (default `1000`, `>= 1`), and
`MAX_FILE_CONTENT_CHARS` (default `20000`, `>= 1`, also the schema `maxLength` and the
`file_append` result-size cap); the five non-flag knobs are validated **only when
enabled**. `main.py` registers the toolset in `build_default_tools(...)` **only when**
`ENABLE_TOOLS` and `ENABLE_FILE_TOOL` are both true (added last, after `exec`).

---

### Streaming replies (Bot API 10.0 `sendMessageDraft`)

A **private** chat with `ENABLE_STREAMING=true` (default) shows a live Telegram
*draft* in the compose box that animates as the model generates, **in parallel
with** the "typing…" keep-alive — the typing action is the fallback that stays
visible if the draft can't be shown (the bot isn't on Telegram's streaming
allowlist, so `send_message_draft` is rejected fail-soft). Group/channel chats and
a disabled knob always degrade to the classic "typing…" + chunked final reply.
The full reply is still **always** delivered as a normal message afterwards — the
draft is a *preview only*.

**The channel-agnostic `on_text_delta` seam.** Streaming is expressed as a callback
that is threaded down three layers, keeping `AgentService` free of any transport
import and `process_message`'s `-> str` return shape intact:

1. **`llm/client.py::LLMClient.complete`** now takes
   `on_text_delta: Callable[[str], Awaitable[None]] | None = None`. `None` → the
   unchanged non-streaming path. Set → the request is sent with `stream=True` and
   `complete` async-iterates `client.chat.completions.create(..., stream=True)`,
   accumulating `chunk.choices[0].delta.content` and awaiting
   `on_text_delta(<accumulated-so-far text>)` on every non-empty content piece.
   `delta.tool_calls` fragments (keyed by `.index`; the first fragment supplies
   `id`/`type`/`name`, later ones append `arguments`) are reassembled via
   `_accumulate_tool_call_fragment`, so a streamed tool-call turn yields the same
   `LLMResult.tool_calls` a non-streaming turn would. The four exception→`LLMError`
   mappings (timeout / http_error / connection / empty_response) are unchanged;
   `usage` is `None` on the streaming path (a draft is not a billable final
   completion). `complete` still returns the full `LLMResult` — `process_message`
   persists it exactly as before.
2. **`agent/tool_loop.py::run_tool_loop`** and **`agent/service.py::process_message`**
   each take the same `on_text_delta` and forward it to the LLM `complete`. Because
   only content-bearing *final* turns emit `delta.content` (a tool-call turn's
   `content` is empty), text is streamed only on the answer turn, automatically.
   **Forwarding is conditional**: the callback is passed to `complete` *only when it
   is not `None`*, so an LLM client (or a test fake) that has no notion of
   `on_text_delta` still works when a caller doesn't stream.
3. **`telegram/bot.py`** (the only layer that knows about Telegram) builds the
   callback via `_DraftStreamer` and hands it to
   `service.process_message(..., on_text_delta=streamer.on_delta)`. It is the
   **only** place that decides *when* a chat streams:
   `streaming = bool(config.enable_streaming) and chat.type == ChatType.PRIVATE`.

**`_DraftStreamer`** (in `telegram/bot.py`) coalesces the burst of per-token deltas
into throttled, fail-soft `send_message_draft(chat_id, draft_id, text)` updates:

- **Throttle**: a delta is pushed only if `>= DRAFT_REFRESH_SECONDS` (0.3 s) since
  the last push; `finalize(text)` does one trailing push of the *complete* reply so
  the preview ends showing the full answer before the real message lands.
- **Preview cap**: `send_message_draft` is called with the *tail* of the
  accumulated text (`_tail_preview`, capped at `CHUNK_SIZE`) — a draft only shows
  its tail, and we never send an oversized text. The **final** message is delivered
  whole via `_send_long` regardless of length.
- **Fail-soft**: a `TelegramError` from a draft update (the most common cause — the
  bot not being on Telegram's streaming allowlist) is logged *without* a traceback
  and swallowed. The turn keeps running and the full reply is always sent as a
  normal message, so a draft failure costs the user nothing.
- **Privacy**: a push logs only the chat id and the *class* of any error — **never
  the draft body** (message content must not reach the logs/audit, per the privacy
  invariant).
- **Cancellable**: `on_delta` deliberately does **not** catch `CancelledError`. A
  `/stop` mid-generation cancels the turn; the cancellation propagates through the
  in-flight `await on_text_delta(...)` and `handle_message`'s existing
  `except asyncio.CancelledError` posts the "⛔️ **Interrupted.**" notice.
- **Typing keep-alive runs in parallel**: the streaming branch also wraps
  `process_message` in `_with_typing`, so the "typing…" action fires alongside the
  draft. The draft is the *preferred* indicator; the typing action is the *fallback*
  that stays visible when the draft can't be shown (the bot is not on Telegram's
  streaming allowlist, so `send_message_draft` is rejected fail-soft). Without this,
  such a deployment would have **no** "the bot is working" feedback at all. When the
  draft *can* be shown the typing action is at worst a harmless duplicate (and if
  Telegram rejects a typing action while a draft is up, `_typing_loop` already
  swallows that error).
- Each streaming turn gets a fresh positive, non-zero `draft_id` from a
  module-level `itertools.count(1)`. The draft is ephemeral (~30 s after the last
  update), so ids are never persisted.

**The Bot API 10.3 Stop button is a later phase.** PTB 22.8 exposes
`send_message_draft` but **not** the 10.3 stop-button primitives (`can_stop` /
`keep_on_stop` / `MessageGenerationStopped`). So the interactive *stop* path is the
existing `/stop` command (which cancels the in-flight turn), not a per-message Stop
button. Adding the 10.3 button is a follow-on once PTB ships those APIs.

**The scheduler path stays non-streaming.** `main.py::_run_schedule` calls
`process_message` **without** `on_text_delta`, so scheduled (cron) runs always use
the non-streaming path and deliver their notification via `deliver_markdown` — there
is no compose-box venue for a scheduled run, and no callback is wired.

---

## Per-module reference

The one-paragraph-per-module internals. `CLAUDE.md` keeps the module *map* and the
*dependency rules*; this is where the "what actually happens" lives.

- **`telegram/bot.py`** — the *only* module that knows about Telegram. Auth is an
  allow-list check (`_is_authorized`) done **in each handler** (this PTB build has no
  `Middleware` API). Unauthorised users are silently ignored — never reply to them. It
  calls `AgentService.process_message()` and never the OpenAI SDK. `handle_message`
  handles **text *and* photos**: it normalises the update into an `AgentMessage` via
  `telegram/media.py::normalize_message` (text → one `TextContent`; photo →
  `ImageContent` + optional caption `TextContent`), catches `MediaError` and replies
  with its user-safe message (never crashes), and passes the `AgentMessage` to the
  service. The message filter is `filters.TEXT | filters.PHOTO` (commands still
  excluded). It also owns `/stop` — the interrupt command: `handle_message` registers
  its own `asyncio.current_task()` as the chat's **in-flight reply** in
  `bot_data["in_flight"][chat_id]` (a per-chat slot — the per-conversation lock
  serialises a chat, so there is at most one) and removes it in a `finally` (on
  completion *and* cancellation, so a settled turn never lingers as a stale handle);
  because the app runs with `concurrent_updates(True)`, `cmd_stop` runs as an
  independent task and cancels that task for *its own chat* (a done/absent handle → a
  "Nothing to stop." reply; the targeted task otherwise posts its own "⛔️
  **Interrupted.**" notice, a **Telegram Reply quoting the interrupted message**).
  `handle_message` also catches `asyncio.CancelledError` around the turn (the
  per-conversation lock is an async context manager, so it already released on unwind
  and the next message proceeds; the typing keep-alive stops in `_with_typing`'s
  `finally`), sends the user-safe "⛔️ **Interrupted.**" as a **Telegram Reply** to the
  interrupted message, and **re-raises** (so PTB never logs the cancel as a handler
  error and the task is observed as cancelled). `/stop` stops a *generation* only —
  it never drops the conversation or memory (that is `/new`) and never touches another
  chat's turn. It also owns: `/start` `/new` `/context` `/help` `/status`
  (``/context`` is a read-only preview of the phase-2.4 context window — it calls
  ``AgentService.context_status``, which runs the same ``plan_context`` with an empty
  current user and reports counts/estimates only, reading no attachment blob and
  leaking no text/digest/path), the phase-2.5 memory commands (`/remember <content>` /
  `/memories` / `/forget <id>` / `/forget all CONFIRM` — each does the per-handler
  allow-list auth first, then calls the `AgentService` memory methods
  `remember_memory` / `list_memories` / `forget_memory` / `forget_all_memories`,
  mapping any `AgentError` to its user-safe message; `/forget all` without the literal
  `CONFIRM` token just prints the confirmation prompt and deletes nothing), a `typing`
  keep-alive loop, and **sending model replies as HTML** (`_send_long` renders the
  reply via `telegram/markdown.py` and sends each chunk with `parse_mode=HTML`; on a
  Telegram 400 "can't parse" it re-sends that chunk as **plain text** so a reply is
  never lost — `split_into_chunks` is still the plain-text fallback splitter and must
  never lose content). The **final answer** to a user's question is sent as a
  **Telegram Reply** to the user's message
  (`_send_long(chat, reply, reply_to_message_id=message.message_id)`) so it visibly
  quotes what it is answering — **only the final answer** carries the reference: the
  first chunk of a multi-chunk reply is quoted (the user's message is quoted once, not
  per chunk), while command acks (`/help`, `/remember`, …), error notices, the typing
  keep-alive, and intermediate sends are **not** replies. `_memory_scope(update)`
  builds the opaque `telegram:<effective_user.id>` scope (the *only* place a Telegram
  user id becomes a scope; the service/memory/DB never see Telegram types). The
  phase-3 `/tool_audit [limit]` command (allow-list auth first, then
  `AgentService.list_tool_audit_events(scope, limit)`) is a **read-only, scope-isolated**
  view of the caller's own recent tool activity — it renders each event as HTML
  (`**#{id}** time — tool / event`, then the stable code and optional `Nms`),
  newest-first, shows a safe "No tool activity" empty state, silently ignores
  unauthorised senders, and never prints arguments/results/the raw scope or user id;
  `limit` defaults to 20 and is clamped to 1–50 (non-numeric → a usage hint, service
  not called). It also owns the **approval callback handler** (wired via
  `TelegramApprovalBroker.build_callback_handler()` when an approval broker is
  supplied to `build_application`). The command list lives in `_COMMANDS` (single
  source of truth for the `/help` reply *and* the native Telegram command menu
  advertised via `set_my_commands` in `register_command_menu` — `/remember`,
  `/memories`, `/forget`, `/tool_audit`, `/mcp_status`, and `/infra_status` are in it).
  The phase-4 `/mcp_status` command (allow-list auth first, then reads the in-memory
  `McpManager` from `app.bot_data`) is a **read-only** status view: it shows each
  configured server's name, `available`/`unavailable`, and discovered-tool count,
  plus the total; it does **not** connect / refresh / call the LLM or any MCP server,
  shows "MCP: disabled" when there is no manager / `ENABLE_TOOLS=false` / no servers,
  silently ignores unauthorised senders, and **never** prints a URL / host / header /
  token / tool description / schema / server instructions / failure detail. The
  phase-5.1 `/infra_status` command (allow-list auth first) is a **read-only** view of
  the **configured** SSH-observation targets — each target's name + its three tool
  names + the total; it does **not** connect, probe reachability, import `asyncssh`,
  or call the LLM, shows "Infrastructure: disabled" when `ENABLE_TOOLS=false` / no
  targets, states it shows nothing about reachability, silently ignores unauthorised
  senders, and **never** prints a host / port / username / key path / known-hosts path
  / mount path / service / command. The phase-4.x `/mcp` command is **one**
  `CommandHandler("mcp", cmd_mcp)` that dispatches on its first argument — there is
  **no** separate `mcp_auth` command (such a handler would never match `/mcp auth …`):
  a bare or non-`auth` `/mcp` renders the read-only status view **plus the caller's
  own** per-server OAuth state (from `bot_data["oauth_manager"].oauth_status` —
  connected / authentication required / expired / not configured; a status-lookup
  failure degrades to "required"; **never** another user's state; unavailable servers
  show no OAuth line; no URLs / tokens), while `/mcp auth <server>` starts the login:
  it calls `oauth_manager.initiate(telegram_user_id=…, chat_id=…, mcp_server=…)`
  (the credential binds to the **user**, the chat only receives the notification) and
  replies with an HTML prompt carrying an **`InlineKeyboardButton` URL button** (the
  user never copies a URL) plus an expiry note; missing server argument → a usage
  hint, no OAuth manager → "not configured", an `OAuthError` → its stable `user_safe`
  text, any other exception → a generic "try again" **without** the exception detail.
  Startup hooks (command menu + DB init + MCP discovery) are chained with
  `compose_startup_hooks`.

- **`qq/bot.py`** + **`qq/commands.py`** — the *only* modules that know about QQ / the
  `botpy` SDK (phase 10, multi-channel). They mirror the `telegram/` package: the single
  knowledge source for the SDK, never touching the OpenAI SDK, Telegram, or ORM. The
  adapter is a thin, channel-agnostic transport over the same `AgentService` — no
  images, no streaming draft, **plain-text C2C (private-chat) send/receive** in this
  slice. On top of that it offers the same **slash-command surface** as the Telegram
  bot, **reply-quoting** of the user's message, and a **native command panel**. The
  command *logic* lives in `qq/commands.py` (pure, botpy-free, Telegram-free — a
  channel never imports another channel): `_QQ_COMMANDS` is the single source of
  truth (13 commands: the core set + read-only `/mcp_status` + `/user_status`,
  **minus** `/start` (no QQ
  concept) and `/mcp`/`/mcp auth` (OAuth is Telegram-bound)); `build_c2c_panel_items`
  maps it to the panel's `command` items, **filtering to the 14-char name cap** (which
  drops `/schedule_status` — it is still *dispatched* by hand) and the 20-item cap;
  `known_command_names()` exposes the dispatchable set; and `dispatch(command, args,
  …)` runs one command by reusing the channel-agnostic `AgentService` methods
  (`reset` / `conversation_status` / `context_status` / the memory methods /
  `list_tool_audit_events`) and the startup `Config` + `McpManager`, returning a
  **`CommandReply`** (a `NamedTuple` of the reply `text` plus a `markdown: bool` flag)
  or `None` (send nothing). Each handler reports its own shape **per branch** — the
  same command is plain in one outcome and Markdown in another (e.g. `/memories` is
  plain when there are none, Markdown when it lists them). The **command *reply* text
  renders in Chinese** (what a QQ user reads when they type the command — the
  channel's user-facing language; the Telegram adapter renders the *same* commands in
  English, only the delivery layer differs). The one `_QQ_COMMANDS` table
  (`(command, description)` — a single **Chinese** description) is the source of
  truth for *both* command-describing surfaces: the `/help` reply **and** the native
  command *panel*, so they never drift. An
  `AgentError` becomes its `user_safe` text; any other
  exception is logged by class and surfaced as a fixed generic notice — the dispatcher
  never raises. `QQChannel`
  is a plain class (no `botpy`) so it unit-tests against a fake `C2CMessage` without a
  live websocket. It holds the `service`, `repository`, `config`, and `mcp_manager`
  (the last two feed the read-only config commands) **and a QQ-local `_in_flight` dict**
  (conversation id → the `asyncio.Task` currently generating its reply), the QQ
  counterpart of the Telegram layer's in-flight registry. `on_c2c_message_create(message)`
  does, in order: (1) read
  `message.author.user_openid` — there is **no allow-list** (this is the owner's
  personal bot and a C2C chat is one-to-one, so any sender is served); a message with
  a **missing** openid is malformed and is ignored (logged by class only — there is no
  openid to leak); (2) strip `message.content`, and if blank return (no processing, no
  reply); (3) **command branch** — if the text starts with `/` and the leading token is
  a *known* command, it is dispatched and its reply delivered (via `_send_long`,
  **not** quoted, with `markdown=reply.markdown` so the delivery type matches the
  reply's shape), and the message is **not** stored as a conversation turn (matching
  Telegram);
  an **unknown** `/…` falls through to the normal turn (never swallowed); (4) the normal
  agent turn: key the
  persistent conversation by the deterministic synthetic `qq_chat_id(openid)` (stored as
  **both** chat id and user id — QQ has no separate numeric identity), creating it via
  `repo.get_or_create_conversation`; register `asyncio.current_task()` in `_in_flight`
  (so a later `/stop` — arriving as its own message/task — can cancel it); build
  `AgentMessage(contents=[TextContent(text)], source="qq")` and call
  `service.process_message(cid, agent_message, memory_scope=f"qq:{openid}")` — no
  `delivery_chat_id`, no `on_text_delta`. An `AgentError` maps to its `user_safe` text
  (logged by `category` only); any other exception logs by class and replies with a
  generic "unexpected error" notice; an `asyncio.CancelledError` (`/stop`) logs,
  sends a short "已停止" notice **quoted** to the interrupted message, and re-raises.
  The handle is removed in `finally` (completion *and* cancellation). Delivery goes
  through the local `_send_long(message, text, *, quote_id=None, markdown=True)`,
  which **chooses the message type per reply**: a **Markdown** send
  (`msg_type=2`, `markdown={"content": …}` — the model's text placed **verbatim** in the
  nested `markdown.content` field, top-level `content` unset) for the **agent-turn
  answer** (its default) and for **structured** command displays, and a **plain-text**
  send (`msg_type=0`, `content=…`) for **simple one-line command receipts**. The
  command branch passes `markdown=reply.markdown` so the delivery type follows the
  `CommandReply` shape each handler reports; there is **no** Markdown→anything
  conversion or escaping pass on the send path (unlike the Telegram adapter's
  `telegram/markdown.py` HTML conversion) — the text goes out exactly as produced, and
  QQ's client renders the Markdown it recognises. Chunks are split by the local
  `_split_for_qq` (same never-lose-content contract as the Telegram chunker, but
  **kept local** so a channel never imports another channel); the short error notice
  (`_safe_reply`) is also sent as plain text (`msg_type=0`).
  **Reply-quoting:** a normal answer's **first** chunk also carries
  `message_reference={"message_id": str(message.id), "ignore_get_message_error": True}`
  (the visible quote), mirroring the Telegram adapter's quote-once; later chunks and
  command acks do not. `message_reference` is **distinct** from the `msg_id`
  passive-reply thread — the former is the visible quote, the latter how QQ knows which
  message is being answered. `msg_seq` increments per chunk (1, 2, 3, …) because the
  QQ API dedups on `(msg_id, msg_seq)` and would otherwise drop every chunk after the
  first. `build_qq_client(service, repository, config, mcp_manager,
  approval_broker=None)` is the **only** place that imports `botpy` (lazily, so a
  Telegram-only deploy never loads it): it wraps a fresh `QQChannel` in a `botpy.Client`
  subclass whose `on_c2c_message_create` delegates to it, **whose `on_ready`
  best-effort (a) creates-or-updates the native command panel and (b) replaces the
  global C2C custom menu**, and — when an
  `approval_broker` is passed — **whose `on_interaction_create(interaction)` delegates
  to `approval_broker.handle_interaction(interaction)`** (the QQ tool-approval click
  path, see `qq/approval.py` below). The client is built with
  `Intents(public_messages=True, interaction=approval_broker is not None)` (bit `1 << 25`
  enables the C2C `c2c_message_create` event; bit `1 << 26` enables `interaction_create`,
  subscribed **only** when approval is wired so a tools-off deploy requests no extra
  scope) and `ext_handlers=False` (botpy's default would drop a rotating `botpy.log`
  file into the CWD; `bot_log=True` keeps its lifecycle logs propagating to our
  already-configured root logger instead). When an `approval_broker` is passed,
  `build_qq_client` also calls `approval_broker.bind_client(client)` so the broker can
  send the approval card (`client.api.post_c2c_message`) and ack the click
  (`client.api.on_interaction_result`).
  **The command panel:** botpy has **no** `menu`/`panels` wrapper, so `_ensure_c2c_panel`
  (fired from `on_ready`, after login, when the token is valid) makes raw
  `self.http.request(Route(…))` calls — the same primitive `botpy`'s own API layer uses:
  `GET /v2/panels?scope=c2c` → find a record whose `panel.remark` equals our fixed
  marker (`fibrecase-c2c`) → if found `PUT /v2/panels/{panel_id}` (with the record's
  `version` for optimistic locking), else `POST /v2/panels` (scope `c2c`,
  `target_type=all`, items from `build_c2c_panel_items`, the same `remark`). The
  `remark` marker makes it **idempotent across restarts** (the panel API is not — a
  blind re-POST stacks up to 20 identical panels). It **never raises**: any failure is
  logged by class and swallowed so a panel hiccup can never break startup or message
  handling.
  **The global custom menu (``/v2/menu``):** a second best-effort surface, distinct
  from the command panel. It is the C2C "⋮" menu that appears next to the input box
  for *every* C2C user — a **global, owner-configured** resource (no per-user
  remark/marker). `on_ready` also calls `_ensure_global_menu`, which issues a single
  raw `self.http.request(Route("PUT", "/v2/menu"), json=…)` with the body
  `{"menu": {"items": …}}` (built by the pure `_global_menu_payload`). Unlike the
  panel, `PUT /v2/menu` **replaces the whole menu**, so sending the same fixed payload
  on every startup is **idempotent by construction** — no create-or-update dance, no
  remark to match. It adds two `send_message` items (the only item type that fits a
  personal bot — `link` needs a https URL, `switch` a search endpoint we don't have):
  **"对话指令" → `/help`** (the input-box text that, when sent, dispatches `/help`
  and shows the native command list) and **"工具能力" → `你会使用哪些工具？`** (sent as
  a normal agent turn — a plain conversational prompt, not a command — so it runs the
  full tool loop and replies with a quoted Markdown answer listing the bot's tools). Both are fixed,
  secret-free literals the channel fully controls (no openid, command argument, or
  message body). It **never raises** — a menu hiccup is logged by class and swallowed
  exactly like the panel. **Approval on a QQ turn works:** an `ask` tool's request is routed
  (by scope prefix `qq:`) to the `qq/approval.py::QQApprovalBroker`, which sends an
  *active* C2C Markdown card with callback buttons and resolves the click's
  `INTERACTION_CREATE` (acked within 3 s); `deny` tools are still rejected. The full
  mechanism is in the `qq/approval.py` entry below. The composition root (`main.py`)
  builds the client in
  `_post_init` (on the PTB running loop, because `botpy.Client.__init__` grabs the
  running loop) and drives it as an `asyncio.Task` via `async with client: await
  client.start(app_id, secret)` — the SDK's own `run()` is blocking (it owns a loop) and
  cannot be used; in `_post_shutdown` the client is `close()`-d and the QQ task tree
  it spawned on the loop is cancelled (see the "botpy owns its event loop" entry
  above — `close()` alone leaves the SDK's websocket/heartbeat tasks pending). **Privacy:**
  the QQ adapter logs only the synthetic conversation id, the QQ *message id*, a text
  length, and (for a command) the command *name* — **never** the raw `user_openid` (a
  user identity), a command *argument* (which can carry memory content), or the message
  / reply body. **`/user_status`** is the one command that *deliberately* returns
  the caller's own `user_openid` **to that caller in-chat** (so they can fill a
  schedule's `receiver.qq`); it recovers the openid from the `qq:` scope prefix and
  is user-facing — the openid appears in the reply but is **never** logged (the
  privacy rule above still holds). **`deliver_qq_markdown(client, openid, text)`** is
  a module-level, botpy-free async helper (safe for the composition root to import):
  a **proactive**, chunked Markdown C2C send to an openid — an *active* message (no
  `msg_id` / `msg_seq`) outside the 5-min passive-reply window, one `msg_type=2`
  Markdown message per `_split_for_qq` chunk. The composition root's schedule
  delivery (`main.py::_deliver_schedule_notification`) uses it to push a scheduled
  run's result to a `receiver.qq`, alongside Telegram's `deliver_markdown`.

- **`qq/approval.py`** — the phase-10 **QQ tool-approval broker + scope router**
  (pure Python, **botpy-free** — it imports only `..memory.hash_scope` and
  `..tools.approval`; the `botpy` client is injected at `bind_client`, so it unit-tests
  against a fake `client.api`). `QQApprovalBroker` implements the channel-agnostic
  `ToolApprovalProvider`. `request_approval(request)`: fails **closed to DENIED** if no
  client is bound or `request.scope` is not `qq:<openid>`; else derives `openid` from
  the scope (C2C is one-to-one, so the sender openid is *both* principal and chat),
  records a `hash_scope` principal fingerprint (the raw openid is never held), registers
  an `asyncio.Future` in `_pending`, sends the card, then `await asyncio.wait_for(fut,
  timeout)` (timeout → `EXPIRED`; a `CancelledError` from `/stop` falls through
  `finally`, which drops the pending entry so it never leaks). The **card**
  (`_send_approval_message`) is an *active* `client.api.post_c2c_message(openid,
  msg_type=2, markdown={...}, keyboard=_approval_keyboard(request_id))` — **no
  `msg_id`/`msg_seq`**, so it is outside the 5-min passive-reply window and cannot
  collide with the turn's `(msg_id, msg_seq)` dedup. `_card_text` renders the fixed
  title, `**工具：**`, `**用途：**` (summary), and `**操作：**` (the `approval_detail`
  fence if present, else a pretty-JSON `**参数：**` block), then the one-time/expiry
  hint — **never** the raw openid/chat/secret; `_approval_keyboard` is a single row of
  two `action.type=1` callback buttons (permission `type=2` everyone, `visited_label`
  marks the clicked one) whose `data` is `v1:<request_id>:<a|d>` (request id + a single
  decision char only). `handle_interaction(interaction)`: ignores non-button events
  (`data.type != 11`, no ack), parses the clicker's `user_openid` + `button_data`,
  resolves via `_resolve` (principal fingerprint match + not expired + known
  `request_id` + one-time → set the future and return `code=0`; otherwise void the
  pending request and return `code=1`), and **acks within 3 s** via
  `client.api.on_interaction_result(interaction.id, code)` (an ack failure is logged by
  class and swallowed — it never changes the already-made decision). `shutdown()`
  resolves every pending future to `EXPIRED`. `QQScopedApprovalRouter(telegram_broker,
  qq_broker)` is the single `ToolApprovalProvider` `main.py` injects: `_provider`
  returns the QQ broker when `request.scope` starts with `qq:`, else the Telegram
  broker, and `shutdown()` drains both. Module-level `request_id_from` /
  `decision_from` parse the button `data` (`v1:<request_id>:<a|d>`), rejecting anything
  malformed.

- **`telegram/media.py`** — the *only* module that fetches Telegram media (phase 2.2).
  `normalize_message(msg, max_bytes)` → `AgentMessage`. `extract_image_message` takes
  the **largest** `message.photo[-1]` rendition, downloads it **in memory** via
  `PhotoSize.get_file()` → `File.download_as_bytearray()` (no temp file), then
  validates: size (→ `MediaError` `image_too_large` if over `max_bytes`) and MIME
  (magic-byte sniff for `image/jpeg`/`image/png`/`image/webp`, else `MediaError`
  `unsupported_mime`). A download failure is a `MediaError` (`download_failed`). It
  logs `message_id`/`content_type`/`mime_type`/`size_bytes` **only** — never the bytes,
  base64, or any secret. The Telegram `file_id`/`PhotoSize` never leave this module.

- **`telegram/markdown.py`** — converts the model's Markdown to Telegram's HTML subset
  for display (Telegram does not render Markdown). `to_telegram_html(text)` handles
  bold (`**`/`__`), italic (`*`/`_`), strikethrough (`~~`), inline code (`` ` ``, kept
  **verbatim** — emphasis/links/strikethrough are never applied inside it), fenced
  code blocks (``` ``` ```), links (`[x](https://…)`) and headings (`#`), and escapes
  `& < >`. A single `_` between word chars (e.g. `snake_case`,
  `config/system_prompt.txt`) stays literal. `to_telegram_html_chunks(text, limit)`
  splits into `HtmlChunk(text, html)` pieces that are **tag-balanced per chunk** — it
  splits *source* into blocks (a fenced code block is one atomic block; text splits at
  blank lines) *before* rendering, so a chunk never starts mid-`<pre>`/`**`. A fenced
  block larger than the limit stays its own chunk (sent as plain text on 400).

- **`telegram/approval.py`** — the phase-3 **in-memory Telegram approval broker** (the
  *only* approval-path module that knows about Telegram).
  `TelegramApprovalBroker(repository)` implements the channel-agnostic
  `ToolApprovalProvider` contract (injected as the `AgentService`'s
  `approval_provider` by the composition root `main`). `request_approval(request)`
  resolves the transport chat from `request.conversation_id` (via
  `repository.get_conversation_by_id`), hashes `request.scope` to a `principal_hash`
  (the raw user id is **never** held), presents an **Approve/Deny** inline-button
  prompt in the *original* chat (fixed, secret-free title + tool name + the tool's
  safe **purpose** summary under a `What it does:` line + a readable **argument view**
  when the call has arguments — either the default pretty-JSON **`Arguments:`** block
  (labelled `json`), or, when the tool overrides the optional `approval_detail` hook
  (`file_edit` / `file_write` / `file_append`, `exec`), a tool-supplied plain-text view
  under an **`Action:`** label
  (labelled by the tool's `approval_language` hook — `diff` for the three `file_*`
  writers, `bash` for `exec` — so Telegram highlights it by language rather than
  guessing); both
  rendered in `<pre><code>` — the `<pre>` carrying a sanitised `class="language-…"`
  attribute (the tool-declared language is kept to `[A-Za-z0-9_-]`, capped at 24 chars,
  lowercased; a hostile value can't inject a second `class` or close the tag, and an
  empty label is dropped) — HTML-escaped so a value can't inject markup, length-bounded,
  and omitted entirely for an argument-free call) + an expiry hint; the callback data
  carries only `v1:<request_id>:<a|d>`), and awaits the decision on an
  `asyncio.Future` under `asyncio.wait_for` (never blocks the loop, never busy-polls).
  Once the approval is decided or expires, the card is **finalised in place**: the
  `Approve`/`Deny` buttons (labelled **`✅ Approve`** / **`❌ Deny`**) are removed (an
  empty `InlineKeyboardMarkup([])` — serialised to `{}` on the wire, the Bot API
  "remove keyboard" signal; `None` would be dropped by PTB and leave the buttons), the
  "one-time / will expire" hint line is replaced by a single **bold, emoji-tagged
  status word** — `<b>✅ Approved.</b>` / `<b>❌ Denied.</b>` /
  `<b>⏰ Expired (no decision in time).</b>` (no "Status:" label) — **and the argument
  view (`Arguments:` JSON or `Action:` detail block) is dropped** (the buttons are
  already gone, so the resolved card keeps only the title, tool name, purpose summary,
  and status) — via `edit_message_text` (best-effort — a failed edit never changes the
  decision and posts no follow-up message; every resolution path funnels through the
  `wait_for`, so finalisation is single-sourced in `request_approval`). It is
  **one-time + expiry + (principal, chat)-bound**: a repeat click, an unknown/stale id,
  a lapsed deadline, or a click from any *other* user or *other* chat **voids** the
  pending request (resolves `EXPIRED` and drops it) so the tool is never executed and
  the waiter unblocks immediately — no existence leak, no double-execution. Any setup
  failure (no app bound, unresolvable conversation, failed prompt send) fails
  **closed** to `DENIED`. `shutdown()` resolves all pending requests as `EXPIRED` so
  any waiting caller completes on process exit. The `CallbackQueryHandler` (pattern
  `^v1:`) is built by `build_callback_handler()` and wired into the app by
  `build_application` — it is a *callback* handler, **not** a tool; the model can never
  invoke it, and no amount of model text can bypass the gate.

- **`agent/messages.py`** — the channel-independent content model (phase 2.2):
  `AgentMessage(contents: list[ContentPart], source, metadata)` with
  `TextContent(text)` and `ImageContent(data: bytes, mime_type, filename?)`.
  `ContentPart` is a `Union` so future `FileContent`/`AudioContent`/… extend it
  without touching the agent or converter. `AgentMessage.text` returns the joined text
  (what gets **persisted**); `has_image()` / `is_empty()` are the small helpers the
  service uses. The agent layer depends on these types, **never** on Telegram
  `Message`/`PhotoSize`/`file_id`.

- **`attachments/`** — the phase-2.3 blob store, the *only* module that knows how
  attachment bytes live on disk. `AttachmentStore(root)` is **channel-/protocol-/ORM-free**
  (it imports none of Telegram, the OpenAI SDK, or SQLAlchemy — verify before
  editing). `save(bytes) → StoredBlob(sha256, storage_key, size_bytes, created)` is
  content-addressed by the SHA-256 of the bytes at `<root>/<digest[:2]>/<digest>` and
  **atomic** (temp file in the same dir → `fsync` → rename; never a direct write to
  the final name); identical bytes reuse the existing blob (`created=False`, no
  rewrite). `read(digest)` integrity-checks (re-hashes) and raises
  `AttachmentNotFoundError` / `AttachmentCorruptError` for a missing or corrupt blob
  (both user-safe, so a caller can *skip* the image while keeping its text);
  `delete(digest)` treats a missing file as already-cleaned and raises
  `AttachmentStorageError` only on a genuine I/O failure. `storage_key_for`/`iter_blobs`
  are the discovery helpers. Every digest is validated (64 lowercase hex) before use —
  a caller value can never escape the root. It logs **only** a short digest prefix,
  byte counts, and the operation result.

- **`memory/`** — the phase-2.5 long-term-memory logic, the *only* module that knows
  how memory text is normalised and ranked. `memory/text.py` is **pure Python** (imports
  none of Telegram, the OpenAI SDK, or SQLAlchemy — verify before editing) and does
  **no I/O**: `normalize_text` (casefold + trim + whitespace collapse, deterministic
  over ASCII/CJK/emoji/punctuation), `extract_terms` (each CJK codepoint → a
  single-char term; each contiguous ASCII letter/digit run → a word token, ASCII
  tokens <2 chars dropped; de-duplicated), `rank_memories(query, candidates, limit)`
  (a stable, all-descending score key: (1) full normalised-query **substring hit**,
  (2) **unique term-overlap count**, (3) newer `updated_at`, (4) larger `id`; an
  empty / punctuation-only / no-term query returns `[]`; a zero-score candidate is
  never returned to pad the result), `build_memory_reference_text` /
  `memory_reference_line` (the fixed `MEMORY_REFERENCE_HEADER` wrapper + one
  `- [memory #id] content` bullet, raw content **verbatim**), and `hash_scope` (a
  salted SHA-256 prefix — stable to correlate log events but **not invertible** to the
  raw scope/user id; the *single* implementation shared by the repository and the
  service for safe logging). `MemoryCandidate` is a lightweight, channel-/ORM-free
  view (id, original `content`, `normalized_content`, `updated_at`) the service builds
  from repository records. The raw memory content is user text and **can** be
  instructive — the safety boundary is the fixed wrapper (treat as background facts,
  not instructions) plus the fact that it rides a separate `user`-role message that
  can never alter the main prompt's role, tools, or permissions (it is deliberately
  **not** a second `system` message — endpoints 400 on two system messages); it is
  **not** sanitized.

- **`agent/service.py`** — the channel-agnostic core (reusable for a future
  web/Discord/API). It holds a **per-conversation `asyncio.Lock`**
  (`conversation_lock`) so one chat is serialised while different chats run
  concurrently. `process_message(conversation_id, user_message, *, memory_scope=None)`
  accepts a **`str` or an `AgentMessage`** (a bare string is normalised to a single
  `TextContent`; empty text short-circuits, exactly as before). Flow: acquire lock →
  load history **with attachments** (detached) → persist the user turn **text** and
  (phase 2.3) any image blobs + metadata → **(phase 2.5) when `memory_scope` is set
  and the message has text, deterministically retrieve + rank memories (no LLM)** →
  **build `TurnCandidate`s and run the phase-2.4/2.5 `plan_context()` planner (no bytes
  read yet; the ranked memories are selected here too, within the memory sub-budget)**
  → **rehydrate only the selected attachments from the store** (downgraded turns and
  unselected/older turns are sent as plain text or omitted — their blobs are *never*
  read; out-of-window and out-of-budget images stay on disk) → build context
  (**main system prompt, then the optional single memory-reference `user` message, then
  selected history, then the current turn**; the current turn = in-memory bytes, images
  never downgraded) → **best-effort stamp `last_retrieved_at` on the actually-injected
  memories** → **run the tool loop** (when `enable_tools`) *or* a single LLM call (when
  disabled) → persist assistant turn → return text. If the planner reports
  `current_over_budget` it raises `AgentError(category="context_limit")` (**no LLM
  call**) — the user's text and already-persisted image are kept, matching other
  failure paths. LLM failures become `AgentError` with a **generic, user-safe message**
  (no stack traces / keys / paths leak to Telegram); a `ToolLoopLimitError` maps to
  the `tool_limit` message; an attachment write/metadata failure maps to
  `attachment_error` and **compensates** (removes a just-created orphaned blob) so an
  un-persisted image is never sent; a memory **retrieval** failure maps to
  `memory_error` and aborts **before** the LLM is called. **Only the user turn and the
  final assistant turn are persisted**; the intermediate `tool_calls` / `tool` turns
  are not stored. `reset()` additionally **reclaims** blobs orphaned by the dropped
  conversation (keeping any still referenced elsewhere) and **never touches
  `memories`**. The memory *command* methods (`remember_memory` / `list_memories` /
  `forget_memory` / `forget_all_memories`) are scope-isolated, never call the LLM,
  trim + validate (`memory_invalid` / `memory_limit`), and fail safe into
  `memory_error`. Phase 3 adds constructor params `policy=` / `approval_provider=` /
  `auditor=` / `tool_timeout_seconds=` / `tool_approval_timeout_seconds=` (the tool
  loop is now run with these when `enable_tools`; `auditor` defaults to
  `NoopAuditor`), and a `list_tool_audit_events(scope, limit)` method (a
  scope-isolated, metadata-only read that maps a repository failure to a user-safe
  `tool_audit_error`). When `attachment_store` is `None` (tests / opt-out) images are
  sent in the current turn but **not** persisted — the exact phase-2.2 behaviour. The
  planner logs only safe metadata (budget, estimated cost, selected/dropped message
  counts, images kept vs downgraded, `memories_selected`, `memory_cost`) — never
  content, captions, full digests, paths, bytes, or secrets.

- **`agent/tool_loop.py`** — the piece inserted between the service and the LLM.
  `run_tool_loop(llm, messages, registry, max_iterations, *, policy, approval_provider,
  auditor, tool_timeout_seconds, approval_timeout_seconds, conversation_id, scope)`
  calls the LLM, and if the result has `tool_calls` it appends the assistant tool-call
  message and runs each call through a **strict, audited gate** in this exact order —
  **parse → registered? → policy (allow/ask/deny) → JSON-Schema validation →
  pre-execution audit (fail-closed) → (if `ask`) one-time human approval →
  `asyncio.wait_for(execute, tool_timeout_seconds)` → terminal audit** — then appends
  the `tool` result message and calls the LLM again, until a message with no tool
  calls (the final answer) or the iteration limit is hit (→ `ToolLoopLimitError`).
  **Approval is not a tool**: the `ask` decision comes from the injected
  `ToolApprovalProvider` (the Telegram broker), driven by the out-of-band callback, so
  model text can never approve a call. Every failure (unknown tool, `deny`, invalid
  arguments, a fail-closed pre-audit write, an un-approved/expired approval, a timeout,
  an execution exception) returns a **stable, non-echoing** JSON error result
  (`{"error": {"code": …, "message": …}}`) to the model instead of executing or
  leaking the exception/args. **Phase 4.x principal bridge**: the loop sets the
  `mcp/auth/principal.py::active_principal` ContextVar to the `scope` (e.g.
  `telegram:<user_id>`) **around** `tool.execute()` and resets it in a `finally` —
  this is the *only* way a wrapped OAuth MCP tool learns *which user* is invoking it,
  so `McpOAuthAuth` can attach that user's own bearer token; a call with no `scope`
  runs with no principal (the startup handshake path). It depends only on an LLM that
  accepts `tools=` (a `Protocol`), a `ToolRegistry`, a `ToolPermissionPolicy`, a
  `ToolApprovalProvider`, and a `ToolAuditor`; it knows nothing about Telegram, the DB,
  or the OpenAI SDK.

- **`tools/`** — provider-/channel-agnostic. `base.Tool` is an ABC: `name`,
  `description`, JSON-schema `parameters`, a declared `default_permission`
  (`ToolPermission`, base default `ask`), a safe `approval_summary(arguments)` (shown
  on the approval card under a `What it does:` line — it should describe the tool's
  **purpose** and **never echo** `arguments`, because the card shows the arguments
  separately in its own `Arguments:` block; the **default** here is a generic,
  argument-free purpose line that only names the tool, while the built-ins and the MCP
  `McpTool` each override it with a purpose line), an optional
  `approval_detail(arguments) -> str | None` (a **plain-text**, faithful argument view
  that — when non-`None` — **replaces** the generic `Arguments:` JSON block on the
  card, shown under an `Action:` label; the provider HTML-escapes + length-bounds it
  and drops it on finalisation exactly like the JSON block. The **default** is `None`
  → the generic JSON block; the state-changing built-ins `file_edit` (a faithful
  **git-style diff** of its `old_string`/`new_string` — `--- a/<path>`/`+++ b/<path>`
  headers, each old line `-`-prefixed, each new line `+`-prefixed, empty
  `new_string` a pure deletion), `file_write` (the new content as a pure
  **addition** — every line `+`-prefixed), `file_append` (the content to be added as
  a `+`-prefixed block, existing content not dumped), and `exec` (the command as a
  `$ …` bash block) override it), an optional `approval_language(arguments) ->
  str | None` (a fixed
  **Pygments language name** — `diff` / `bash` / `json` / … — that the provider
  sanitises into a `<pre class="language-…">` attribute so Telegram syntax-highlights
  the code block instead of guessing its language; the tool declares a fixed
  vocabulary, **never** a value derived from argument content, so it cannot inject a
  second `class`/close the tag; the provider keeps only `[A-Za-z0-9_-]`, caps it at 24
  chars, lowercases it, and drops the label when empty; the **default** is `None` →
  unlabelled, `file_edit` / `file_write` / `file_append` return `diff`, and `exec`
  returns `bash`; the generic
  `Arguments:` JSON block is always labelled `json`), and `async execute(arguments)
  -> str`; `spec()` builds the inner `function` block. `registry.ToolRegistry` does `register`/`add`/`get`/`names` (a
  `register()` of an invalid declared schema is a `ValueError`, checked with
  `jsonschema`'s `Draft202012Validator.check_schema`), `to_openai_schema(names=None)`
  (list of `{"type":"function","function":{...}}` — it honours the `names` set so a
  `deny` tool is withheld), `validate_arguments(name, arguments)` (JSON-Schema
  validation via the *maintained `jsonschema` library*, not hand-rolled;
  `None`→`{}`; a non-object/malformed/missing/wrong-type/extra-property payload is
  rejected), and `execute(name, arguments)` — dispatching by name and converting a
  tool's exception into a stable `error_result("tool_execution_failed")` (so one bad
  tool can't kill the loop; an *unknown* name raises `ToolNotFoundError`, which the
  loop turns into the stable `unknown_tool` result). `tools/builtin/` holds
  `get_current_time` (no args), `echo` (`{"message": str}`), and `system_info`
  (hostname/platform/Python version via stdlib `socket`+`platform`, **no subprocess**);
  `get_current_time` and `echo` **declare `allow`** (safe read-only), while
  `system_info` is deliberately declared `ask` (still read-only — set to `ask` to
  exercise the approval flow); **each built-in overrides `approval_summary` with a
  fixed, argument-free purpose line** so the approval card shows *what it does* rather
  than the generic fallback, and `build_default_tools()` assembles them. The opt-in
  **`exec`** tool (`tools/builtin/exec.py`) is the more general of the two
  state-changing built-ins: `/bin/sh -c` full shell, `ask`, a static
  catastrophic-command backstop (`tools/exec_policy.py`), arg-vector spawn,
  process-group kill on timeout/cancel, and tail-truncated output — added by
  `build_default_tools(...)` **only when `enable_exec` is true**. The opt-in **`file`
  toolset** (`tools/builtin/file.py`) is the narrower second state-changing built-in
  (see the [file toolset section](#status-phase-by-phase)). Both keep their
  path/command/content **only** in the model-facing result, never in logs or the audit
  table. Phase-3
  sub-modules: `policy.py` (`ToolPermission`, `ToolPolicy`/`build_policy(overrides,
  registry=)` — precedence: config override > declared default, unknown tool → `ask`;
  `advertised_names()` drops `deny` tools; `parse_permission(raw)` parses one of
  `allow`/`ask`/`deny` case-insensitively → `ToolPolicyError` on a bad value;
  `FileBackedToolPolicy(path, registry)` — a `ToolPolicy` **subclass** whose overrides
  come from `MCP_PERMISSIONS_FILE`, hot-reloaded on each `resolve`/`advertised_names`
  call (an mtime/size re-read, no background watcher): missing/blank file → all
  declared defaults, present-but-malformed at runtime → keep the **last-good** inner
  policy + warn, never crash; it rebuilds via the existing `build_policy` so built-in
  declared defaults flow through unchanged), `permissions_file.py` (channel-/protocol-/ORM-free
  — the *only* module that knows the file's on-disk shape:
  `parse_permissions_json`/`load_permissions_file` strict read, `merge_permissions`
  the seed/sync merge (current tools first at their existing perm or `""`, then
  surviving **filled** orphans sorted, unfilled orphans dropped, idempotent),
  `serialize` (canonical `json.dumps(indent=2, sort_keys=True)+"\n"`, used for both
  the byte-compare and the write), `atomic_write` (same-dir `mkstemp`+`fsync`+`os.replace`,
  **skips** when byte-identical → no mtime churn), and
  `reconcile_permissions_file(path, current_names)` the backend→file seed;
  `PermissionsFileError` on a present-but-malformed file), `approval.py` (the
  channel-agnostic, stdlib-only `ApprovalDecision`/`ApprovalRequest`/
  `ToolApprovalProvider` contract; `ApprovalRequest` carries the already-schema-validated
  `arguments` so the card can show them, but the loop and auditor keep them out of logs
  / the audit table / model-facing error text), and `audit.py` (the `ToolAuditor`
  protocol + `NoopAuditor`, the stable result `code` constants, and
  `error_result(code)`).

- **`mcp/`** — the phase-4 **MCP tool provider** (Streamable HTTP + stdio), the *only*
  module that knows how to talk to an MCP server. It is
  **channel-/protocol-/DB-/OpenAI-SDK-free** (it depends only on the MCP SDK and its
  HTTP client / stdio transport — it imports none of Telegram, the OpenAI SDK, or
  SQLAlchemy; verify before editing). `wrapper.py` exposes
  `local_tool_name(server, remote)` (→ `mcp_<server>__<remote>`, a legal
  `[A-Za-z0-9_-]+` name with the remote segment capped at 90 chars so the local name
  fits the `String(128)` column) and `McpTool` (a first-class `Tool`:
  `default_permission = ask` **unconditionally**, `parameters` = the remote
  `input_schema` verbatim, a fixed non-instructional `description` that never surfaces
  server instructions, and an `approval_summary` that returns the tool's
  `description` (purpose) and **never echoes the (remote) arguments** — the arguments
  are shown separately on the card as a readable-JSON `Arguments:` block; `execute()`
  forwards `arguments` to the connected session's `call_tool` and maps the response to
  a bounded, non-echoing string — multi-part text joined by newlines, everything else
  a stable code: `mcp_unavailable` / `mcp_tool_error` / `mcp_unsupported_result` /
  `mcp_result_too_large`; it never raises a bare remote exception).
  `manager.py::McpManager` is built only when `ENABLE_TOOLS=true` and at least one
  server is configured; `start()` runs per server — **dispatching on `transport`**
  (http: http client with bearer header / OAuth auth → Streamable HTTP; stdio: spawn
  the operator-configured process via `stdio_client(StdioServerParameters(command,
  args, env, cwd))`) — both yielding the same `(read, write)` streams →
  `ClientSession` → `initialize()` + `tools/list()` under `wait_for` → wrap each tool
  atomically — and `add`s the results to the registry after the built-ins.
  **Phase 4.x**: `McpManager` takes an optional `oauth_auth_factory(spec)` — for an
  http server with `auth_type == "oauth"` the http client is built with
  `auth=<factory output>` (`McpOAuthAuth`) **instead of** a bearer header (OAuth is
  **http-only** — a stdio server has no request to carry a header, so config forbids
  `auth_type`/`bearer_token_env` on it); if the factory is `None` such an http server
  is marked `unavailable` with the stable **log-only** code
  `mcp_oauth_not_configured` and **never connects** (a missing credential set must
  degrade, not crash, and the code must not leak into `status()`). Fault isolation
  (one bad server → `unavailable`, the rest start; a stdio spawn failure counts; the
  bot never fails to boot), atomic per-server discovery (any illegal name/schema/
  collision drops the whole server), bearer header read from env only at client-build
  time (never stored/logged/echoed), `status()` returns only
  `name`/`available`/`tool_count` (never URL/host/token/description/schema/
  instructions/failure detail, nor the stdio `command`/`args`/`env`/`cwd` — OAuth
  stable codes are **log-only**), and `close()` is idempotent (closing a stdio
  server's stack lets the SDK kill its child process). The composition root `main`
  owns the lifecycle: construct (or skip) in `main()`, `start()` in `_post_init`
  (after DB init) and `close()` on shutdown.

- **`mcp/auth/`** — the phase-4.x **user-level OAuth** machinery for MCP servers;
  provider-agnostic, channel-free, and DB-agnostic (storage behind the `OAuthStorage`
  ABC — the SQLite implementation lives in `database/oauth.py`). `provider.py`: the
  `OAuthProvider` ABC (`authorization_url(redirect_uri, state) -> str`,
  `async exchange_code(code, redirect_uri) -> TokenResponse`,
  `async refresh_token(refresh_token) -> TokenResponse`) + `GoogleOAuthProvider`
  (fixed Google endpoints; form-encoded token POSTs; provider errors →
  `OAuthProviderError` with a **fixed, non-echoing** reason — the provider response
  body is never surfaced, logged, or stored). `manager.py::OAuthManager` (over the
  `OAuthStorage`): `initiate(telegram_user_id, chat_id, mcp_server)` validates the
  server is an OAuth server (else `OAuthError("mcp_server_not_oauth")`), the provider
  exists (`oauth_provider_not_configured`), and a callback base is configured
  (`oauth_callback_not_configured`), then mints a `secrets.token_urlsafe(32)`
  **state** persisted bound to `(user, chat, provider, server)` with a TTL and returns
  the `PendingAuthorization` (authorization URL with `state` + `<base>/oauth/callback`
  redirect); `complete_authorization(params)` — the callback brain: consume the
  pending state **first** (single-use; unknown/expired/replay/no-`code` → `INVALID`,
  provider `error=…` → `DENIED`), then exchange the code and **upsert** the credential
  **bound to the pending record's triple** (never to forged query params), then fire
  the injected async `notifier` (a notifier failure never changes the outcome);
  `valid_access_token(telegram_user_id, mcp_server)` — returns the stored access token
  while valid (expiring within the last **60 s** still counts as valid), else refreshes
  under a per-`(user, server)` lock (concurrent callers refresh **once**): rotated
  refresh token persisted, missing one keeps the old, **refresh failure keeps the
  credential** (returns `None`; recovery is re-`/mcp auth`); `oauth_status`/
  `authenticated` are the **token-free** classifiers (`not_oauth` / `not_configured` /
  `provider_not_configured` / `authentication_required` / `connected` — including
  expired-with-refresh, which will auto-refresh — / `expired` — expired **without**
  refresh). `models.py`: the records (`CredentialRecord`, `PendingAuthorizationRecord`,
  `PendingAuthorization`, `TokenResponse`), `AuthorizationStatus`/
  `AuthorizationOutcome`, and `OAuthError(code, user_safe)` with its stable codes.
  `principal.py`: the `active_principal` **ContextVar** (holds an opaque scope like
  `telegram:<user_id>` during a tool execution) + `telegram_user_id_from_scope`
  (parses the numeric id; non-telegram/unknown formats → `None`).
  `oauth_auth.py::McpOAuthAuth(httpx2.Auth)`: the per-user token hook — its
  `async_auth_flow` reads `active_principal`, resolves the user id, calls
  `manager.valid_access_token`, and sets `Authorization: Bearer <token>`; **no
  principal or no credential → no header**; any lookup failure → no header + a
  **stable-code-only** log line (never the token, user id, or exception text).
  `server.py`: `OAuthCallbackServer` — a **minimal starlette app** (zero new
  dependencies: starlette/uvicorn are transitive of the MCP SDK) with a single
  `GET /oauth/callback` route and a fixed `404` ("Not found") for every other path;
  it maps the manager's outcome to fixed HTML (success names the connected server;
  denied/invalid/expired/error are generic) and its `start()`/`stop()` run **as a task
  on PTB's own event loop** (see `main.py`). **Logging rule**: nothing in this package
  ever logs an access/refresh token, an authorization code, a client secret, a
  `state`, or the full callback URL (its query carries `state`+`code`) — tests assert
  this.

- **`infrastructure/`** — the phase-5.1 **read-only SSH observation provider** (a Tool
  Provider, like MCP — it yields ordinary tools, not a new execution path). It is
  channel-/protocol-/DB-/OpenAI-SDK-free (it may use AsyncSSH + stdlib — it imports
  none of Telegram, the OpenAI SDK, or SQLAlchemy; **verify before editing**).
  `provider.py`: `local_tool_name(target, observation)` →
  `infra_<target>__<observation>`; three fixed remote-command templates
  (`_HOST_COMMAND` / `_disk_command(mounts)` / `_service_command(services)`) whose
  only interpolated values are the startup-validated `mounts` / `services` (each
  shell-quoted via `_shquote`); three strict parsers (`_parse_host` / `_parse_disk` /
  `_parse_service`) that raise a private `_ParseError` on any
  missing/duplicated/extra field, illegal number, or wrong record set; `InfraTool` (a
  first-class `Tool` with `default_permission = allow` — strictly read-only, so it
  runs **without** a per-call approval like `get_current_time`/`echo`; a fixed
  secret-free `approval_summary`, and `execute()` that `del arguments`, opens a
  **short-lived** host-key-pinned key-only connection via the module-level `_connect`
  (lazy `import asyncssh`; `client_keys`/`known_hosts` never `None`,
  `agent_path=""`, no password/kbd-interactive), runs the command, and closes the
  connection in a `finally`), and maps any failure to the stable non-echoing codes
  `infra_unavailable` / `infra_invalid_response` / `infra_result_too_large` (never a
  bare asyncssh exception; the loop's `tool_execution_failed` is the backstop).
  `build_infra_tools(targets, *, connect_timeout_seconds, max_result_chars)` yields
  `targets × 3` in stable order. **Logging rule**: this package logs only the tool
  name + stable code + the exception **class** (`type(exc).__name__`) — **never** the
  host, IP, private-key path, known-hosts path, username, mount path, service, command,
  stdout, stderr, or any secret; the model-facing result is parsed JSON (bounded by
  `MAX_INFRA_TOOL_RESULT_CHARS`) or a stable code. `asyncssh` is **lazy-imported**
  inside `_connect`, so an empty target list or `ENABLE_TOOLS=false` never imports it.

- **`agent/context.py`** — the *single* owner of how the context window is chosen, and
  **pure Python** (imports none of Telegram, the OpenAI SDK, SQLAlchemy, the
  filesystem, or `AttachmentStore` — it works only on role/text/attachment-*metadata*
  candidates with **no image bytes**). `ChatMessage.content` is **`str | list[dict]`**:
  a plain `str` for every text-only message, or a `list` of OpenAI content parts for a
  message that carries an image — the *current* multimodal turn (built by
  `llm/message_converter.py`) **or a history turn whose image attachments were
  rehydrated** (phase 2.3). It carries optional `tool_calls` / `tool_call_id` (both
  `None` for every chat-only message, so `to_dict()` is unchanged there). Two
  selection layers: the legacy `build_context(system, history, max_n)` (system + most
  recent N *messages*; kept byte-for-byte so the text-only path is unchanged) and the
  **phase-2.4/2.5 budget planner** — `estimate_text_cost` / `estimate_parts_cost` /
  `message_cost` (a deterministic, model-agnostic estimate) plus `plan_context()`,
  which groups history into complete turns and, before any blob is read, selects turns
  to fit **both** `MAX_CONTEXT_MESSAGES` and the token budget, downgrading a turn to
  text-only when its images won't fit and returning `current_over_budget` when
  system+current alone overflow. Phase 2.5 adds `memories=` +
  `max_memory_estimated_tokens` to `plan_context`: after the `current_over_budget`
  guard, it greedily selects already-ranked `MemoryCandidate`s whose *whole
  reference-message* cost fits the memory sub-budget **and** the total budget (a
  too-big candidate is skipped, lower-scored ones still tried, content never
  truncated); the memory cost is committed before history selection, and
  `ContextPlan` exposes `selected_memories` + `memory_cost` (both empty/0 when nothing
  is injected → the plan is byte-for-byte the phase-2.4 plan).

- **`llm/client.py`** — the *only* module that knows the OpenAI SDK. Wraps
  `AsyncOpenAI` in `complete(messages, *, tools=None, ...) -> LLMResult`. When
  `tools` is passed it is forwarded to `chat.completions.create`; a model reply's
  `tool_calls` are normalised into the canonical dict shape and set on
  `LLMResult.tool_calls`. Provider failures become one `LLMError` with a stable
  `category` (`timeout` / `http_error` / `connection` / `empty_response` / `error`).
  **A response with tool calls but no text is *not* an `empty_response`** (only blank
  content *and* no tool calls is). `stream=True` is accepted but raises
  `NotImplementedError`. The wire `ChatMessage.content` is `str | list[dict]` (the
  `list` case is the multimodal current turn from
  `agent_message_to_openai_content`); both serialise straight through `to_dict()`.
  Note: this is the *wire* client; the tool *loop* lives in `agent/tool_loop.py`, and
  the `AgentMessage` → OpenAI `content` mapping lives in `llm/message_converter.py`
  (a pure function, no SDK import).

- **`database/`** — `models.py` (ORM), `session.py` (engine/session factory +
  `init_db`), `repository.py` (the only layer that touches ORM; handlers never write
  SQL). One Telegram chat = one conversation, keyed by `telegram_chat_id`.
  `Message.role` already allows `tool` (ahead of phase 2), but tool turns are never
  written. Phase 2.3 adds the `Attachment` table (see above) and a
  `Message.attachments` relationship; the repository gains
  `add_message_attachments` (one commit per message),
  `get_messages_with_attachments` (eager-loads attachments via `selectinload` and
  returns **detached** `MessageWithAttachments`/`AttachmentRef` records — safe after
  session close, no lazy loading), and `attachment_sha256_for_chat` /
  `distinct_attachment_sha256` (the digest sets `reset()` uses to reclaim blobs).
  Phase 2.5 adds the `Memory` table and a `MemoryRecord` detached dataclass; the
  repository gains `add_memory` / `list_memories` / `get_memory` / `delete_memory` /
  `clear_memories` / `count_memories` / `list_memories_for_search` /
  `mark_memories_retrieved` — **every by-id read/delete is filtered by
  `scope + id` in SQL** (a foreign or missing id returns `None` / `False`, never
  leaking existence), and `mark_memories_retrieved` stamps `last_retrieved_at` on the
  given ids (no-op on empty). Phase 3 adds the **`tool_audit_events`** append-only
  table (columns: `id`, `created_at`, `conversation_id`, `tool_name`, `tool_call_id`
  (nullable), `iteration`, `event_type`, `code`, `latency_ms` (nullable),
  `scope_hash`) — the repository gains `add_tool_audit_event` (**fail-closed**:
  returns `False` on any write error, never raises) and
  `list_tool_audit_events(scope_hash, limit)` (**scope-isolated** by `scope_hash` in
  SQL, newest-first, `limit` clamped). `database/audit.py::RepositoryToolAuditor` is
  the channel-agnostic→DB implementation of the `ToolAuditor` contract:
  `record_pre` (the fail-closed pre-execution write) and `record` (best-effort
  terminal write); it hashes the raw `scope` via `hash_scope` at this boundary, so the
  raw scope/user id never reaches the table. `get_messages()` is unchanged and remains
  the plain-text path. Phase 4.x adds two tables: **`oauth_credentials`** (`id`,
  `telegram_user_id` (indexed), `provider` `String(32)`, `mcp_server` `String(64)`,
  `access_token` `Text`, `refresh_token` `Text` (nullable), `expires_at` (nullable),
  `scopes` `Text` (nullable), `created_at`/`updated_at`, with a **unique constraint
  on `(telegram_user_id, provider, mcp_server)`** — one active credential per triple)
  and **`oauth_authorization_states`** (`id`, `state` `String(128)` unique,
  `telegram_user_id` (indexed), `chat_id`, `provider`, `mcp_server`, `expires_at`,
  `created_at`). `database/oauth.py::OAuthStorageImpl` is the SQLite implementation of
  the `mcp/auth/storage.py::OAuthStorage` ABC: `save_credential` is an **upsert** on
  the unique triple (re-authorization overwrites, never duplicates),
  `get_credential`/`has_credential` are **filtered by `telegram_user_id` in SQL** (a
  foreign user's credential is indistinguishable from a missing one — no existence
  leak), `create_pending` / `consume_pending` (select + delete **in one unit of work**
  → single-use) / `delete_pending` (best-effort no-op when absent), and datetimes
  stored naive by SQLite are normalised back to **tz-aware UTC** on read.
  `reset_conversation` explicitly deletes the old conversation's attachments and
  messages — **and never touches `memories`, `tool_audit_events`,
  `oauth_credentials`, or `oauth_authorization_states`** (memory is per-`scope`, the
  audit log is a global, append-only trail, and OAuth credentials are per-*user* — a
  `/new` must never force re-authentication; this is regression-tested). `init_db`
  (`create_all`) creates the new tables on a fresh DB *and* adds any missing one to an
  existing DB — no data loss, no manual `data/agent.db` wipe, no Alembic.
  **Phase 9 (Automation)** reuses the `conversations` table for scheduled-run venues —
  **no new table, no migration** — by reserving a slice of the
  `telegram_chat_id` space: `SCHEDULE_CHAT_ID_BASE = 9_000_000_000_000_000_000` and
  `SCHEDULE_CHAT_ID_MAX = BASE + 2**32` (a 2³² window ~10⁷× above any real chat id, so
  a real interactive chat can never collide with it), and
  `schedule_chat_id(name) = BASE + int(sha256(name.encode())[:8], 16)` — a
  **deterministic** synthetic id (same name → same id every call; distinct names
  collide only with negligible 32-bit probability, and a collision is harmless). The
  repository gains `delete_conversation(conversation_id)` (the runner's `finally`
  teardown: explicitly removes the row + its messages + its attachments, **not**
  relying on FK cascade; a missing id is a no-op returning `False`) and
  `clear_ephemeral_conversations()` (the startup sweep: deletes **only** conversations
  whose `telegram_chat_id` falls strictly inside the reserved range, together with
  their messages + attachments, returning the count — a real chat id is never in range,
  so it is a no-op returning `0` for the empty-`SCHEDULES` case). `reset_conversation`
  is unchanged but doubles as the **self-heal**: a killed run's leftover venue row is
  wiped on the next `reset_conversation` for that name-derived id.

- **`main.py`** — the composition root. When `ENABLE_TOOLS=true` it builds the
  registry (`build_default_tools()`), the policy (**`FileBackedToolPolicy(config.
  mcp_permissions_file, registry)`** when `MCP_PERMISSIONS_FILE` is set, else a plain
  **`build_policy({}, registry=)`** — so with no file the built-ins ride their declared
  defaults and everything else defaults `ask`), the auditor
  (`RepositoryToolAuditor(repository)`), the Telegram approval broker
  (`TelegramApprovalBroker(repository)`), and — when a tool `registry` exists — a
  `QQApprovalBroker()` plus a `QQScopedApprovalRouter` over the two (scope prefix
  `qq:` → QQ broker, else Telegram); the **router** is what it hands as the
  `approval_provider` to `AgentService` (so the single service serves both channels),
  along with the policy/auditor + both timeouts; when `ENABLE_TOOLS=false`, all of
  these are `None` and the service degrades fully to Phase 1. It passes the Telegram
  broker to
  `build_application(config, service, repository, approval_broker=)` (which binds the
  app to the broker and registers the callback handler), and on shutdown calls
  `approval_broker.shutdown()` (resolving any pending approvals as `EXPIRED`) **and**
  `self._qq_approval_broker.shutdown()` **before** closing the LLM client. **Phase 4 adds the MCP lifecycle**: it
  constructs an `McpManager` **only** when `ENABLE_TOOLS=true` *and*
  `config.mcp_servers` is non-empty (otherwise `None` — no MCP connection / stdio
  process is ever opened), starts it in the `_post_init` startup hook (chained with
  `compose_startup_hooks`, after DB init), registers its discovered tools into the
  registry, and calls `manager.close()` on shutdown (before the LLM client).
  **After** that `registry.add` (the only point where the full MCP set is known) it
  seeds/syncs the dedicated permissions file via
  `reconcile_permissions_file(config.mcp_permissions_file, [t.name for t in
  mcp_manager.tools()])` — only when the file is configured *and* an MCP manager
  exists — wrapped in a try/except that logs ERROR (path + exception **class** only)
  and never blocks boot (a pre-existing file was already validated at config-load;
  this is a race guard, and the file is hot-reloaded on read). The manager (and its
  status) is exposed to the Telegram layer via `app.bot_data` for the read-only
  `/mcp_status` command. **Phase 4.x adds the OAuth lifecycle** in the composition
  root (`main.py`): `_setup_oauth()` builds the provider registry via
  `_build_provider(provider_name)` — the **single** place in the codebase that
  branches on a provider name (`google` reads `GOOGLE_OAUTH_CLIENT_ID`/`SECRET`/
  `SCOPES` from the environment *there only*; any other name → `None`) — and
  constructs the `OAuthManager` **only** when a callback base URL is set, at least one
  server declares `auth_type == "oauth"`, and at least one provider actually built (a
  missing credential set leaves OAuth off, not an error). It passes the
  `oauth_auth_factory` to the `McpManager` constructor (so oauth servers get a
  `McpOAuthAuth` instead of a bearer header), stores the manager in `app.bot_data`
  for the `/mcp` command, and injects `_oauth_notifier` (the async hook that messages
  the user in their chat after a callback outcome). The callback server is
  `start()`ed in `_post_init` (after DB init, only when the manager exists) and
  `stop()`ed first in `_post_shutdown` (idempotent). Nothing OAuth-specific ever
  reaches `AgentService`, the LLM client, or the conversation store. **Phase 5.1 adds
  the infra lifecycle**: in `__init__` it builds
  `self.infra_tools = build_infra_tools(config.infra_ssh_targets,
  connect_timeout_seconds=…, max_result_chars=…)` **only** when
  `config.enable_tools and config.infra_ssh_targets` (else `[]` — no provider,
  `asyncssh` never imported); in `_post_init`, **after** the MCP `registry.add`, it
  `registry.add(*self.infra_tools)` atomically (any `ValueError` → a startup
  `ConfigError` naming the colliding tool) and records the count in the init log. The
  MCP permissions-file reconcile still passes **only** the MCP tools — infra tools
  are local and are **never** seeded into `MCP_PERMISSIONS_FILE`. **The schedule
  runner is channel-aware (phase 9 + 10):** `_run_schedule(spec)` derives the
  dedicated-venue row principal from the spec's `identity` —
  `spec.telegram.user_id` for a telegram-identity run, `qq_chat_id(spec.qq.user_openid)`
  for a qq-identity run — and calls `process_message` with `spec.memory_scope()`
  (`telegram:<user_id>` or `qq:<openid>`) and `spec.approval_delivery_chat_id()`
  (`receiver.telegram.chat_id` for telegram identity, `None` for qq — the QQ approval
  broker routes by the `qq:` scope prefix, not a chat id). Delivery is delegated to
  `_deliver_schedule_notification(spec, text)`, which best-efforts the formatted
  notice (task name + result, or the fixed safe `AgentError` phrase) to **every**
  present `receiver` channel — telegram via `deliver_markdown`, qq via
  `deliver_qq_markdown` — where a failure (or a `qq` receiver while the QQ channel is
  not running, `self._qq_client is None`) is logged by **schedule name only**, never
  blocks the other channel, and never raises; the `finally` still deletes the venue.
- **`automation/`** — phase 9 (Automation). Two pure modules, **no** Telegram /
  OpenAI SDK / ORM / AgentService imports: **`cron.py`** is a zero-dependency,
  **strict 5-field** cron grammar (`parse_cron` → frozen `CronSpec`): per-field
  `*` / value / `a-b` (inclusive) / `*/n` / `a-b/n` / comma lists (mixable), month
  names `JAN`–`DEC` + day names `SUN`–`SAT` (case-insensitive), **`0` and `7` both
  = Sunday** (`7` normalises to `0`), the **Vixie day-of-month / day-of-week OR
  rule** (both restricted → *either* matching day fires; one restricted → that one
  wins), and strict rejections (`CronError`, never a silent "never fires") of wrong
  field count, `?`, `@daily`-shorthands, inverted ranges, out-of-bounds values,
  empty fields, and unknown tokens. `CronSpec.next_fire(after, tz)` is a **strictly
  after**, **bounded** (`_MAX_SEARCH_DAYS = 1830`) wall-clock search in the
  caller's `zoneinfo` timezone; a calendar-impossible expression (Feb 31) returns
  `None` rather than looping forever. **`scheduler.py`** is a single background
  `asyncio.Task` (`Scheduler`) that only (1) watches an **injected clock**
  (`now_fn`) and (2) fires an **injected** `runner` coroutine when a schedule is
  due — deliberately channel- and service-agnostic. Invariants: **no catch-up**
  (every next fire is recomputed from *now* at start and after each fire, so a
  fire missed while down is never replayed); **per-task single-flight** (a
  previous in-flight run makes the next due tick *skip* — safe log — and advance
  the due time, so a stuck turn never builds a queue); **fault isolation** (each
  run is wrapped; one schedule's runner exception is logged by **name + exception
  class only**, never the text, and never stops the loop or the others);
  `start`/`stop` are **idempotent, never raise**, and `stop` cancels the loop then
  gives in-flight runs a **bounded** chance to finish (a genuinely stuck one is
  cancelled) so shutdown never hangs; sleep is capped at 30 s so `stop` latency
  stays bounded. The channel-aware runner (dedicated fresh conversation →
  `process_message` under the spec's `identity` → multi-channel notification →
  `finally` cleanup) lives in the composition root (`main.py::_run_schedule`), not
  here — the scheduler itself only knows a schedule's `name` + `cron`.

---

## Gotchas that are easy to get wrong

- **This PTB build strips `CallbackContext` shortcuts**: `context.user` / `.chat` /
  `.message` (and `.user_id`/`.chat_id`/`.effective_user`) do **not** exist here —
  only `.application`, `.bot`, `.bot_data`, `.error`, `.user_data`, `.chat_data`, …
  Reading any of the stripped attributes raises `AttributeError` at runtime. Handlers
  therefore read the sender/chat/message from the **`Update` object**
  (`update.effective_user` / `.effective_chat` / `.effective_message`). This build also
  has **no `Middleware` API**, so auth is per-handler. `Chat`/`Update`/`User`/`Message`
  are frozen `TelegramObject`s (you can't set attributes on instances — patch
  `Chat.send_message` at the **class** level in tests).
- **OpenAI is a fork**: the installed `openai` uses `httpx2` (not plain `httpx`) as its
  HTTP layer. When mocking `client._client.chat.completions.create`, construct error
  responses with `httpx2.Response(...)` (see `tests/test_llm_client.py`).
- **This PTB `PhotoSize` has no `download_as_bytearray`** — only `get_file()` (→ a
  `telegram.File`, which *does* have `download_as_bytearray()`/`download_to_memory()`).
  In `telegram/media.py` the flow is `await photo[-1].get_file()` then
  `await file.download_as_bytearray()`. In tests, patch `PhotoSize.get_file` to return
  a fake `File` (see `_patch_download` in `tests/test_multimodal.py`); you cannot patch
  `download_as_bytearray` on `PhotoSize` directly (it isn't there).
- **A Telegram photo's text lives in `message.caption`, not `message.text`** (which is
  `None` for a bare photo). The handler filter is `TEXT | PHOTO`, and
  `telegram/media.py::normalize_message` reads the caption into a `TextContent`. An
  image-only message therefore persists an empty-string user turn — that is correct,
  not a bug. (Phase 2.3: the *image itself* is persisted as a blob + `attachments`
  row; the empty text is just the missing caption.)
- **Never send raw Markdown to Telegram — always render it to HTML first.** Telegram
  does not render Markdown, so any reply whose text still contains `**…` / `_…_` /
  backticks sent with `parse_mode=HTML` (or with no parse mode) shows the markup
  literally. The one path is: build the text in the supported Markdown subset, then
  route it through `telegram/markdown.py` — `_send_long` (→
  `to_telegram_html_chunks`) for normal/chunked replies, or `to_telegram_html(text)`
  for a single message that carries an inline `reply_markup`. This is the exact bug
  the `/mcp` replies had (status view + OAuth login prompt built Markdown but sent it
  straight with `parse_mode=HTML`, so `**server**` rendered as literal asterisks); the
  fix was to route both through the renderer. When adding a command or any user-facing
  notice, default to `_send_long` and only send with `parse_mode=HTML` on text you have
  already passed through `to_telegram_html`/`to_telegram_html_chunks`.
- **SQLAlchemy**: sessions use `expire_on_commit=False`. Do **not** read lazy-loaded
  ORM attributes (e.g. `conversation.messages`) after the session closes — use the
  repository's scalar return types (`MessageRecord`). SQLite has FK enforcement turned
  **on** via a connect event (needed for cascade deletes on reset).
- **`conversations.id` uses `sqlite_autoincrement`** so a `/new` always yields a new,
  larger id (visible to the user). Don't remove it.
- **Logging never leaks secrets or media**: `tool_loop.py` logs only
  `tool requested: <name>` / `tool completed: <name> latency=<N>ms`; the
  tool-security paths log only the tool *name*, the stable event/result *code*, a
  **short, irreversible scope hash** (`hash_scope`), and — on an execution exception —
  the exception **class** (`type(exc).__name__`, e.g. `ValueError`), **never the
  exception message/text**; `telegram/media.py` logs only `message_id`/`mime_type`/
  `size_bytes`; `attachments/store.py` logs only a **short digest prefix**
  (`digest[:8]`), byte counts, and the operation result; the memory paths (service +
  repository) log only a **short, irreversible scope hash** (`hash_scope`, a 12-char
  salted SHA-256 prefix), `memory_id`, `content_length`, retrieval `hits`,
  `memory_cost`, and the stable category — **never** the raw scope, the user id, the
  memory **content**, the retrieval query, the full user message, an image, a digest,
  or a path. The **audit log** stores only `scope_hash` + tool name + event type +
  stable code + nullable call-id/iteration/latency — **never** arguments, results, or
  the raw scope. The **MCP paths** (manager + wrapper + `/mcp_status`) log/audit/status
  only the server *name*, the URL *scheme/host hash* (never the full URL), and stable
  status codes — **never** the endpoint URL, the `Authorization` header or bearer
  token, the stdio **`command`/`args`/`env`/`cwd`**, tool arguments, tool results, the
  remote **exception body** (only the exception **class**, `type(exc).__name__`), the
  server's **instructions**, or the raw scope/user id. The **OAuth paths**
  (`mcp/auth/*`, `database/oauth.py`, the callback server, `/mcp`) log only stable
  outcome/state *codes* and server names — **never** an access token, a refresh token,
  an authorization **code**, the client **secret**, a `state` value, the
  `Authorization` header, or the **full callback URL** (its query string carries
  `state`+`code`); provider errors are fixed, non-echoing messages (the provider
  response body is never logged), and `/mcp` output is status-class only (no token,
  scope string, or provider detail). The **infra paths** (`infrastructure/*`,
  `/infra_status`) log/audit/status only the tool name and the stable result code —
  **never** the host/IP, the private-key path, the known-hosts path, the username, a
  mount path, the service/unit name in a *log* (a unit name may appear in
  *config-validation* errors and on the `/infra_status` list, but the *log* carries
  only tool + code + exception class), the remote **command**, or any
  **stdout/stderr** (only the exception **class**, `type(exc).__name__`);
  `/infra_status` output is configuration-name only (target name + the three tool
  names — no host/port/user/path/command). Never log the API key, Telegram token,
  `Authorization` header, full message/tool-result bodies, **tool arguments, tool
  results, or exception text**, a **full digest**, a **storage path**, a **caption**,
  **image bytes, or base64 image data**, an **MCP endpoint/URL**, an **MCP token**, an
  **MCP stdio `command`/`args`/`env`/`cwd`**, any **OAuth token / code / client secret
  / state**, or an **infra host / key path / known-hosts path / mount path / command
  output** (the LLM client's `_safe()` deliberately strips request headers). The **QQ
  adapter** (`qq/bot.py`) logs only the synthetic conversation id, the QQ *message id*,
  and a text length — **never** the raw `user_openid` (a user identity), the message
  **body**, the **reply** body, or `QQ_CLIENT_SECRET`. Keep it
  that way when adding tools, media types, memory commands, MCP servers, OAuth
  providers, or infra targets.
- **Approval is a callback / button, not a tool**: the `Approve`/`Deny` decision
  reaches the tool loop only through the injected `ToolApprovalProvider` — for a
  Telegram turn the broker's `CallbackQueryHandler` (a PTB **callback-query**
  handler), for a QQ turn the `QQApprovalBroker.handle_interaction` (a
  `botpy` `interaction_create` event). Either way it is not part of the tool registry
  and the model can never request, invoke, or "approve" a tool by emitting text — the
  only path to approval is the owner pressing the button in the bound chat. Don't add
  an `approve`/`confirm` *tool*; that would let the model grant itself approval.
- **`botpy` owns its event loop and would drop a stray log file**: `botpy.Client`
  must be **constructed and started on the running PTB loop** (`main.py::_post_init`)
  — its `__init__` grabs `asyncio.get_event_loop()` and `run()` is **blocking** (it
  owns a loop via `run_until_complete`), so the backend drives it as an `asyncio.Task`
  via `async with client: await client.start(app_id, secret)`. In `_post_shutdown`
  the backend `close()`s the client and then cancels **every task the QQ subsystem
  spawned on that loop** — not just the outer task (never call the blocking
  `run()`). `close()` only closes the HTTP client; the SDK's own `ConnectionSession`
  runner, `BotWebSocket` receive loop and `_send_heart` heartbeat coroutines
  outlive it, and the outer task (suspended in `asyncio.wait`) swallows its own
  cancellation, so without an explicit teardown the loop's close destroys them
  mid-`aiohttp`-teardown (`Task was destroyed but it is pending` /
  `RuntimeError: coroutine ignored GeneratorExit` on Ctrl+C). The teardown
  (`main.py::_qq_shutdown_tasks`) diffs `asyncio.all_tasks()` against a baseline
  snapshot taken just before the QQ task started, so it cancels only QQ-created
  tasks and leaves unrelated in-flight work (a Telegram approval callback, a
  scheduled run, the OAuth callback server) untouched. And pass
  `ext_handlers=False` in `build_qq_client` — botpy's **default** `ext_handlers=True`
  installs a `TimedRotatingFileHandler` that writes a rotating `botpy.log` into the
  *current working directory* (repo root in dev, `/app` under Docker); `bot_log=True`
  keeps its lifecycle logs propagating to our already-configured root logger instead,
  so no stray file appears.
- **QQ turns can now be approved for `ask` tools via a QQ button card** (`qq/
  approval.py::QQApprovalBroker`), selected per-request by the `QQScopedApprovalRouter`
  (scope prefix `qq:` → QQ broker, else Telegram). The card is an **active** C2C Markdown
  message (no `msg_id`, so no 5-min passive window / no dedup collision) with a
  `keyboard` of `action.type=1` callback buttons; the click arrives as
  `INTERACTION_CREATE` (Intent bit `1<<26`, event `type=11`) →
  `client.on_interaction_create` → `handle_interaction`, which must **ack within 3 s**
  (`client.api.on_interaction_result(interaction_id, code)`) or the client spins.
  Binding is by the clicker openid's `hash_scope` fingerprint (a C2C chat is one-to-one,
  so principal == chat); a foreign/unknown/expired/repeat click voids the pending
  request (resolves `EXPIRED`) and acks `code=1` — never executed, never a leak.
  `deny` tools are still rejected; only `ask` is approvable. **Do not** route a QQ
  approval back to Telegram — each channel owns its own approval UI.
- **QQ C2C dedups on `(msg_id, msg_seq)`**: a reply is `message.reply(msg_type=…, msg_seq=…)`
  keyed to the *incoming* message id. The QQ API
  **rejects a re-sent identical pair**, so a multi-chunk reply must send each chunk with an
  incrementing `msg_seq` (1, 2, 3, …) — reusing `msg_seq=1` for every chunk would land
  only the first. Delivery type is chosen per reply by `_send_long(markdown=…)`: the
  **agent-turn answer** and **structured** command displays go out as Markdown (`msg_type=2`,
  text in the nested `markdown.content`) so the QQ client renders them, sent **verbatim**
  (no conversion/escape layer); **simple** one-line command receipts (and the short error
  notice) go out as plain text (`msg_type=0`).
- **QQ `message_reference` is a quote, *distinct* from the `msg_id` thread.** `message.reply`
  always passes the incoming `message.id` as the passive-reply `msg_id` (how QQ knows which
  message is being answered). The *visible* quote is a separate `message_reference`
  `{"message_id": str(id), "ignore_get_message_error": True}` — only the answer's **first**
  chunk carries it (quote-once, mirroring Telegram). Do not conflate the two: dropping the
  quote is a cosmetic loss, but omitting `msg_id` breaks the passive reply itself.
- **QQ slash-commands arrive as plain text; only a *known* leading token is intercepted.**
  QQ has no separate command event — `/new` is just a C2C message whose `content` starts
  with `/`. The dispatcher intercepts **only** a token in `commands.known_command_names()`;
  an unknown `/…` **must** fall through to the normal agent turn (never swallowed), so an
  accidental `/foo` is still answered by the model, not eaten. Command messages are not
  stored as conversation turns (matching Telegram) — the agent only sees non-command text.
- **The QQ `/stop` registry is QQ-local (`QQChannel._in_flight`), not shared with Telegram.**
  botpy invokes each C2C message as its own `asyncio.Task`, so `asyncio.current_task()` at
  turn start is the cancellable handle; a later `/stop` (a separate message → separate task)
  pops and cancels it. The handle **must** be removed in a `finally` (completion *and*
  cancellation) so a finished/stopped turn never lingers as a stale, cancellable handle,
  and the cancelled turn's own "已停止" notice is best-effort (a failed send must not mask
  the re-raise). Do not reach across into the Telegram layer's `_IN_FLIGHT` — the two
  channels have disjoint conversation-id spaces and lifecycles.
- **The QQ command panel is idempotent via the `remark` marker, and its failures are swallowed.**
  The panel API is *not* idempotent — a blind `POST /v2/panels` on every restart stacks up to
  20 identical panels. `_ensure_c2c_panel` first `GET /v2/panels?scope=c2c`, finds the record
  whose `panel.remark == "fibrecase-c2c"`, and `PUT`s it (with the record's `version` for
  optimistic locking) or, if absent, `POST`s. It runs from `on_ready` (token valid post-login)
  and **never raises** — a panel hiccup (network, a 4xx) is logged by class and swallowed so
  it can never break startup or message handling. botpy has **no** `menu`/`panels` wrapper;
  the calls are raw `self.http.request(Route(…), json=… / params=…)` (the same primitive
  botpy's own `api.py` uses), lazy-imported so a Telegram-only deploy never touches `botpy`.
- **The QQ global custom menu is idempotent *by replace* (no remark), and its failures are swallowed.**
  Unlike the command panel, `PUT /v2/menu` **replaces the entire** global C2C menu with the
  body, so `_ensure_global_menu` fires a single `PUT` of the fixed two-item payload on every
  `on_ready` — there is no create-or-update dance and no `remark` marker to match (a blind
  re-send is safe). It is a **global, owner-configured** resource (visible to every C2C user),
  not per-user like the panel. It **never raises** — a menu hiccup is logged by class and
  swallowed so it can never break startup or message handling, exactly like the panel. The
  two items are fixed, secret-free literals (no openid, command argument, or message body).
- **A QQ command's *argument* is logged-unsafe even though its *name* is safe.** `/remember`
  / `/forget` arguments carry memory content; `/tool_audit`'s is a number; the rest are empty.
  The QQ adapter logs the command **name** and the synthetic conversation id, but **never**
  the argument string (nor the raw openid or the reply body) — assert this in the command
  tests alongside the feature.
- **Pre-execution audit is fail-closed; terminal audit is best-effort**: `record_pre`
  (the `requested` record) must succeed or the tool does **not** run
  (`audit_unavailable`); a failure writing a *terminal* record (completed/timed_out/
  failed/…) is logged and the turn continues — the tool must **not** be re-executed to
  re-attempt the audit write.
- **A found-but-invalid approval click voids the pending request immediately**
  (resolves `EXPIRED` + drops it). Don't "fix" a foreign/repeat/stale/expired click
  into a no-op that leaves the `Future` unset — that would leave the waiting
  `request_approval` task hanging until the full approval timeout, stalling the whole
  conversation.
- **Only `scope_hash` ever reaches the audit table** (the raw `scope`/user id is hashed
  by `hash_scope` at the `RepositoryToolAuditor` boundary). `/tool_audit` and the audit
  row both must never expose the raw scope, the bare user id, tool arguments, or tool
  results.
- **`attachments` records must be read detached**: `get_messages_with_attachments`
  eager-loads attachments (``selectinload``) and materialises them into
  `AttachmentRef` while the session is open, so the returned `MessageWithAttachments`
  is safe after the session closes. Do **not** touch a live ORM relationship
  (`message.attachments`) after the session closes — it would raise
  `DetachedInstanceError` (same rule as `Conversation.messages`).
- **`/new` reclaim never deletes a shared blob**: `reset()` snapshots the dropped
  chat's digests, then deletes a blob **only if** `distinct_attachment_sha256` no
  longer lists it. A blob referenced by *any* other conversation must survive. A
  delete that fails (or a blob already missing) is logged and **must not** prevent the
  new conversation from being created.
- **Rehydration is plan-scoped**: the phase-2.4 planner selects the turns *before* any
  rehydration, so only a **selected** turn's blobs are read from disk; a turn that was
  dropped (message cap), downgraded (token budget), or is simply out of range has its
  image blob **never read** and never sent. Don't "fix" this by rehydrating everything
  and truncating/planning after — it would read (and could ship) images the user can
  no longer see.
- **`storage_key` is never user input**: blob paths derive only from the SHA-256
  (validated to 64 lowercase hex) — never from a filename, caption, or Telegram
  `file_id`, which are the only things that could carry a path traversal.
- **OAuth: the pending state is the only authority on the callback's target**.
  `complete_authorization` reads `(user, chat, provider, server)` from the *stored*
  pending record — the provider redirect carries only `code` + `state`. Don't
  "simplify" by trusting `telegram_user_id`/`provider`/`mcp_server` query parameters
  on the callback; that is exactly the forged-binding attack (spec §28). The state is
  consumed (select+delete, one unit of work) **before** the code is exchanged — a
  replay must find nothing.
- **OAuth: a refresh failure must NOT delete the credential.**
  `valid_access_token` keeps the row on a provider refresh error and returns `None`;
  the user sees "expired" and re-authenticates via `/mcp auth`. Deleting on failure
  would silently log the user out and destroy the only recovery path. Likewise a token
  expiring within the last **60 s** (`_EXPIRY_SKEW`) is still treated as valid to avoid
  a refresh stampede at the boundary.
- **OAuth: the credential binds to the Telegram *user*, never the conversation.**
  `oauth_credentials` is keyed by `(telegram_user_id, provider, mcp_server)` — a
  `/new` (new conversation id), a new chat, or a restart must **never** force
  re-authentication, and `reset_conversation` must not touch either OAuth table
  (regression-tested). Conversely, user A's credential is invisible to user B
  (filtered in SQL; a foreign lookup is indistinguishable from missing) and `/mcp`
  must only ever render the *caller's own* state.
- **OAuth: the per-user token rides a ContextVar, not the loop's arguments.**
  `run_tool_loop` sets `active_principal` around `tool.execute()` (reset in
  `finally`); `McpOAuthAuth` (an `httpx2.Auth` on the http client) reads it to pick the
  user's bearer token. There is deliberately **no** `user_id` parameter threaded
  through `Tool.execute` — don't add one; the contextvar is the channel. A call with
  no principal (the startup handshake) must send **no** `Authorization` header, and a
  token-resolution failure must still send the request (headerless) rather than
  raising into the gate.
- **OAuth: the callback server is the only inbound listener, and it lives on PTB's
  loop.** It starts in `_post_init` *only* when the `OAuthManager` exists (callback
  base configured + an oauth server + a buildable provider) and stops first in
  `_post_shutdown`. It has exactly one route (`GET /oauth/callback`) + a fixed 404 —
  don't add routes. Its `notifier` is an injected async hook (the manager never
  imports Telegram), and a notifier failure must never change the callback outcome.
- **OAuth: the Google branch lives in exactly one place.**
  `main.py::_build_provider` is the *only* `if provider_name == "google"` in the
  codebase and the only place `GOOGLE_OAUTH_CLIENT_ID/SECRET/SCOPES` are read. Adding
  a provider = implement the `OAuthProvider` ABC + one line there; a scattered
  `if … == "google"` anywhere else is a spec violation.
- **OAuth: `McpManager.status()` stays 3-field.** The stable OAuth failure codes (e.g.
  `mcp_oauth_not_configured`) are **log-only** — `status()` still returns exactly
  `name`/`available`/`tool_count`, so `/mcp` and `/mcp_status` can't leak a failure
  reason (or URL) to the user.
- **`/mcp` is one command, not two.** `/mcp auth <server>` is `cmd_mcp` dispatching on
  its first argument — there is no `mcp_auth` `CommandHandler` (it would never match
  `/mcp auth …`), and a stray non-`auth` argument still shows the status view without
  starting a flow. The login URL goes out as an `InlineKeyboardButton` — never as a
  bare URL the user must copy.
- **`ChatMessage` is defined twice** — one in `agent/context.py` (built for the agent,
  used by the tool loop) and one in `llm/client.py` (the wire type the client
  serialises). Both now carry the same optional `tool_calls`/`tool_call_id` fields and
  both `to_dict()` identically for a plain message. Keep them in sync if you change
  either.
- `data/` and its `.db` are created automatically at startup (`create_engine` makes
  the parent dir). The shared `repo` test fixture is **file-backed** SQLite, built
  through the same production `create_engine` / `create_session_factory` / `init_db`
  helpers (default pool, in a `TemporaryDirectory`) so its access pattern matches
  the app: the repository is written concurrently, and a single shared in-memory
  connection (the old `StaticPool` fixture) is not safe for two sessions — the
  second `commit` closes the shared connection and detaches the first session's
  in-flight instance, surfacing as a flaky `DetachedInstanceError` /
  `InvalidRequestError` on `refresh`. Keep the `repo` fixture file-backed; other
  single-session DB tests may still build their own in-memory / `tmp_path` engine.

---

## Configuration knob reference

The full, strictly-validated config reference. (User-facing defaults live in
`configuration.md`; this is the developer's authoritative list of what each knob does,
its validation, and the cross-knob invariants. All come from `config.py::load_config`
/ `Config.__post_init__`.) Secrets (`TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`,
`QQ_CLIENT_SECRET`) are env-only and must never be committed — `.env` and `data/`
are git-ignored.

Two non-obvious rules:
- **`OPENAI_BASE_URL` is the API *prefix*** (e.g. `https://<host>/v1`), **not** the
  full `.../chat/completions` URL. The OpenAI SDK appends `/chat/completions` itself.
  (Verified against a local HTTP server: with base `.../v1` it requests exactly
  `/v1/chat/completions`.) Setting it to the full URL is the #1 misconfiguration.
- `SYSTEM_PROMPT` (env) overrides `SYSTEM_PROMPT_PATH` (file) which overrides a
  built-in fallback. File is the intended default (`config/system_prompt.txt`).

### LLM / OpenAI knobs

- **`REASONING_EFFORT`** (default `low`): the reasoning-effort knob sent to the
  provider on **every** completion — a deployment-wide default, not a per-message
  override, exactly like `OPENAI_MODEL`. It is always present on the request (never
  omitted). Validated against `low` / `medium` / `high` / `xhigh`
  (case-insensitive) at
  startup; any other value raises `ConfigError` (fail-fast). Only meaningful for
  reasoning models — a non-reasoning model that rejects the field will surface the
  provider's error on the first call, so leave it at a value your endpoint accepts.

### Tool-calling knobs (phase 2.1)

- **`ENABLE_TOOLS`** (default `true`): when `false`, `AgentService` skips the tool
  loop entirely and behaves exactly as Phase 1 (one LLM call, no `tools` advertised,
  nothing tool-related persisted). It is a hard, complete degradation switch.
- **`MAX_TOOL_ITERATIONS`** (default `20`): hard cap on LLM↔tool round-trips per
  message. Hitting it raises a `ToolLoopLimitError`, surfaced to the user as a generic,
  user-safe "too many tool calls" message (category `tool_limit`).

### Streaming replies knob (Bot API 10.0 `sendMessageDraft`)

- **`ENABLE_STREAMING`** (default **`true`**): the opt-in switch for *streaming*
  (draft-preview) replies. When on, a **private** chat shows a live Telegram *draft*
  in the compose box that animates as the model generates (via
  `telegram.ext`'s `sendMessageDraft`), **in parallel with** the "typing…" keep-alive
  (the typing action is the fallback that stays visible if the draft can't be shown —
  i.e. the bot isn't on Telegram's streaming allowlist). **Group/channel chats always
  degrade** to the classic "typing…" + chunked final reply (a draft only applies to
  private chats). When off, every chat uses the classic "typing…" + chunked reply
  path. Parsed as a strict boolean like every other knob (`ConfigError` on a bad
  value, fail-fast).

  The full mechanism is described in the
  [Streaming replies](#streaming-replies--bot-api-100-sendmessagedraft) phase note below
  and the module reference entries for `llm/client.py` (the `on_text_delta` seam) and
  `telegram/bot.py` (`_DraftStreamer`).

### Multimodal-input knob (phase 2.2)

- **`MAX_IMAGE_SIZE_MB`** (default `10`): a Telegram photo larger than this is refused
  with a user-safe "图片过大" message (category `image_too_large`) before anything
  reaches the LLM. The cap is enforced in `telegram/media.py` (the adapter), the
  single gatekeeper.

### Attachment-storage knob (phase 2.3)

- **`ATTACHMENT_STORAGE_PATH`** (default `./data/attachments`): the root directory for
  the content-addressed image blob store. Relative paths stay relative to the working
  directory (like `DATABASE_URL` / the system prompt); the directory is created on
  demand. Under Docker the default is inside the `./data:/app/data` bind mount. Only
  blob *bytes* live here — the DB holds metadata only.

### Context-budget knobs (phase 2.4)

- **`MAX_CONTEXT_ESTIMATED_TOKENS`** (default `200000`): a **conservative,
  model-agnostic estimate** of the total prompt (system + selected history + current
  user turn). It is *not* a provider billing token count — there is no
  model-specific tokenizer. It is an independent, second limit that works alongside
  `MAX_CONTEXT_MESSAGES`; the effective context must satisfy **both**.
- **`CONTEXT_IMAGE_ESTIMATED_TOKENS`** (default `2000`): the estimated cost attributed
  to each image kept in context, used by the planner to decide whether a history turn's
  images fit.

Both are validated as positive integers (`>= 1`) in `Config.__post_init__`; a
zero/negative/non-integer value raises `ConfigError`.

### Memory knobs (phase 2.5)

- **`MAX_MEMORIES_PER_SCOPE`** (default `200`): the maximum number of memories one
  principal (`scope`) may save. Reaching it makes `/remember` fail with
  `memory_limit` (English "Memory limit reached for your account. Forget a memory
  first.").
- **`MAX_MEMORY_CHARS`** (default `1000`): maximum length (chars, after trim) of one
  saved memory. `/remember` of empty or over-length text fails with `memory_invalid`.
- **`MAX_RETRIEVED_MEMORIES`** (default `5`): the cap on how many relevant memories are
  retrieved (and thus eligible for injection) per message.
- **`MAX_MEMORY_ESTIMATED_TOKENS`** (default `3000`): a phase-2.4 **estimated-unit
  sub-budget** for the injected reference memory (the same model-agnostic estimate, not
  a billing count). A memory that would push the single reference message over this
  sub-budget is skipped (never truncated). It is validated as `>= 1` **and** as
  `<= MAX_CONTEXT_ESTIMATED_TOKENS`; a larger value raises `ConfigError`.

All four are validated as positive integers in `Config.__post_init__`; the
cross-knob check (`max_memory_estimated_tokens > max_context_estimated_tokens`) is the
only cross-knob invariant.

### Tool-security knobs (phase 3)

- **`TOOL_APPROVAL_TIMEOUT_SECONDS`** (default `60`): how long to wait for a Telegram
  `Approve`/`Deny` decision on an `ask` tool call before it expires
  (`approval_expired`). Must be `> 0`.
- **`TOOL_TIMEOUT_SECONDS`** (default `30`): the maximum execution time for one tool
  invocation; on timeout the tool is cancelled via `asyncio.wait_for` and the model is
  told it timed out (`tool_timeout`). Must be `> 0`.

Both timeout knobs are validated as positive numbers (`> 0`) in
`Config.__post_init__`; a zero/negative value raises `ConfigError`. (No separate flag
enables the audit log — it is on whenever `ENABLE_TOOLS=true`, and is viewable via
`/tool_audit`.)

### MCP knobs (phase 4) — MCP tool provider over Streamable HTTP + stdio

The server list comes from a standalone `MCP_SERVERS_FILE` when that is set, else the
inline `MCP_SERVERS`.

- **`MCP_SERVERS`** (default empty; or the file `MCP_SERVERS_FILE`, below): a JSON
  **array** of server objects. Each has a `name` (must match
  `[a-z][a-z0-9_-]{0,31}`) and an optional `transport` (`"http"`, the default, or
  `"stdio"`). **http**: `url` (required, absolute `https://` with a host and **no**
  userinfo/fragment/query — a query-string token is rejected and never echoed) +
  optional `bearer_token_env` (an **env var name**, value read only when the http
  client is built as the `Authorization: Bearer` header — never stored/logged/echoed;
  must be a valid, non-empty name) + optional `authentication`. **stdio**: `command`
  (required, bare executable name or path, `[A-Za-z0-9_./-]{1,256}`, **no shell**) +
  optional `args` (array of non-empty strings, passed verbatim — no glob/`$VAR`) +
  optional `env` (object of env-var name → non-empty string) + optional `cwd` (path).
  **Mutually exclusive**: an http entry must not set `command`/`args`/`env`/`cwd`; a
  stdio entry must not set `url`, `bearer_token_env`, or `authentication` (a spawned
  process has no HTTP request to carry an auth header — credentials go in its
  `env`). Empty = no MCP client is constructed and **no MCP connection / stdio process
  is ever opened**. Parsing is **strict and fail-fast at startup**: a non-array, a
  non-object entry, an unknown field, a bad server name, an illegal `transport`, a bad
  URL, a missing/illegal `command`, a bad `args`/`env`/`cwd`, a duplicate name, a
  transport-field mismatch, or a `bearer_token_env` that is empty or not a valid
  env-var name all raise `ConfigError`. Errors name only the server and the field —
  **never** the token value, the full URL, or the stdio `command`/`args`/`env`/`cwd`
  (an `env` error names the **key**, never the value).
- **`MCP_SERVERS_FILE`** (default empty): a path (relative to the working directory)
  to a standalone file holding the **same** JSON **array**. When set and non-empty it
  **wins over** the inline `MCP_SERVERS` (which is then ignored — use one or the other).
  This is the **preferred** source for multiple / stdio servers. A set-but-
  **missing/unreadable** file, or one that is **blank** (0-byte / whitespace-only), is
  a startup `ConfigError` naming the path (it must never silently disable servers); an
  explicit `[]` in the file is valid and means "no servers". When unset, the inline
  `MCP_SERVERS` is used exactly as before.
- **`MCP_PERMISSIONS_FILE`** (default empty): a path (relative to the working
  directory) to a **dedicated** JSON **array** file holding per-tool permission
  overrides for **MCP tools only** — each entry
  `{ "tool": "mcp_<server>__<remote>", "permission": "allow"|"ask"|"deny"|"" }`. This
  **replaces** the old `TOOL_PERMISSION_OVERRIDES` env var, which is no longer read.
  The built-ins (`get_current_time`/`echo`/`system_info`) are **not** in it — they
  always ride their *declared* defaults and are un-overridable through it. It is
  **backend-maintained**: on startup the backend re-syncs it to the current MCP tool
  set (a newly discovered tool appears *unfilled* `""` = its default; an entry the
  operator **filled** in is always preserved, even if that tool later vanishes; an
  *unfilled* entry for a vanished tool is pruned), and it is **hot-reloaded** (an
  mtime/size-checked re-read on each policy consultation — no background watcher — so
  an edit takes effect on the next tool call, no restart). `""` (or the field absent)
  means *use the tool's default*. A set-but-**missing** or **blank** file means "no
  overrides" (all MCP tools default `ask`) — **not** an error. A set-but-
  **present-and-malformed** file (non-array, non-object entry, missing/illegal `tool`,
  unknown field, illegal permission, duplicate tool) is a startup `ConfigError`
  naming only the offending tool/field — **but only when `ENABLE_TOOLS=true`** (a
  malformed file with tools off is not validated); a malformed file *at runtime*
  (hot-reload) keeps the **last-good** policy + warns, never crashes the loop.
- **`MCP_CONNECT_TIMEOUT_SECONDS`** (default `10`): the per-server handshake (connect
  / initialize / tools-list) timeout in seconds; on timeout that server is marked
  `unavailable` and the rest start normally. Must be `> 0`.
- **`MAX_MCP_TOOL_RESULT_CHARS`** (default `10000`): the hard cap on how much text from
  a single remote tool result is handed back to the model; over it → stable
  `mcp_result_too_large` (not truncated, no prefix echoed). Must be `>= 1`.
- **`MCP_ALLOW_INSECURE_HTTP`** (default `false`): a hard opt-in to allow plaintext
  `http://` endpoints; by default only `https` is accepted. Intended only for endpoints
  you control (local / trusted intranet). **HTTP transport only** — stdio servers have
  no URL, so this never applies to them.

The two numeric knobs are validated in `Config.__post_init__` (`> 0` / `>= 1`); the
server-list structure (from `MCP_SERVERS_FILE` or the inline `MCP_SERVERS`, shared
validation) is validated in `load_config` (per transport), and a configured-but-
missing/blank `MCP_SERVERS_FILE` is a `ConfigError`. The `MCP_PERMISSIONS_FILE` is
strictly parsed in `load_config` (present + non-blank + malformed → `ConfigError`,
only when `ENABLE_TOOLS=true`; missing/blank → no overrides). The endpoint, token, and
stdio process spec are **never** controllable by the model, chat input, memory, or tool
arguments — they come only from these startup-time settings.

### User-level OAuth knobs (phase 4.x)

- **`OAUTH_CALLBACK_BASE_URL`** (default empty): the **bare public origin** the
  provider redirects to (`<base>/oauth/callback` — that full URI is what must be
  registered with the provider and reachable by it). Empty = **OAuth is entirely off**
  (no providers, no manager, no callback listener). Non-empty must be an absolute
  `http(s)://` origin with a host and **no** userinfo/path/query/fragment/trailing
  slash — a violation is a startup `ConfigError`. It is the only config value OAuth
  needs on `Config`; the provider credentials are **not** config fields.
- **`OAUTH_CALLBACK_PORT`** (default `8090`): TCP port of the minimal callback HTTP
  server (started only when OAuth is configured). Must be `1..65535`.
- **`OAUTH_STATE_TTL_SECONDS`** (default `600`): how long an in-flight authorization
  `state` stays valid before the callback is voided. Must be `> 0`.
- **`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_SCOPES`**
  (all optional, **env-only**): read **only** inside `main.py::_build_provider` (the
  single provider registry) when an MCP server asks for provider `google`. They are
  never stored on `Config`, never logged. Missing id/secret ≠ error — the google
  provider is simply "not configured" (oauth servers degrade to `unavailable` with a
  **log-only** stable code; `/mcp auth` reports "not configured").
  `GOOGLE_OAUTH_SCOPES` is whitespace-separated; empty → the Google Calendar read-only
  scope.

And on each **http** `MCP_SERVERS` entry: an optional **`"authentication"`** object —
`{"type": "none"}` or `{"type": "oauth", "provider": "<id>"}` — **mutually exclusive**
with `bearer_token_env` (both → `ConfigError`); `oauth` requires a non-empty
`provider`, `none` forbids one; unknown fields are rejected. `authentication` (and
`bearer_token_env`) are **http-only**: a stdio entry may not carry either (a spawned
process has no HTTP request to carry an auth header — any credential belongs in its
`env`), and setting one on a stdio entry is a `ConfigError`.

### Infra knobs (phase 5.1)

- **`INFRA_SSH_TARGETS`** (default empty): a JSON **array** of SSH-observation
  targets; empty = the `infrastructure/` provider is off and no SSH connection is ever
  opened. This is the *inline* source — the target list may instead live in the
  **default file `config/infra_ssh_targets.json`** (read when present) or an explicit
  `INFRA_SSH_TARGETS_FILE` (see below). Each object: `name`
  (`[a-z][a-z0-9_-]{0,31}`, unique), `host` (hostname or IPv4/IPv6 literal — **no**
  user/port/path/whitespace; bracketed `[...]` must be a valid IPv6), `port` (int
  `1..65535`), `username`, `private_key_path` + `known_hosts_path` (existing,
  **readable** files — **absolute or relative to the working directory**; not
  `~`/`..`/symlink/directory; known-hosts non-empty — checked to exist at startup so a
  botched secret mount fails fast, contents never read), `mounts` (absolute paths the
  disk tool observes), `services` (systemd unit names the service tool reads). Max 16
  targets, duplicate names refused. A violation is a startup `ConfigError` naming the
  target (or index) and field, **never** the host / key path / known-hosts path / mount
  path (a service/unit name may appear — it is operator-chosen, not a secret).
- **`INFRA_SSH_TARGETS_FILE`** (default empty): a path (relative to the working
  directory, e.g. `config/infra_ssh_targets.json`) to a standalone file holding the
  **same** JSON **array**. Source selection (the same file-over-inline idea as
  `MCP_SERVERS_FILE`, with a well-known default path so the common single-file setup
  needs no env var): when **set**, it is the strict source of truth and **wins** over
  both the default file and the inline `INFRA_SSH_TARGETS` (both ignored) — a
  set-but-**missing**/**unreadable**/**blank** file is a startup `ConfigError` naming
  the path (an explicit `[]` = no targets); when **unset**, the **default file
  `config/infra_ssh_targets.json`** is read when it exists (a present-but-
  **blank** default file is a `ConfigError`), and if that file is **absent** the
  inline `INFRA_SSH_TARGETS` is used (so inline-only config and the default-off case
  both work with no file).
- **`INFRA_SSH_CONNECT_TIMEOUT_SECONDS`** (default `10`): the SSH connect/handshake
  timeout for one approved call. Must be `> 0` **and** `<= TOOL_TIMEOUT_SECONDS`.
- **`MAX_INFRA_TOOL_RESULT_CHARS`** (default `8000`): the hard cap on one infra tool
  result returned to the model; over it → stable `infra_result_too_large` (not
  truncated, no prefix echoed). Must be `>= 1`.

The two numeric knobs + the cross-knob invariant
(`INFRA_SSH_CONNECT_TIMEOUT_SECONDS <= TOOL_TIMEOUT_SECONDS`) are validated in
`Config.__post_init__`; the target-list structure is validated in `load_config`. The
host, key paths, known-hosts path, username, mount paths, and command are **never**
controllable by the model, chat input, memory, or tool arguments — they come only from
these startup-time settings.

### Scheduled (cron) run knobs (phase 9)

- **`SCHEDULES`** (default empty): a JSON **array** of cron schedules; empty
  (unset / `[]` / whitespace) = no automation, no scheduler task, no scheduled runs
  (isomorphic to an empty `MCP_SERVERS`). This is the *inline* source — the schedule
  list may instead live in the **default file `config/schedules.json`** (read when
  present) or an explicit `SCHEDULES_FILE` (see below). Each object has **exactly**
  five fields: `name` (`[a-z][a-z0-9_-]{0,31}`, **unique** across the list — it also
  derives the dedicated-conversation synthetic id), `cron` (a **strict 5-field**
  expression — see the `automation/` per-module reference; bad cron → `ConfigError`),
  `prompt` (the fixed per-run text; non-empty after strip, **≤ 2000** chars),
  `identity` (`"telegram"` / `"qq"` — the channel the single run **executes under**:
  it fixes the memory scope injected and where an in-run `ask` approval card is
  routed), and `receiver` (a **non-empty** object listing **every** channel that
  receives the delivered result, keys ⊆ `{"telegram","qq"}`):
  - **`receiver.telegram`** (optional): `{ "chat_id": <positive int>, "user_id":
    <positive int> }` — `chat_id` is the delivery chat (and the approval
    `delivery_chat_id` when `identity=="telegram"`), `user_id` is the owner
    principal (memory scope + the dedicated row's user column).
  - **`receiver.qq`** (optional): `{ "user_openid": <non-empty str> }` — the
    delivery target, and the owner principal when `identity=="qq"`.
  The `identity`'s receiver **must be present** (`identity=="telegram"` ⇒ `telegram`
  present; `identity=="qq"` ⇒ `qq` present), which enforces "≥ 1 receiver" for free.
  All `chat_id` / `user_id` are **positive** `int` (never `bool`/float/string/`None`);
  `user_openid` is a **non-empty** `str`. Max **16** schedules, duplicate names
  refused. A violation is a startup `ConfigError` naming the **schedule (or index)
  and field/key**, **never** the `prompt` body, a `user_openid`, or any other field
  value (mirrors the infra-targets rule).
- **`SCHEDULES_FILE`** (default empty): a path (relative to the working directory,
  e.g. `config/schedules.json`) to a standalone file holding the **same** JSON
  **array**. Source selection (the same file-over-inline idea as `MCP_SERVERS_FILE`,
  with a well-known default path so the common single-file setup needs no env var):
  when **set**, it is the strict source of truth and **wins** over both the default
  file and the inline `SCHEDULES` (both ignored) — a set-but-**missing**/**unreadable**
  /**blank** file is a startup `ConfigError` naming the path (an explicit `[]` = no
  schedules); when **unset**, the **default file `config/schedules.json`** is read when
  it exists (a present-but-**blank** default file is a `ConfigError`), and if that
  file is **absent** the inline `SCHEDULES` is used (so inline-only config and the
  default-off case both work with no file). The **same** strict per-entry validation
  applies to a file-configured schedule.
- **`SCHEDULE_TIMEZONE`** (default empty): the IANA timezone name cron wall-clock
  times are evaluated in. **Validated at startup** — an invalid name is a
  `ConfigError` (naming the field, never echoing it); blank / whitespace / unset is
  treated as *unset* (use the process-local tz, which Docker sets via `TZ`), **not**
  an error.

The schedule list is validated in `load_config` (strict, fail-fast). Schedules come
**only** from these startup-time settings — the model, chat input, memory, and tool
arguments can **never** create, modify, delete, or trigger a schedule; changing one
requires a restart. `chat_id` / `user_id` / `user_openid` / `prompt` / the run's reply
are **never** written to the logs, the audit table, or `/schedule_status` (the `cron`
**is** shown there, alongside the name and next-fire time — the one allowed schedule
attribute in a command); the notification itself carries the task name + result
(content delivered to the owner, allowed). The one deliberate exception is the
**`/user_status`** command on each channel: it returns the caller's own identity
(Telegram `user_id` + `chat_id`, QQ `user_openid`) **to that caller in-chat** so they
can fill a schedule's `receiver` — user-facing, and still **never logged**.

### QQ channel knobs (phase 10 — multi-channel, C2C plain text + commands)

The QQ channel is **off by default** and turns on the moment `QQ_APP_ID` is set —
the same optional-channel gating as every other opt-in provider (MCP, infra, OAuth,
schedules). When off, `botpy` is never imported and no websocket is opened; the
Telegram bot runs unchanged. **There is no separate toggle for the slash-commands,
the command panel, or the global custom menu** — they come on with the channel (a QQ
channel without commands / a discovery surface would be the odd one out), so all three
are always available once `QQ_APP_ID` is set.

- **`QQ_APP_ID`** (default empty): the QQ open-platform app id (a **non-secret**,
  string of digits). Empty = the QQ channel is **disabled** (no client, no websocket).
  This is the *gate* — it is read in `config.py` and decides whether the channel
  exists at all. **There is no allow-list** (unlike the Telegram adapter's
  `TELEGRAM_ALLOWED_USER_IDS`): the channel is the owner's *personal* bot and a C2C
  chat is a one-to-one private chat, so any `user_openid` that can DM (or @) the app
  is served. Access is bounded by the fact that only the owner has the bot's app id +
  a QQ account that can be added to it — the same trust posture as every other
  personal deployment of this backend.
- **`QQ_CLIENT_SECRET`** (env-only **secret**; *not* read by `config.py`): the app's
  client secret. It is read **only** in `main.py` at client-build time (in `_post_init`),
  **never** stored on `Config`, **never** logged, and **never** committed. If
  `QQ_APP_ID` is set but the secret is empty, the channel is skipped with a `warning`
  (fail-soft — the Telegram bot still runs) rather than a hard startup error. This
  asymmetry (app id in `Config`, secret read at the composition root) is deliberate:
  the secret is used exactly once, to start the websocket, and should not ride on the
  config object that is freely passed around.

**The command set and the panel are config-free.** The 13 slash-commands, their reply
text, and the native command panel are all derived from the hard-coded
`qq/commands.py::_QQ_COMMANDS` table (single source of truth for both `/help` and the
panel) — there is no env var to add, reorder, or disable a QQ command. `config` and
`mcp_manager` are threaded into `build_qq_client` only so the read-only commands
(`/status` model, `/mcp_status`, `/infra_status`, `/schedule_status`) can render; they
are not secrets.

The full mechanism is in the `qq/bot.py` + `qq/commands.py` per-module reference entry
and the "QQ 会话用保留区间的合成 id" invariant in [architecture.md](architecture.md).

### Exec shell-tool knobs (opt-in)

- **`ENABLE_EXEC_TOOL`** (default **`false`**): the opt-in switch for the `exec` shell
  tool. Off → `exec` is never registered, never advertised to the model, and its other
  knobs are not validated (a default deployment stays subprocess-free, matching the
  "no shell by design" posture). On + `ENABLE_TOOLS=true` → `build_default_tools(...)`
  adds `exec` (always `ask`).
- **`MAX_EXEC_TOOL_RESULT_CHARS`** (default `8000`): the cap on one `exec` command's
  stdout / stderr before **tail-truncation** (a fixed `[N chars … truncated]` marker,
  not an error — unlike MCP/infra, because this is the direct result of a
  human-approved command). Must be `>= 1`; validated only when enabled.
- **`EXEC_WORKDIR`** (default empty = the process cwd): the fixed directory a command
  runs in. When set it must be an **existing directory** or startup `ConfigError`
  (fail-fast); relative to the working directory. Validated only when enabled.
- **`EXEC_POLICY_DENY_PATTERNS`** (default empty): a JSON **array** of regex strings
  the operator may **add** to the static catastrophic-command denylist (add-only — the
  core list in `tools/exec_policy.py` is compiled in code and can never be removed).
  Always parse-validated and fail-closed: a non-array, a non-string/blank element, or
  a pattern that does not compile is a startup `ConfigError` naming the index (never
  the pattern body), even when the tool is off. The denylist is a *backstop*, not a
  sandbox.

### File toolset knobs (opt-in)

- **`ENABLE_FILE_TOOL`** (default **`false`**): the opt-in switch for the `file`
  toolset. Off → the eleven `file_*` tools are never registered, never advertised to
  the model, and the other file knobs are not validated (a default deployment stays
  write-free). On + `ENABLE_TOOLS=true` → `build_default_tools(...)` adds the `file`
  toolset (`file_read` / `file_ls` `allow`, the other nine always `ask`).
- **`FILE_WORKDIR`** (default `None`): the confinement root every `path` / `source` /
  `target` must resolve inside (relative to the working directory). **Required** when
  the toolset is enabled — it must be an **existing directory**, else a startup
  `ConfigError` (fail-fast). This is deliberately stricter than the optional
  `EXEC_WORKDIR` (which may be unset): a confinement root is the toolset's security
  premise, so the operator must explicitly choose it. Validated only when enabled.
- **`MAX_FILE_STRING_CHARS`** (default `2000`): the cap on a `file_edit`'s
  `old_string` / `new_string`, **also baked into the parameter schema's `maxLength`**
  — it bounds both the model's proposal and, with it, the approval card's argument
  block (`file_edit`'s `Action:` diff, which reuses the same length bound as the
  generic JSON block). Must be `>= 1`; validated only when enabled.
- **`MAX_FILE_READ_CHARS`** (default `8000`): the cap on a `file_read` result's content
  before **tail-truncation** (a fixed `[N chars of earlier output truncated]` marker,
  not an error — the read was already human-approved). Must be `>= 1`; validated only
  when enabled.
- **`MAX_FILE_LIST_ENTRIES`** (default `1000`): the cap on how many entries one
  `file_ls` call returns; over it, entries are dropped and a `truncated` flag is set
  (not an error). Must be `>= 1`; validated only when enabled.
- **`MAX_FILE_CONTENT_CHARS`** (default `20000`): the cap on the whole-file
  writers' `content` — `file_write` and `file_append` — **also baked into the
  parameter schema's `maxLength`** (bounding the model's proposal and, with it, the
  approval card's argument block). For `file_append` it additionally caps the
  **resulting** file size (`existing + content` → `file_result_too_large`), which the
  `content` cap alone would not cover. It is a *separate* knob from
  `MAX_FILE_STRING_CHARS` (a larger default) because a whole file is bigger than a
  single replace string. Must be `>= 1`; validated only when enabled.

The exec and file knobs come only from these startup-time settings; the model can never
set them, and what it proposes (a command, or a path / source / target + old/new
strings) is gated by the per-call `ask` approval (+ the static denylist for `exec`, +
path confinement for `file`), and never reaches the logs or the audit table.

---

## Test coverage map

`tests/conftest.py` provides a `repo` fixture (file-backed SQLite through the
production helpers — see the deployment note above for why it is not a shared
in-memory connection) and `FakeLLM` /
`RecordingLLM` fakes. **All tests pass, all mocked** — nothing ever talks to the real
LLM endpoint, Telegram, an MCP server, an OAuth provider, or an SSH target. The fakes
used across the suite: `FakeToolLLM` (`test_tool_loop.py`) and
`ScriptedToolLLM`/`AlwaysCallsToolLLM` (`test_agent.py`) and
`ScriptedRecordingLLM` (`test_multimodal.py`) and `RecordingMultimodalLLM`
(`test_attachments.py`) are small fakes that replay scripted `LLMResult`s (raising an
`Exception` entry instead of returning it); `RecordingStore` (a read-recording
`AttachmentStore` subclass) spies on history rehydration. The phase-3 gate tests
(`test_tool_security.py`) drive the loop with `_ScriptedLLM` + a `_RecordingAuditor`
(an in-memory recorder that can force the pre-write to fail closed) + a `_FakeApproval`
(scripted `ApprovalDecision`); `test_telegram_approval.py` drives the broker with a
fake repository + a fake `Application`/`Bot` that record `send_message` (returning a
fake message carrying a `message_id`) and `edit_message_text` (the in-place card
finalisation); `test_tool_audit_db.py`/`test_tool_audit_command.py` use the real
in-memory repository / a real PTB `Update`+`CallbackContext`. The **phase-4 MCP tests**
mock the SDK's transport/session: `test_mcp.py` patches `McpManager`'s
`create_mcp_http_client` / `streamable_http_client` / `ClientSession` with fakes (async
context managers; `streamable_http_client`'s yielded read/write streams are themselves
async CMs, matching the SDK's `ClientSession.__aenter__`) and a `_FakeSession` that
records `initialize`/`list_tools`/`call_tool`; `test_mcp_security.py` drives a real
`McpTool` wrapping a `_FakeRemoteSession` through the same
`_ScriptedLLM`/`_RecordingAuditor`/`_FakeApproval` fakes as the phase-3 gate tests.
OAuth is mocked with a fake provider / `httpx2.MockTransport` / starlette `TestClient`
— no real Google, no real callback listener. Infra SSH is faked by injecting a stub
module into `sys.modules` (to assert the **real** `_connect` kwargs) or by replacing
`infrastructure.provider._connect` with a `_ConnectStub`, and `test_infra.py` asserts
`asyncssh` is **never imported** when no targets are configured.

A map of which behaviours each phase covers:

- **Phase 1**: db init, create conversation, save message, load history, reset, context
  builder, unauthorized user, LLM client (incl. `httpx2` error construction),
  `process_message`, concurrency lock, Telegram handlers — including `/context`
  (handler renders the window/budget/image-downgrade preview; no-conversation and
  unauthorised cases are safe no-ops), `AgentService.context_status` (a read-only,
  metadata-only preview that issues **no** LLM call and **no** attachment-blob read),
  and the **Telegram Reply** on the final answer (`handle_message` sends the model
  reply with `reply_to_message_id=message.message_id` so it quotes the user's message —
  a multi-chunk reply quotes only its **first** chunk, and command acks / error
  notices carry **no** reply). **`/stop`** (the interrupt command): cancelling the
  chat's in-flight reply task unwinds the turn (the conversation lock releases so a
  later message proceeds, the typing keep-alive stops, and the in-flight handle is
  removed) and posts the user-safe "⛔️ **Interrupted.**" notice as a **Telegram Reply
  quoting the interrupted message** (the model reply is *not* sent); `/stop` when idle
  → "Nothing to stop."; unauthorised → a silent no-op; `/stop` targets **only its own
  chat** (a second in-flight chat's task is untouched); and the in-flight registration
  does not leak on the ordinary (non-stopped) completion path.
- **Markdown → Telegram HTML** (`tests/test_telegram_markdown.py`): bold/italic/
  strikethrough/inline-code/fenced-code/links/headings, entity escaping
  (`& < >`), code-spans kept verbatim (no emphasis injected), snake_case left literal,
  chunk tag-balance invariant, plus handler-level "reply sent with `parse_mode=HTML`"
  and "400 → plain-text fallback so a reply is never lost".
- **Phase 2.1 tool runtime** (7 required behaviours + extras), in
  `tests/test_tools.py`, `tests/test_tool_loop.py`, `tests/test_agent.py`,
  `tests/test_llm_client.py`: 1. registry registration, 2. OpenAI schema generation,
  3. tool execution (by name + unknown/error fallbacks), 4. normal chat without tools,
  5. single tool call, 6. multiple tool calls (one turn + sequential rounds), 7.
  infinite-loop iteration cap. Plus: client `tools=` pass-through, `tool_calls`
  normalisation, the no-false-`empty_response` guarantee, and service-level
  "persist only user + final assistant" / `tool_limit` / "disabled stays a single
  call".
- **Multimodal input (phase 2.2)** in `tests/test_multimodal.py` — the 9 required
  behaviours: (1) plain text → `TextContent` with unchanged phase-1 path, (2) Unicode
  emoji preserved verbatim, (3) photo → downloaded bytes → `ImageContent` (download
  mocked), (4) photo + caption → `ImageContent` + `TextContent`, (5) OpenAI conversion
  (right MIME, base64 round-trips to the bytes, text, and part order), (6) image + tool
  calling end-to-end, (7) `ENABLE_TOOLS=false` still delivers the image (no tools
  sent), (8) oversize image refused before the LLM, (9) memory-only lifecycle (no temp
  files) with the LLM failure surfaced user-safe.
- **Persistent image attachments (phase 2.3)** in `tests/test_attachments.py` — the 20
  required behaviours, blobs written to `tmp_path` (never the repo's `data/`):
  (1) JPEG/PNG/WebP save → content-addressed blob + metadata, read-back bytes/MIME/size
  match; (2) dedup — one blob for two messages, two attachment refs; (3) fresh-DB init
  **and** a simulated v1.3.0 upgrade both create `attachments` without losing messages;
  (4) repository returns detached message + attachment records (no lazy load after
  close); (5) a text-only message creates no attachment and `get_messages()` is
  unchanged; (6) photo+caption → current LLM call gets the data URL, DB stores caption
  + metadata, file in store; (7) a plain-text follow-up replays the history image
  (read from the store, no Telegram re-download); (8) a genuine cross-restart rebuild
  (new engine/repo/store over the same db + dir); (9) `MAX_CONTEXT_MESSAGES`
  truncation means an out-of-window image is neither read (spy on the store) nor sent;
  (10) multi-part `[Image, Text]` / `[Text, Image]` order + stable `position`;
  (11) a missing/corrupt blob skips the image but keeps the text (no unhandled
  exception); (12) a write/metadata failure raises a user-safe error, calls no LLM,
  and compensates the orphaned blob; (13) after an LLM error the image is still
  replayable; (14) history replay works with tools on (tools advertised) and
  `ENABLE_TOOLS=false` (asserts `tools is None`); (15–17) `/new` GC — an orphan blob +
  metadata removed with a larger new id, a shared blob kept until its last reference,
  and a reset that succeeds even when a blob is missing / its delete raises (plus a
  direct store-level check that a real `delete()` I/O failure raises
  `AttachmentStorageError`, not a `TypeError`, so the GC's
  `except AttachmentStoreError` still catches it); (18) the plain-text wire shape is
  unchanged with a store attached; (19) logs contain no image bytes, base64, caption,
  full digest, token, or API key (a short digest prefix is the only allowed form);
  (20) full suite green.
- **Attachment-aware context management (phase 2.4)** — the 20 required behaviours.
  In `tests/test_context.py` (pure estimator + planner, no I/O): (1) stable estimator
  values for ASCII/CJK/other-Unicode/empty + per-message envelope + parts, (2) system
  always first & in budget, chronological order and unchanged `build_context`
  behaviour when the budget is ample, (3) message-cap-only → the newest *complete*
  user/assistant turns, never split, (4) token-budget-only → the newest *continuous*
  tail of turns, (5) a full (image) turn that won't fit but whose text-only form will →
  downgraded (text kept, `keep_images=False`), (6) text-only also too big → stop, no
  skipping to older turns, (7) current request (with image) over budget → stable
  `current_over_budget`, no partial current message, (8) anomalous history rows
  (leading assistant / consecutive assistants / unanswered user) group
  deterministically and never raise. In `tests/test_attachments.py` (service + store,
  blobs in `tmp_path`): (9) `RecordingStore` proves a downgraded history image is
  **not** `read()` and reaches the wire as plain text, (10) ample budget → v1.4.0
  replay unchanged (correct data URL + position), (11) downgraded image → caption +
  assistant reply remain but the user `content` is a plain `str`, (12) message cap
  **and** token budget together → satisfies both, current user always present,
  (13) current text **or** current image over budget → no LLM call,
  `AgentError.category == "context_limit"`, turn/image still persisted for a later
  smaller request, (14) a selected-but-missing blob degrades to text (v1.4.0) without
  re-planning or window expansion, (15) budget-selected history with tools **on**
  (tools advertised) and `ENABLE_TOOLS=false` (asserts `tools is None`), (16)
  `attachment_store=None` → phase-2.2 current-image behaviour and unchanged plain-text
  wire shape, (18) `Config` defaults/min-valid/zero/negative/non-int → `ConfigError`,
  (19) the pre-existing suite still passes alongside the new planner tests, (20) full
  suite green.
- **Explicit long-term memory (phase 2.5)** — the 21 required behaviours, across
  `tests/test_memory.py` (pure logic, no I/O/ORM/Telegram),
  `tests/test_memory_db.py` (repository, in-memory/temp-file SQLite),
  `tests/test_memory_service.py` (service + fake LLM), and `tests/test_telegram.py`
  (commands). Pure logic: (1) normalization is deterministic casefold+trim+collapse
  over ASCII/CJK/emoji/punctuation, (2) term extraction (CJK single-codepoint terms,
  ASCII word tokens, ASCII<2 dropped, dedup), (3) ranking (substring hit beats term
  overlap, overlap count ordering, tiebreak newer `updated_at` then larger `id`,
  zero-score never returned, empty/punctuation-only query → `[]`, `limit` respected),
  (4) the fixed reference wrapper + verbatim bullets, (5) `hash_scope` is stable,
  distinct, and not invertible to the raw scope. DB: (6) fresh DB **and** a simulated
  v1.5.0 upgrade both create `memories` without losing existing messages,
  (7) add/list/delete/clear + timestamps + detached `MemoryRecord` after session close,
  (8) **scope isolation** — A's memories never appear in B's list/search/get/delete/
  clear and B cannot see/delete A's, (9) delete-missing → `False`,
  (10) `mark_memories_retrieved` stamps only the given ids, (11) `/new` (`reset`) does
  not touch memories. Service: (12) a matching memory injects a reference `user`
  message **verbatim, right after the main system prompt**, and the request carries
  **exactly one** `system` message (the main prompt — a second `system` message 400s on
  many endpoints), (13) no-scope / empty-query (image-only) / no-match → **no** memory
  message (context byte-for-byte phase-2.4), (14) memory + history + current stay
  within the total budget, (15) an over-sub-budget memory is **skipped, not
  truncated**, and a lower-scored one can still fit, (16) current-over-budget →
  `context_limit`, **no LLM call**, and `last_retrieved_at` stays `None`,
  (17) only **actually-injected** memories are stamped `last_retrieved_at`,
  (18) a memory-repo failure → safe `memory_error`, **no LLM call**, (19) injection
  with tools **on** and `ENABLE_TOOLS=false` (no `tool_calls` in the context) is
  unregressed. Commands (`test_telegram.py`): (20) `/remember` saves + reports the id
  (scope `telegram:<uid>`, no LLM), rejects empty (no write), `/memories` lists own +
  empty-state, `/forget <id>` deletes / missing → not found, `/forget all` requires
  the exact `CONFIRM` token (no delete otherwise) / `CONFIRM` clears all, all
  unauthorized → safe no-ops, and (21) memory-command logs contain **no** raw scope/
  user id/memory content (only `scope_hash`, `memory_id`, `content_length`).
- **Tool Security (phase 3)** — across `tests/test_tool_policy.py` (policy +
  permission parsing + the `MCP_PERMISSIONS_FILE` config gate),
  `tests/test_tool_security.py` (the execution gate in the loop),
  `tests/test_tool_audit_db.py` (the audit table + repository),
  `tests/test_telegram_approval.py` (the approval broker), and
  `tests/test_tool_audit_command.py` (`/tool_audit`). **Policy**
  (`test_tool_policy.py`): the built-ins resolve to their declared permissions
  (`get_current_time` and `echo` → `allow`; `system_info` is deliberately `ask`) while
  a newly-registered tool defaults to `ask`; `build_policy` precedence (config
  override > declared default; unknown tool → `ask`); `advertised_names` withholds a
  `deny` tool (and the OpenAI schema matches); `parse_permission` is case-insensitive
  and raises `ToolPolicyError` on a bad value; and `load_config` captures
  `cfg.mcp_permissions_file` (a `Path`) when set — a set-but-missing/blank file is
  *fine* (it is seeded at startup) — and still fails fast with `ConfigError` on a
  non-positive timeout. **Gate in the loop** (`test_tool_security.py`, scripted LLM +
  fake approval + recording auditor, no network): a `deny` tool is neither advertised
  nor executed (refused + audited `tool_denied`); malformed / non-object
  (array/string/number) / missing-required / wrong-type / extra-property arguments are
  rejected with the exact fixed `invalid_arguments` message and **never executed nor
  approved**; an invalid declared schema is a `ValueError` at `register()`; a slow tool
  is cancelled by `asyncio.wait_for` → `tool_timeout` while the loop continues;
  multiple calls in one turn keep model order under timeout; the `ask` lifecycle
  (approved → executed **exactly once**, denied, expired — each with the right audit
  events and stable model-facing code); a **pre-audit** write failure is
  **fail-closed** (`audit_unavailable`, tool not run); a **terminal** audit write
  failure does **not** re-execute the tool (ran exactly once);
  `ENABLE_TOOLS`-off / `registry=None` is a single LLM call with `tools is None` and
  no gate; and caplog tests prove the model result and the logs carry the stable code
  / exception *class* but **never** the exception text or the tool arguments.
  **Audit DB** (`test_tool_audit_db.py`): fresh DB **and** a simulated v1.6.0 upgrade
  both create `tool_audit_events` without losing existing conversations/messages/
  attachments/memories; append-only event ordering with **safe fields only** (scope
  stored as `scope_hash` at the boundary); the full approval-lifecycle event sequence;
  **scope isolation** (A's events never appear for B's hash; a foreign/missing hash →
  `[]`); and a write failure → `False` (fail-closed) at both the
  `RepositoryToolAuditor` level (mock `add_tool_audit_event` → `False`) and the
  repository level (patch `_session_factory` to raise → returns `False`, never
  raises). **Approval broker** (`test_telegram_approval.py`, fake repo/app/query/
  update): approve → executes **exactly once**; deny; timeout → `EXPIRED`; `shutdown`
  → all pending `EXPIRED`; a repeat click is a safe no-op; an unknown id is a safe
  no-op; a lapsed deadline → `EXPIRED`; the callback-data parser; another allow-listed
  user **cannot** approve (→ `EXPIRED`, no existence leak); another chat **cannot**
  approve; the prompt + callback data are **secret-free** (no scope/user id/chat id);
  the default `approval_summary` does **not** echo arguments; **the purpose summaries
  + Arguments block** — every built-in returns a complete, secret-free purpose line
  (never the generic fallback) under the card's `What it does:` label, `echo`'s
  summary never echoes its (user-input) argument, an MCP `McpTool`'s summary is just
  the tool's `description` (purpose, under a `(🌐Remote)` marker, with the (remote)
  arguments withheld from it), and the card shows the (schema-validated) arguments as
  a readable-JSON `Arguments:` block in `<pre><code>` when the call has arguments
  (pretty-printed, CJK preserved) — **omitted entirely** for an argument-free call —
  with argument values **HTML-escaped** so a value containing markup can't inject tags
  (and the arguments are never written to the logs); **in-place card finalisation** —
  the prompt buttons are labelled **`✅ Approve`** / **`❌ Deny`**, and on approve /
  deny / timeout the *same* message (matched by `message_id`) is edited once via
  `edit_message_text` with an empty `InlineKeyboardMarkup([])` (buttons removed), the
  "one-time / will expire" hint replaced by a single bold, emoji-tagged status word
  (`<b>✅ Approved.</b>` / `<b>❌ Denied.</b>` / `<b>⏰ Expired …</b>` — no "Status:"
  label), **and the `Arguments:` block dropped** (a call with arguments shows them in
  the live prompt but the resolved card keeps only title + tool name + purpose +
  status); a failed edit is swallowed (decision unchanged, no raise), a missing
  `message_id` skips the edit, and a decision posts **no** separate follow-up message
  (exactly one prompt + one edit); a blocked conversation **stays ordered** while
  another proceeds (real `AgentService` + `repo` fixture — A1 blocks on approval, B1
  completes, A2 waits on A's lock, then the approval is released); and
  `build_application` wires the callback handler **only** when a broker is supplied.
  **`/tool_audit`** (`test_tool_audit_command.py`): unauthorized → silent; empty state;
  renders events as HTML newest-first with safe fields; default limit 20; clamped to
  max 50 / min 1; an explicit limit honoured; a non-numeric limit → usage hint
  (service not called); a service failure → user-safe message (no `Traceback`); and
  the output is **secret-free** (no raw scope/user id/args/results).
- **Remote MCP tool provider (phase 4)** — across `tests/test_mcp.py` (config parsing
  + manager + naming, **Streamable HTTP + stdio**), `tests/test_mcp_servers_file.py`
  (`MCP_SERVERS_FILE` source-selection), `tests/test_mcp_permissions_file.py` (the
  dedicated `MCP_PERMISSIONS_FILE` — merge/parse/serialize/seed/hot-reload + the
  config gate), `tests/test_mcp_security.py` (the execution gate applied to a wrapped
  MCP tool), and `tests/test_mcp_status.py` (the `/mcp_status` command). All MCP
  network is mocked with a fake SDK session/transport — no real connection, no
  stdio/subprocess (a stdio server's `stdio_client` is monkeypatched on the manager
  module exactly like `streamable_http_client`). **`MCP_PERMISSIONS_FILE`**
  (`test_mcp_permissions_file.py`, `tmp_path` JSON files, no network): the
  **seed/sync merge** (a new tool appears unfilled `""`; a **filled** entry for a
  vanished tool is kept; an **unfilled** entry for a vanished tool is dropped;
  deterministic order — current tools first then filled orphans sorted; idempotent);
  strict **parse** (valid array, `[]`, missing→`[]`, blank→`[]`, and each violation →
  `PermissionsFileError`: invalid JSON / non-array / non-object entry / missing or
  non-string `tool` / bad tool name / bad permission / unknown field / duplicate
  tool); **serialize** (byte-identical round-trip; `atomic_write` skips a byte-
  identical no-op — mtime/contents unchanged); **config** (path captured as a
  `Path`; unset→`None`; set-but-missing/blank→fine; **present+malformed +
  `ENABLE_TOOLS=true` → `ConfigError`**, malformed + `ENABLE_TOOLS=false` → no error);
  and the **hot-reload** `FileBackedToolPolicy` (missing file → MCP tool `ask` +
  built-ins keep declared defaults; write a `deny` → next `resolve`/`advertised_names`
  withhold it; rewrite to `""` → back to `ask`; a **corrupt write after a good state**
  → keeps last-good + a warning that names only the path, never the file contents, and
  does not raise; an **unchanged file** → no rebuild, proven by a spy on the read).
  **`MCP_SERVERS_FILE`** (`test_mcp_servers_file.py`, `tmp_path` JSON files, no
  network): a file is parsed (http / stdio / mixed); the file **wins over** inline
  `MCP_SERVERS` (inline ignored, not an error) while inline-only still works
  (regression); both unset → `()`; an explicit `[]` file → no servers; a
  **missing/unreadable/blank (0-byte/whitespace) file** → `ConfigError` (never a
  silent drop, never a crash); a file with invalid JSON / a non-array / a bad entry
  (e.g. stdio without `command`) / a duplicate name → the *same* `ConfigError`s as
  inline; and a `bearer_token_env` in the file is resolved from the **process** env
  with the value never stored on the spec. **Config / manager** (`test_mcp.py`): empty
  → no manager; a valid `MCP_SERVERS` parse (the bearer *value* is never stored on the
  spec); a valid **stdio** entry parses (`command`/`args`/`env`/`cwd`; an env *value*
  is never echoed in an error); strict rejection of non-array / non-object / bad-JSON /
  bad server name / duplicate name / unknown field / missing name-or-url / non-`https`
  by default / `http`-only-with-opt-in / unsafe URL (userinfo, fragment, query, bad
  scheme, no scheme) / a query-string token (rejected **and** never echoed) / an empty
  or invalid `bearer_token_env` / a non-positive timeout or result-chars; **stdio
  strictness** — an illegal `transport`, a missing/illegal `command`
  (whitespace/metacharacters), a non-array/non-string/empty `args`, a non-object/
  bad-key/empty-value `env` (error names the key, never the value), an empty `cwd`, a
  stdio entry setting `url`/`bearer_token_env`/`authentication`, an http entry setting
  `command`/`args`/`env`/`cwd`, and the no-`transport` default staying byte-for-byte
  http; a source-level check that `main` only constructs the manager when
  `ENABLE_TOOLS` and servers are set; two healthy servers discover in order;
  **stdio + http mix** discover in order with the right `StdioServerParameters` opened
  per server; **per-server fault isolation** (a connect / initialize / list failure, a
  timeout, or a **stdio spawn failure** — fake `stdio_client` raises `OSError` — marks
  only that server `unavailable` with the right stable code and the rest start, and
  the log carries the exception *class* but never the `command`); **atomic discovery**
  (one illegal name / illegal schema / missing schema / duplicate name drops the whole
  server, `mcp_invalid_tool`, while a good server is unaffected); the bearer header is
  read from env at client-build time (exact `Authorization: Bearer …` header asserted)
  and absent when no token env; **no unexpected DB table** (`tables` is exactly
  `conversations`/`messages`/`attachments`/`memories`/`tool_audit_events`/
  `oauth_credentials`/`oauth_authorization_states` — the last two are the phase-4.x
  OAuth tables); and `status()` exposes only `name`/`available`/`tool_count` (never
  the URL host, token, stdio `command`/`args`/`env`/`cwd`, or failure detail).
  **Naming**: `local_tool_name` namespacing and `is_valid_remote_tool_name` (valid /
  invalid / over-90 / exactly-90). **Gate in the loop** (`test_mcp_security.py`, a
  fake remote session + a real `McpTool` + scripted LLM + recording auditor + fake
  approval, **no network**): the wrapped tool appears in the OpenAI schema and
  defaults to `ask`; two servers exposing the *same* remote name don't collide and a
  policy override on one namespaced name affects only that name; both are callable in
  model order; **arguments are schema-rejected before any network request**
  (wrong-type / missing / extra-property / non-object / malformed / number →
  `session.calls == []`, a `validation_failed` audit with `invalid_arguments`, no
  `started`); valid arguments reach the network; the `ask` lifecycle (approved →
  `call_tool` **exactly once** + the approval request carries the tool name, denied /
  expired → zero calls); a **pre-audit** write failure is **fail-closed** (no
  `call_tool`, `audit_unavailable` in the model-facing result); `ask`/`allow`
  timeouts → `started` then `tool_timeout`; an `allow` override runs with **no**
  approval; a `deny` tool is **withheld from the schema** and refused + audited if
  called anyway; multi-part text result blocks merge with newlines; the result-shape
  mapping (`mcp_tool_error` / `mcp_unsupported_result` / `mcp_result_too_large`); a
  transport exception → `mcp_unavailable` with **no endpoint or exception body
  echoed**; multi-call order is preserved; a multi-round conversation (2 calls, 3 LLM
  calls); the iteration limit → `ToolLoopLimitError`; and privacy (the approval
  summary never echoes arguments, and the logs never carry arguments).
  **`/mcp_status`** (`test_mcp_status.py`): unauthorized → silent; no-manager /
  tools-disabled / zero-servers → "disabled"; renders servers as HTML
  (available/unavailable + tool count + total, `parse_mode=HTML`); does **not**
  connect or call the LLM (a `Spy` subclass that raises on `start`/`close`); many
  servers are all delivered across chunks with no loss; and the output is
  **secret-free** (no URL/host/token/`Bearer`/`secret`/raw user id).
  `("mcp_status", "Show remote MCP tool status")` is in `_COMMANDS`.
- **User-level OAuth for MCP (phase 4.x)** — across `tests/test_oauth_config.py`,
  `tests/test_oauth_google.py`, `tests/test_oauth_db.py`,
  `tests/test_oauth_manager.py`, `tests/test_mcp_oauth.py`,
  `tests/test_oauth_callback.py`, and `tests/test_mcp_command.py`. All OAuth network
  is mocked (fake provider / `httpx2.MockTransport` / starlette `TestClient`) — no
  real Google, no real callback listener, in-memory or `tmp_path` SQLite only.
  **Config** (`test_oauth_config.py`): `OAUTH_CALLBACK_BASE_URL` strict bare-origin
  validation (empty = off; absolute http(s) + host required; userinfo / path / query
  / fragment / trailing slash each a `ConfigError`), `OAUTH_CALLBACK_PORT` range,
  `OAUTH_STATE_TTL_SECONDS` positivity, and the `MCP_SERVERS` `authentication` field
  (valid oauth+provider, `type none`, unknown field rejected, `oauth` without
  provider rejected, `none` with provider rejected, **`authentication` +
  `bearer_token_env` mutually exclusive**, invalid provider id rejected) — all
  failures name the server/field, never a secret. **Google provider**
  (`test_oauth_google.py`, `MockTransport`): `authorization_url` hits
  `https://accounts.google.com/o/oauth2/v2/auth` with client_id / redirect_uri /
  `response_type=code` / state / `access_type=offline` / `prompt=consent` / scope —
  the client **secret never appears in the URL**; `exchange_code` / `refresh_token`
  POST the exact form body to `https://oauth2.googleapis.com/token`; a non-rotated
  refresh returns `refresh_token=None`; a 400 / malformed body →
  `OAuthProviderError` **without echoing the provider body**; missing
  `access_token` → error, missing `expires_in` → `expires_at=None`. **Storage**
  (`test_oauth_db.py`): save/get roundtrip (tz-aware datetimes normalised back from
  SQLite), null expiry, **upsert-not-duplicate** (one row after re-authorization),
  distinct triples coexist, **foreign-user isolation** (get → `None`,
  `has_credential` → `False`), pending **single-use** (a consumed state cannot be read
  again), unknown state → `None`, `delete_pending` best-effort, **`/new`
  (`reset_conversation`) leaves both OAuth tables untouched** (regression, spec §28),
  and a **restart** test (a real `tmp_path` file DB: engine disposed, a fresh engine
  over the same file still reads the credential). **Manager**
  (`test_oauth_manager.py`, `_FakeProvider` + `_FakeStorage`): initiate happy path
  (state ≥ 32 chars, persisted bound to user/chat/server, URL carries state + the
  public redirect URI) and the three stable `OAuthError` codes; the full callback
  outcome map — success (credential saved **bound to the user**, code exchanged
  against the public URI, notifier `(user, chat, server, True)`, state consumed) /
  denied (fixed message, **provider `error_description` never leaked**, state
  deleted) / no state / state-without-code / unknown state / **replay-after-success →
  INVALID** / expired / provider-exchange failure → ERROR **without the code or
  provider reason in `detail`** / provider-missing-at-callback → ERROR / notifier
  failure never breaks the outcome; and **`test_callback_binding_comes_from_pending_
  not_forged_query`** — forged `telegram_user_id`/`provider`/`mcp_server` in the
  callback query cannot redirect the credential (spec §28). `valid_access_token`:
  valid / no-credential / expired → refreshed (old RT consumed, new AT persisted) /
  keep-old-refresh when not rotated / rotated refresh persisted / **refresh failure
  keeps the credential** / expired-without-refresh → `None` / no-known-expiry is
  valid / **concurrent refresh refreshes exactly once**. `oauth_status` all six states
  (a past-expiry credential *with* a refresh token is still `connected`; *without* one
  is `expired`; a foreign user is never `connected`). **MCP integration**
  (`test_mcp_oauth.py`): `telegram_user_id_from_scope` (9 cases); the `McpOAuthAuth`
  hook through a real `httpx2.AsyncClient` + `MockTransport` — no principal → no
  header, `telegram:<id>` → that user's `Bearer` token, foreign user / non-telegram
  scope / no credential → no header, expired → refreshed with **rotation persisted**,
  and a **manager failure still sends the request (without a header)** while the logs
  carry neither the exception detail nor the user id; an oauth server **without** the
  auth factory fails with the stable log-only `mcp_oauth_not_configured` and **never
  connects** (transport patched to record — zero connections); with the factory it
  connects and the client carries an `McpOAuthAuth`; and a real `run_tool_loop` proves
  the loop **sets `active_principal` around `execute()`** so the wrapped tool's
  request carries the invoking user's token (credentials for two users present; the
  right one is chosen). **Callback server** (`test_oauth_callback.py`, starlette
  `TestClient` over a real `OAuthManager`): success (200, "connected", credential
  saved, notifier fired, state consumed, and the logger records contain **neither the
  code, the state, nor the token**); denied / unknown / replayed / stateless / expired
  outcomes; a provider error body (**"LEAKY" / "rejected" never in the response**); a
  **fixed 404 "Not found" for every other path** (parametrised);
  `build_oauth_callback_server` wiring. **Commands** (`test_mcp_command.py`, real PTB
  `Update`/`CallbackContext`): bare `/mcp` is read-only and renders **only the
  caller's own** OAuth state (a second allow-listed user never sees the first's
  "connected"; a status-lookup failure degrades to "required"; unavailable servers
  show no OAuth line); the output is **secret-free** (no URL host / `Bearer` / token /
  raw user id); `/mcp auth <server>` — unauthorised silent, missing server → usage,
  no manager → "not configured", non-OAuth server → stable safe message, happy path
  (initiate bound to the **user**, an **`InlineKeyboardButton`** URL button, the raw
  URL **never** in the body text, an expiry note), and an `initiate` crash → "try
  again" **without the exception detail**; a stray non-`auth` argument still shows the
  status view without initiating; and the menu has `mcp` + `mcp_status` but **no**
  `mcp_auth`.
- **Read-only infrastructure observation via SSH (phase 5.1)** — across
  `tests/test_infra_config.py` (`INFRA_SSH_TARGETS` parsing + the source-selection
  (default file / `INFRA_SSH_TARGETS_FILE` / inline) + the three knobs + the
  cross-knob invariant), `tests/test_infra.py` (the provider + the gate applied to an
  infra tool), and `tests/test_infra_status.py` (the `/infra_status` command). All SSH
  is faked — no real connection, no subprocess; `asyncssh` is exercised either by
  injecting a stub module into `sys.modules` (to assert the **real** `_connect` kwargs)
  or by replacing `infrastructure.provider._connect` with a `_ConnectStub`, and
  `tests/test_infra.py` asserts `asyncssh` is **never imported** when no targets are
  configured. **Config** (`test_infra_config.py`, `tmp_path` key/known-hosts files):
  empty `INFRA_SSH_TARGETS` → `()` + defaults (connect `10.0` / result-chars `8000`);
  a valid target parses every field (incl. IPv4 and bracketed-IPv6 hosts); duplicate
  names refused; structural rejections (non-array / non-object entry / bad name /
  duplicate name / unknown field / `> 16` targets); **unsafe host** rejected and never
  echoed (blank, whitespace, embedded space, `user@host`, `host:port`, absolute path
  `/…`, leading-dot `.hidden`, bare `-`/trailing dash, a 5-label `a.b.c.d.e.f`,
  bracketed IPv4 `[10.0.0.5]`); a host error names the field but **never the host
  value**; bad `port` (non-int / bool / float / out-of-range); bad `username`; key &
  known-hosts **file** validation (missing / `~` / `..` / symlink / directory / empty
  known-hosts — the error names the field, **never the path**; a CWD-relative path is
  accepted); bad `mounts`/`services` (non-absolute mount path); the numeric knobs +
  the **cross-knob invariant** (`connect > tool_timeout` refused; `connect ==
  tool_timeout` ok); and **source selection** — the default file
  `config/infra_ssh_targets.json` is read when present (and wins over a malformed
  inline value; a present-but-blank default is a `ConfigError`; an explicit `[]` =
  none), the inline value is used when the default file is absent (regression), an
  explicit `INFRA_SSH_TARGETS_FILE` **wins over both** the default file and the inline
  value, and a set-but-missing/unreadable/blank explicit file (or malformed JSON /
  non-array / bad entry) is a `ConfigError` that names the field, **never** the host
  or key path. (A conftest autouse fixture points the *default* infra-targets path at
  a non-existent file so the developer's real `config/infra_ssh_targets.json` never
  leaks into the other config tests; `test_infra_config.py` overrides it per-test.)
  **Provider + gate** (`test_infra.py`, `_ScriptedLLM` + `_RecordingAuditor` +
  `_FakeApproval` + `_ConnectStub`, no network): no targets → `asyncssh` not imported
  and no tools built; **three `allow`, argument-free tools per target**; two targets →
  no collision with built-ins or each other; a `deny` override withholds the tool from
  the OpenAI schema; `approval_summary` never echoes the endpoint
  (host/user/mount/service); the **fixed remote-command templates** are host-key-pinned
  and shell-quote their interpolated `mounts`/`services`; each of the three tools
  **parses its stdout to bounded, non-echoing JSON** (disk preserves configured mount
  order); a **connect/auth/host-key failure → `infra_unavailable`** (the log carries
  the exception *class*, never the message); **bad/empty/malformed/nonzero-exit/
  stderr/partial output → `infra_invalid_response`**; an **oversized** result →
  `infra_result_too_large`; the **real `_connect` kwargs** are asserted (explicit
  `client_keys`/`known_hosts` **never `None`**, `agent_path=""`,
  `public_key_auth=True`, `password_auth=False`, `kbdint_auth=False`); the
  **connection is closed after the command** (and on cancel); **model-supplied
  arguments are ignored** (`del arguments`); the full gate — `deny` /
  `invalid_arguments` / a **fail-closed pre-audit** / approval-denied /
  approval-expired each leave the stub with **zero** `connect` calls, while an
  **approved** call connects **exactly once**; a **slow connect or command →
  `tool_timeout`** (connection still closed); multi-call order is preserved; audit +
  logs are **secret-free**; and a **composition-root source check** that `main` builds
  the provider only when `config.enable_tools and config.infra_ssh_targets`; plus the
  `local_tool_name` shape. **`/infra_status`** (`test_infra_status.py`):
  unauthorised → silent; `ENABLE_TOOLS=false` / no targets → "disabled"; renders each
  target's name + its three tool names + the total as HTML (`parse_mode == "HTML"`);
  does **not** connect / import `asyncssh` / call the LLM / mutate config; the output
  is **secret-free** (no host / port / username / key-or-known-hosts path / mount path
  / service / command / `systemctl` / `uname` / `proc`); it states it shows **no
  reachability conclusion**; and `("infra_status", "Show configured infra targets")`
  is in `_COMMANDS`.
- **Scheduled (cron) runs (phase 9)** — across `tests/test_cron.py` (the pure
  5-field parser: every field form, month/day names, `0`/`7` = Sunday, the Vixie
  day-of-month/day-of-week **OR** rule asserted via `CronSpec._day_matches`, all the
  strict rejections — wrong field count / `?` / `@daily` / inverted range /
  out-of-bounds / unknown token / empty — and `next_fire` for representative
  expressions under a fixed tz, incl. leap Feb 29 and calendar-impossible Feb 31 →
  `None` with no infinite loop), `tests/test_schedules_config.py` (the
  `SCHEDULES` / `SCHEDULES_FILE` / `SCHEDULE_TIMEZONE` parsing + **all** invalid sets
  + file-over-inline source selection; the error names the schedule/field but
  **never** echoes the `prompt`), `tests/test_scheduler.py` (the background loop
  driven by an injected clock + gated sleep: on-time fire, several schedules due the
  same minute all fire, **per-task single-flight** (an in-flight run makes the next
  due tick skip), **fault isolation** (one runner's exception logged by name + class
  only — never the text — and never stops the loop/others), `next_fire is None`
  safe-skipped, **no catch-up** on (re)start, idempotent `start`/`stop` leaving no
  dangling task, bounded drain of an in-flight run), `tests/test_schedule_runner.py`
  (the composition-root `_run_schedule`: dedicated **reserved-range** synthetic venue
  via `reset_conversation(name-derived id, row_user_id)` where the row principal is
  `spec.telegram.user_id` for a telegram-identity run or `qq_chat_id(spec.qq.user_openid)`
  for a qq-identity run, `process_message` **exactly
  once** with `spec.memory_scope()` + `spec.approval_delivery_chat_id()`, a formatted
  notification carrying name + result **not** the prompt delivered to **every**
  present `receiver` channel (telegram via `deliver_markdown`, qq via
  `deliver_qq_markdown`), empty reply → no notification, long result chunked,
  `AgentError` → fixed safe notice to all configured receivers, a generic
  exception → no notification, **`finally` always deletes the venue**, a failed
  send on one channel is swallowed (logged by name only, never the openid) without
  blocking the other, a `qq` receiver while `self._qq_client is None` → a safe
  "channel not running" warning and skip, and prompt/reply/openid never reach the
  channel text or logs),
  `tests/test_main_scheduling_wiring.py` (empty `SCHEDULES` → **no scheduler
  object** so no task is ever started and the startup sweep is a no-op; non-empty →
  the scheduler is wired to the bound `_run_schedule` runner + `SCHEDULE_TIMEZONE`),
  plus additive coverage in `tests/test_telegram_approval.py` (**`delivery_chat_id`
  routing**: a scheduled run's card goes to the bound chat, the conversation-lookup
  path is bypassed, the binding is still `(hash_scope(telegram:<user_id>), chat_id)`,
  invalid `delivery_chat_id` values fall through to the lookup path, and the
  interactive path with empty metadata is byte-for-byte the existing lookup),
  `tests/test_database.py` (`delete_conversation` removes the row + messages +
  attachments without relying on FK cascade; `clear_ephemeral_conversations` deletes
  **only** reserved-range venues — a real chat, an in-range synthetic, and a
  just-below-range id coexist and only the synthetic is swept; `reset_conversation`
  self-heals a leftover synthetic row), `tests/test_agent.py` (a scheduled run's
  dedicated venue never serialises with the interactive conversation in the *same
  real chat* — different conversation PKs → different per-conversation locks), and
  `tests/test_telegram.py` (`/schedule_status`: authorised, in the command menu,
  disabled/none/multiple branches, calendar-impossible → "never (untriggerable)",
  **only** name + cron + next-fire rendered — **never** prompt/chat_id/user_id,
  unauthorised → silent, and it never triggers a run / calls the LLM).

### Streaming replies — behaviour coverage

`tests/test_streaming_config.py` (the `ENABLE_STREAMING` knob: default **on**,
explicit on/off, case-insensitive, bad value → `ConfigError`),
`tests/test_llm_client.py` (the `on_text_delta` seam: accumulates + forwards
*accumulated-so-far* text and ends at the full reply; forwards content not
tool-calls; `None` stays non-streaming; timeout / http_error / connection /
empty_response all still translate to `LLMError` mid-stream; a tool-call-only
stream is *not* an `empty_response`), `tests/test_tool_loop.py` (the callback is
forwarded to `complete` on every turn, only the final content turn streams, and
no callback keeps the existing behaviour), `tests/test_agent.py`
(`process_message` streams accumulated text and returns the same final string;
no callback is unchanged; streaming flows through the tool loop), and
`tests/test_telegram.py` (`_tail_preview` keeps the tail beyond the cap;
`_DraftStreamer` throttles a delta burst and pushes the complete reply on
`finalize`; a draft `TelegramError` fails soft and **never** logs the draft body;
**private + `ENABLE_STREAMING=true`** → a draft with a positive `draft_id`, **no**
typing, and the full reply still sent as a normal message; **group** (and
**`ENABLE_STREAMING=false`**) → no draft, typing keep-alive fires; a rejected draft
still leaves the full final message delivered; the reply body never reaches the
logs). **All mocked** — no real LLM/Telegram/network.

### QQ channel (phase 10, C2C plain text) — behaviour coverage

`tests/test_qq.py` — **all mocked** (a fake `C2CMessage` + `FakeService` /
`FakeRepo`; no websocket, no `botpy` client run, no network): (1) any openid
(**no allow-list** — the personal-bot C2C posture) → `process_message` called with
the `qq_chat_id(openid)` conversation, `memory_scope="qq:<openid>"`, an
`AgentMessage` with `source="qq"` and the text; the conversation row is created
keyed by the synthetic id (chat == user); the reply is delivered as one **Markdown**
(`msg_type=2`, text in the nested `markdown.content`, top-level `content` unset)
`reply` at `msg_seq=1`. (2) A **missing** openid (malformed) →
ignored, not processed, no reply. (3) **Blank** content (empty / whitespace /
newlines) → no processing, no reply. (4) An `AgentError` → its `user_safe` text
replied. (5) An **unexpected** exception → a generic "unexpected error" notice, and
the handler does not raise. (6) A **failed send** (`reply` raises) → swallowed, not
raised. (7) A **long** reply → chunked with `msg_seq` incrementing 1, 2, 3, … (each
chunk a distinct dedup key), every chunk a Markdown (`msg_type=2`) message, and the
chunks concatenate back to the full reply (nothing truncated). (8) The local `_split_for_qq` preserves all content /
hard-splits a huge line / single chunk when short / keeps `QQ_MAX_MESSAGE_CHARS
<= 4096`. (9) `build_qq_client` returns a real `botpy.Client` with the
`public_messages` intent bit set and wires `on_c2c_message_create` to the
`QQChannel` logic; with an `approval_broker` passed it additionally sets the `interaction`
intent bit, wires `on_interaction_create` to `approval_broker.handle_interaction`, and
calls `bind_client` (with no broker the `interaction` bit is off). (10) **Privacy**
(asserted in the same tests): the raw
`user_openid`, the message **body**, and the **reply** body **never** appear in any
log record's fields — only the synthetic conversation id. Plus the `qq_chat_id`
invariants: deterministic per openid, inside the reserved `[QQ_CHAT_ID_BASE,
QQ_CHAT_ID_MAX)` range, distinct per openid, and the whole reserved range sits
**below** the schedule range (`QQ_CHAT_ID_MAX <= SCHEDULE_CHAT_ID_BASE`). **All
mocked** — no real QQ/LLM/network/subprocess.

**Slash-commands, reply-quoting, and the panel** (same file): (11) **Dispatch** —
`/new` → `service.reset(cid, cid)` + ack and **no** agent turn / no conversation
row; `/help` lists all 13 commands; an **unknown** `/…` falls through to the agent
as a normal `source="qq"` turn (never swallowed); the command *name* is logged but a
`/remember` **argument** (and the raw openid) is **not** (same test asserts the
memory scope `qq:<openid>` was used). (12) **Each command** reuses the
channel-agnostic `AgentService` methods with the `qq:<openid>` scope and renders
the Telegram phrasing: `/status` (known → `conversation_status` + model/version;
none-yet branch), `/context` (none-yet branch; metadata-only), `/remember` /
`/memories` (with `_utc_stamp`) / `/forget <id>` / `/forget all` (CONFIRM two-step
— without the token it deletes nothing) / `/forget all CONFIRM`, `/tool_audit`
(limit clamped to 1–50, scope-isolated), `/mcp_status` (manager `status()` +
`total_tools`; disabled when manager is `None`), `/infra_status` (`local_tool_name`
per observation; **never** a host/port/path/command — only target name + tool
names; disabled when no targets), `/schedule_status` (name + cron + next-fire only,
**never** prompt/chat_id/user_id; disabled when none), `/user_status` (returns the
caller's own `user_openid` recovered from the `qq:` scope prefix, Markdown, user-facing
in-chat but the openid is **never** in `caplog`). Each command's reply is a
`CommandReply`, and the test asserts the **delivery type follows the shape**: a
**simple** receipt (`/new`, `/remember`, the `/forget` outcomes, the disabled /
"none-yet" notices) is plain (`msg_type=0`), a **structured** display (`/help`,
`/status`, `/context`, the `/memories` list, `/mcp_status` / `/infra_status` /
`/schedule_status` / `/user_status` enabled) is Markdown (`msg_type=2`). (13) **`/stop`** — the turn
registers its `asyncio.Task` in the QQ-local `_in_flight`; a `/stop` from a *separate*
message cancels it (asserted via `pytest.raises(CancelledError)` on the turn task),
the `/stop` sends nothing, the cancelled turn posts a plain-text "已停止" notice
**quoted** to the interrupted message, and the handle is removed; a `/stop` with
nothing running replies "Nothing to stop". (14) **Reply-quoting** — a normal answer's
**first** chunk carries `message_reference={"message_id": str(message.id),
"ignore_get_message_error": True}`; a **long** reply's later chunks do not; command
acks and error notices do **not** quote. (15) **Panel** — `build_c2c_panel_items`
(drops `/schedule_status` over the 14-char name cap, caps at 20, every item
`{type:"command", name:"/<cmd>", desc}` with a **Chinese** `desc` identical to the
command table's description — the panel and the `/help` reply draw from the same
source); `_c2c_panel_payload` (scope `c2c`,
`target_type=all`, the `fibrecase-c2c` remark, no openid/body); `known_command_names`
== the command table; `_ensure_c2c_panel` create-or-update against a fake
`client.http` (`GET`→`POST` when absent, `GET`→`PUT` with the record's `version` when
the marker is found, `GET`→`POST` when a *different* remark is present, and a
`request` exception is swallowed); `on_ready` wires **both** surfaces (the panel's
`GET`→`POST` *and* the menu's single `PUT`). (16) **Global custom menu** —
`_global_menu_payload` returns exactly the two fixed `send_message` items (对话指令
→ `/help`, 工具能力 → `你会使用哪些工具？`) with no openid/body; `_ensure_global_menu`
issues a single `PUT /v2/menu` whose body equals the fixed payload (a replace, so
naturally idempotent — no create-or-update, no remark), and **swallows** a
`request` exception (never raises). **All
mocked** — no real QQ/LLM/network/subprocess.

**Tool approval (QQ button card)** (same file, a fake `client.api` + a fake
`Interaction` — no live websocket, no network): (17) **Approve / Deny** —
`request_approval` (scope `qq:<openid>`) sends one *active* C2C card
(`post_c2c_message` with a `keyboard`, **no** `msg_id`), and the matching
`handle_interaction` (event `type=11`, same openid, the button's `data`) resolves the
waiter to `APPROVED` / `DENIED` and **acks `code=0`** via
`on_interaction_result(interaction.id, …)`; the raw openid / button `data` / reply body
never appear in any log record (only the `hash_scope` fingerprint, tool name, and
decision). (18) **Principal binding** — a click from a **different** openid voids the
pending request (waiter → `EXPIRED`) and acks `code=1`; an **unknown** `request_id`
acks `code=1` without touching any pending; a **repeat** click after the first
consumption also voids + acks `code=1`. (19) **Non-button / no-client / send-failure**
— a `type != 11` interaction is ignored (**no** ack); `request_approval` with **no
client** bound fails closed to `DENIED`; a **card-send failure** pops the pending and
fails closed to `DENIED`. (20) **Expiry / shutdown / cancellation** — a lapsed
`expires_at` (waiter → `EXPIRED`, no decision), `shutdown()` resolving any pending
future to `EXPIRED`, and a `CancelledError` (from `/stop`) dropping the pending entry
without a leak. (21) **Card & keyboard shape** — `_card_text` shows the tool name +
purpose summary (and `detail` fence, or a pretty-JSON arguments block when there is no
detail) and **never** the raw openid/chat/secret; `_approval_keyboard` binds only the
`request_id` (two `action.type=1` callback buttons, `data` = `v1:<request_id>:<a|d>`);
`request_id_from` / `decision_from` parse a well-formed `data` and reject a malformed
one. (22) **Routing provider** — `QQScopedApprovalRouter` dispatches a `qq:`-scoped
request to the QQ broker and any other scope to the Telegram broker, and `shutdown()`
drains both. **All mocked** — no real QQ/LLM/network/subprocess.

---

## Feature limitations

### Tool limitations (phase 2.1 + 3)

- **Three read-only built-ins ship locally** (`get_current_time`, `echo`,
  `system_info`), **plus two opt-in state-changing local capabilities** — the `exec`
  shell tool (off by default; `ENABLE_EXEC_TOOL=true` to enable) and the `file`
  toolset (off by default; `ENABLE_FILE_TOOL=true` to enable). MCP tools (remote
  Streamable HTTP or local stdio) and the phase-5.1 read-only SSH observation tools
  are optional and operator-configured. The current release has no *built-in* network
  scanning, arbitrary-command SSH, or Docker — by design; **`exec` (arbitrary shell,
  backstopped by a static denylist) and the `file` toolset (confined file/directory
  operations in `FILE_WORKDIR`) are the two state-changing local capabilities, both
  opt-in** (`exec` always `ask`; the `file` toolset is `allow` for `file_read` /
  `file_ls` and always `ask` for the other nine).
- **Argument validation is enforced** (phase 3): `function.arguments` is parsed as JSON
  and **schema-validated with `jsonschema` before `execute`** — malformed / non-object
  / missing-required / wrong-type / extra-property payloads are rejected (stable
  `invalid_arguments`) and never executed.
- **Every tool is policy-gated and time-bounded** (phase 3): `allow`/`ask`/`deny`
  decides freely-run / needs-approval / refused; a hung tool is cancelled by
  `asyncio.wait_for` after `TOOL_TIMEOUT_SECONDS` instead of holding the conversation
  lock. `get_current_time` and `echo` declare `allow`; `system_info` is deliberately
  `ask` (to exercise the approval flow); a new tool defaults to `ask` (the read-only
  infra SSH tools, by contrast, declare `allow` — like `get_current_time`/`echo`, they
  are fixed, argument-free, host-key-pinned read-only observations that run without a
  per-call approval).
- **The tool-call transcript is not persisted** — only `user` + final `assistant`
  turns are stored, so the individual `tool_calls`/`tool` turns are not replayable. But
  the **metadata** (tool name, event, stable code, latency, `scope_hash`) **is**
  recorded in the append-only `tool_audit_events` table and viewable via
  `/tool_audit`; that metadata (never args/results) is what makes each call auditable
  after the fact.
- **Remote MCP tools (phase 4) inherit the gate** — a wrapped
  `mcp_<server>__<remote>` tool is a first-class `Tool` that passes through the exact
  same policy/validation/approval/timeout/audit gate as the built-ins. MCP-specific
  limits: **two transports — remote Streamable HTTP + local stdio** (http connects to a
  remote endpoint; stdio spawns an operator-configured process over its stdin/stdout,
  no shell — a stdio spawn failure is just another `unavailable` server, and its
  `command`/`args`/`env`/`cwd` are operator config, never in `status()`/logs),
  **remote text results only** (non-text / empty / oversized → stable code, never
  echoed, capped by `MAX_MCP_TOOL_RESULT_CHARS`), **default `ask`** (a remote tool is
  never auto-`allow`d even if the server claims read-only), **startup-only discovery**
  (no reconnect; a drop — or a stdio process exiting — surfaces as `mcp_unavailable`
  until restart), and **no resources/prompts/sampling**. **User-level OAuth (phase
  4.x)** is now built for **http** servers (per-Telegram-user credential, auto token
  refresh, `/mcp auth`); it is **http-only + user-level** — stdio servers carry no auth
  header (credentials go in their process `env`), and there is no group/shared/global
  OAuth, no multi-account or account switching, no Web UI.
- **Read-only infra observation (phase 5.1) inherits the gate** — each
  `infra_<target>__{host,disk,service}_status` tool is a first-class `Tool`
  (argument-free, `default_permission = allow`) that rides the exact same gate;
  because they are strictly read-only they run without a per-call approval (still
  JSON-Schema-validated, fail-closed pre-audited, time-bounded, and terminal-audited
  like every tool), and an operator may still pin one `deny` by namespaced name.
  Infra-specific limits: **three fixed, argument-free tools per target** (the model
  can name no host / path / service / command — the remote commands are code templates
  whose only interpolation is the startup-validated, shell-quoted `mounts`/
  `services`), **short-lived host-key-pinned key-only connections** (no persistent
  connection, no reconnect, SSH agent off, no password), **parsed results only**
  (malformed/empty/oversized → stable `infra_invalid_response`/
  `infra_result_too_large`, capped by `MAX_INFRA_TOOL_RESULT_CHARS`), **`asyncssh`
  lazy-imported** (empty targets / `ENABLE_TOOLS=false` → never imported, no SSH
  connection ever), and **startup performs no SSH/network probe**.
- **`exec` (opt-in) is one of the two state-changing local tools, and it is
  deliberately hard to abuse.** It runs a single `/bin/sh -c <command>` and returns
  `{exit_code, stdout, stderr}`. Limits: **off by default**
  (`ENABLE_EXEC_TOOL=false` — a default deploy spawns no subprocess at all),
  **always `ask`** (every call needs a one-time human Approve; the command is shown
  verbatim on the approval card — `exec` overrides the optional `approval_detail` hook
  to render it as a `$ …` bash command block in place of the generic JSON, labelled
  `bash` for syntax highlighting — the model can never grant itself approval), a
  **static catastrophic-command backstop** (`tools/exec_policy.py` vetoes destructive
  patterns *before* any spawn, even after approval — a backstop, **not** a sandbox: it
  cannot reason about intent, so it is deliberately small and conservative),
  **arg-vector spawn** (never `shell=True`), a **process-group kill** on timeout/cancel
  (`start_new_session` + `killpg`, so a `sh -c` child tree is never orphaned),
  **output tail-truncated** to `MAX_EXEC_TOOL_RESULT_CHARS` (a fixed
  `[N chars … truncated]` marker; over-cap output is *not* an error, unlike MCP/infra),
  and **`EXEC_WORKDIR`** (optional fixed CWD). The command + stdout/stderr are
  returned to the model **only** — never logged, never in the audit table. There is no
  sandbox / user-drop / cgroup / seccomp isolation: the blast radius of an approved
  command is the account the bot runs under, so it should not run as root, and the
  denylist is a last line, not a substitute for reading the approval card.
- **`file` (opt-in) is the second state-changing local capability, and deliberately a
  much narrower one than `exec`.** It is a **toolset of eleven** tools that lets the
  model do file/directory operations *without writing shell*: `file_read` (a UTF-8
  file's content) and `file_ls` (a directory's entries) are **`allow`** (read-only, no
  per-call approval); `file_edit` (a **precise** edit — swap a **unique**
  `old_string` for `new_string`, or every occurrence with `replace_all`),
  `file_write` (create a file or replace its **entire** content — shell `>`),
  `file_append` (append content, creating the file if absent — shell `>>`),
  `file_mv` / `file_cp` (move / copy a file or dir, **no** overwrite; copying a dir
  needs `recursive`), `file_rm` (delete a **regular file** only), `file_mkdir`
  (create a dir, `parents` for intermediates), `file_rmdir` (delete an **empty** dir
  only), and `file_touch` (create/refresh a file) are all **`ask`** — so the model can
  read and minimally manage files *without writing shell*. `file_write` /
  `file_append` are the **deliberate exception** to "no whole-file write": they let
  the model put arbitrary content into a *single* file, but only inside the root and
  behind per-call approval (the remaining gap is still no deleting/renaming a whole
  tree, no arbitrary shell — those belong to `exec`). Limits: **off by default**
  (`ENABLE_FILE_TOOL=false` — a default deploy performs no file write at all), the
  nine mutating tools are **always `ask`** (every call needs a one-time human Approve;
  `path` / `source` / `target` / `old_string` / `new_string` / `content` are shown
  verbatim on the approval card — `file_edit` / `file_write` / `file_append` override
  the optional `approval_detail` hook to lay them out as a faithful **git-diff-style**
  `Action:` block in place of the generic JSON, labelled `diff` for syntax
  highlighting — the model can never grant itself approval), **path confinement to
  `FILE_WORKDIR`** (the core safety property: `_resolve` collapses `..` and follows
  **symlinks** and refuses anything outside the root *before any I/O* →
  `file_path_escape`, **even after the owner approves**; `FILE_WORKDIR` is a
  **required** existing directory when the toolset is enabled — stricter than the
  optional `EXEC_WORKDIR`), **narrow verbs, no tree clobber** (`file_rm` never
  deletes a directory, `file_rmdir` only an empty one, `file_mv` / `file_cp` refuse an
  existing target), **atomic writes** in `file_edit` / `file_write` (same-dir temp +
  `fsync` + `os.replace`, so a mid-write crash never leaves a half-written file) and
  `file_append` (reads then atomically writes the concatenation — old or full content,
  never a half-appended tail), **exact-match semantics** (`old_string` must be unique
  unless `replace_all` → `file_not_found` / `file_not_unique`), and **four caps**
  (`MAX_FILE_STRING_CHARS` on the replace strings, also the schema `maxLength`;
  `MAX_FILE_READ_CHARS` on a `file_read` result, tail-truncated with the `[N chars …
  truncated]` marker; `MAX_FILE_LIST_ENTRIES` on a `file_ls` result, extra entries
  dropped with a `truncated` flag; `MAX_FILE_CONTENT_CHARS` on `file_write` /
  `file_append` `content` — also the schema `maxLength` — and, for `file_append`, on
  the resulting file size → `file_result_too_large`). The path, file content, and
  old/new strings are returned to the model **only** — never logged, never in the
  audit table.
- Not implemented (out of scope so far): RAG, Web Search, streaming, and the
  autonomous self-driven loop.

### Multimodal input limitations (phase 2.2)

- **Photos only.** `message.photo` is handled. Documents, stickers, video, audio, and
  GIFs are still dropped at the adapter (out of scope — they are the
  `FileContent`/`AudioContent`/… the `ContentPart` union is already shaped to hold).
- **Image persistence** moved to **phase 2.3**: images are now written to the
  content-addressed attachment store and re-attached to history (see
  *Attachment storage limitations* below). The phase-2.2 "images are in-memory only /
  not persisted" behaviour still applies *only* when `attachment_store` is `None`.
- **MIME is sniffed, not validated deeply.** We accept `image/jpeg` / `image/png` /
  `image/webp` (by magic bytes, with a declared-type fallback); anything else is
  refused user-safely. No image *processing* (resize, downscale, EXIF strip).
- **One size gate.** `MAX_IMAGE_SIZE_MB` is enforced in the adapter before the bytes
  are handed on; the service does not re-check. The LLM endpoint must accept the
  base64 `data:` URL payload for the chosen MIME.
- **The backend never guesses model capability.** It sends a standard OpenAI
  multimodal request and lets the endpoint reject (an `http_error`, surfaced
  user-safely) if the model can't see. No `if model == ...` capability table.

### Attachment storage limitations (phase 2.3)

- **Images only.** `ImageContent` (JPEG/PNG/WebP) is persisted.
  `FileContent`/`AudioContent`/… are *not* yet received from Telegram or rendered for
  the LLM — the schema (`content_type`, `position`, one-message→many-attachments)
  already allows them.
- **Local disk only.** Blobs live in `ATTACHMENT_STORAGE_PATH` (default
  `./data/attachments`). No object storage, S3, HTTP file server, DB BLOB, Redis, RAG,
  or vector store.
- **No quota / no background GC.** Blobs are reclaimed only opportunistically by
  `/new` on the owning conversation. There is no retention policy, size cap, or
  scheduled sweep; a blob whose owning conversation is never `/new`'d is never
  deleted. There is also no "delete one attachment" or "clear all attachments"
  command.
- **In-window and in-budget only.** A history image reaches the LLM only if its turn
  is selected by the phase-2.4 planner — inside `MAX_CONTEXT_MESSAGES` **and** within
  `MAX_CONTEXT_ESTIMATED_TOKENS`. Outside either, the image is simply not rehydrated
  (or the turn is downgraded to text) — not an error.
- **Rehydration is best-effort per image.** A missing or corrupt blob degrades that
  one image to "text only" (safe warning logged); it never crashes the turn or feeds a
  fake image.

### Context management limitations (phase 2.4)

- **Estimate, not a billing count.** `MAX_CONTEXT_ESTIMATED_TOKENS` is a conservative,
  deterministic, **model-agnostic estimate** (envelope 4 + CJK 1/codepoint + ASCII
  ceil(n/4) + other-Unicode 1/codepoint + image cost). It is used for *relative
  selection and protection* only — it is **not** an accurate tokenization and there is
  no `tiktoken`/model-specific tokenizer, no model-name→context-window table, and no
  capability guessing. If the real endpoint still returns a context-length HTTP error,
  the existing safe `http_error` path handles it (no retry, no auto-learning of the
  window).
- **Turn-granular, not message-granular.** Selection keeps complete conversation turns
  together; the *newest* turn that cannot fit (even as text-only) is a hard stop — the
  planner never reaches past it to pull in an older turn. Downgrading happens per turn
  (all its images dropped at once), not per image.
- **Current request is never downgraded.** The current user message and its images are
  always kept in full; only *history* images can be downgraded to text. If
  system + current alone exceed the budget the request is refused (no LLM call) rather
  than trimmed.
- **No auto-summarization of dropped history.** Out-of-budget and out-of-cap history
  is simply omitted from the request (still in the DB, visible to the user). There is
  no automatic summarization of *conversation history* — the phase-2.5 memory is a
  separate, **explicit, user-saved** fact store (see *Memory limitations*), not an
  auto-summary of what fell out of the window.
- **No auto-retry.** An over-budget request is surfaced as a user-safe
  `context_limit` (or, if it got through, an `http_error`); the backend never silently
  retries with a smaller context.

### Memory limitations (phase 2.5)

- **Explicit, user-saved only.** Memories are created *only* by the owner via
  `/remember` — there is **no** automatic extraction or summarization of the
  conversation, and no model-driven "remember this." This is a deliberate,
  controllable, auditable foundation, not RAG or a vector DB.
- **Lexical retrieval, not semantic.** `rank_memories` matches by full
  normalised-query **substring** and **term overlap** (CJK single characters + ASCII
  word tokens). It is deterministic and dependency-free but has **no** embeddings, no
  FTS5, no vector store — so paraphrase, synonyms, cross-language, and near-miss
  queries may not retrieve a relevant memory. The backend never claims a recall rate.
- **Whole-memory granularity.** A memory is injected whole (verbatim) or skipped
  entirely — it is never truncated, split, or reworded to fit the budget. A single
  over-sub-budget memory is dropped even if a shorter piece of it would fit.
- **Sub-budget, single message.** Injected memories share **one** `user`-role reference
  message under `MAX_MEMORY_ESTIMATED_TOKENS` (a sub-budget of
  `MAX_CONTEXT_ESTIMATED_TOKENS`), so memory can never push the total over the context
  budget, but the room for memory is bounded by that sub-budget.
- **Content is user text, shown verbatim.** Memory content is *not* sanitized; it can
  contain anything (including instruction-like text). The safety boundary is the fixed
  non-instructional wrapper (`MEMORY_REFERENCE_HEADER`) plus the fact that it rides a
  separate `user`-role message (deliberately **not** a second `system` message — a
  second `system` message triggers a 400 on many OpenAI-compatible endpoints) that
  cannot alter the main prompt's role, tools, or permissions. Do **not**
  "helpfully" strip or reword stored content, and do **not** turn it back into a
  `system` message.
- **Scope = the principal, not the conversation.** Memories are keyed by `scope`
  (currently `telegram:<user_id>`), so they survive `/new`, `/start`, and restarts and
  are shared across all of a principal's conversations. `/new` never clears them; only
  `/forget` does. There is **no** per-conversation memory.
- **No quota sweep / no background GC.** A scope's memories persist until explicitly
  forgotten; there is no retention policy, TTL, or scheduled cleanup.
  `last_retrieved_at` is an informational stamp (only on actually-injected memories),
  not a GC trigger.
- **`last_retrieved_at` is best-effort and coarse.** It is written only when a memory
  is injected, batched before the LLM call. Documented semantics: if the LLM call
  *then* fails, the retrieval still counts. A failed timestamp write is logged and does
  not drop an otherwise-ready turn.

### QQ channel limitations (phase 10, C2C plain text)

- **C2C (private chat) text only.** This slice handles `c2c_message_create`
  (a user DMing the bot) with **plain-text inbound** and **Markdown outbound**
  (`msg_type=2`, so the model's Markdown renders; the short error notice is the one
  `msg_type=0` plain-text send). **群 (group)** and
  **频道/guild** messages are out of scope for *this slice* (no handler wired). Note
  the intent situation is not what it looks like: the **same** `public_messages` bit
  (`1 << 25`, already enabled) also delivers `on_group_at_message_create` — the classic
  QQ *group* @-message event — so classic groups need only a wired handler + group
  identity (`member_openid`, which differs per group from the C2C `user_openid`), not a
  new intent. The newer **频道/guild** system (`guild_id`/`channel_id`, `on_at_message_create`)
  is a *separate* system on `public_guild_messages` (`1 << 30`) and would need that bit
  added. Both are natural, contained follow-ups in `qq/bot.py`.
- **No images / media.** An incoming QQ message is read for its text `content` only;
  an image or any non-text payload is not downloaded, normalised, or echoed back.
  (The channel-agnostic core already supports `ImageContent`; the QQ adapter simply
  does not produce one yet.)
- **No streaming draft.** There is no `on_text_delta` / draft-preview on QQ — a reply
  arrives as one (or several, if long) plain-text message(s) after generation
  completes. The Telegram live-draft preview is Telegram-specific (Bot API 10.0
  `sendMessageDraft`).
- **`deny` tools are still rejected on a QQ turn; `ask` tools are approvable.**
  Approval is routed by scope prefix to a **QQ button-card broker**
  (`qq/approval.py`), so a QQ `ask` tool presents an active C2C Markdown card with
  Approve/Deny callback buttons (the click is an `INTERACTION_CREATE`, acked within
  3 s) — no longer fail-closed. A `deny`-policy tool is still refused, as on Telegram.
- **Command set is the core + read-only `/mcp_status` — not the full Telegram set.**
  There is no `/start` (QQ has no "start" concept; a C2C chat is the conversation) and
  no `/mcp` / `/mcp auth` — the MCP **OAuth login is Telegram-bound** (it returns an
  inline login *button* and a proactive completion notification; QQ has no inline
  button and its proactive send is rate-limited — 2/channel/day inside a 5-minute
  passive window — so the login flow is not reliably deliverable). The 13 commands that
  *are* available: `/new` `/stop` `/help` `/status` `/context` `/remember` `/memories`
  `/forget` `/tool_audit` `/mcp_status` `/infra_status` `/schedule_status`
  `/user_status` (returns the caller's own `user_openid` in-chat; user-facing, never
  logged).
- **Reply-quoting covers the answer, not the panel.** A normal answer's first chunk
  carries a `message_reference` quoting the user's message; command acks and error
  notices do not (there is nothing to quote for them). `/stop`'s "已停止" notice quotes
  the interrupted message.
- **`/schedule_status` is dispatched but omitted from the command panel.** The native
  panel caps each item's `name` at 14 characters; `/schedule_status` is 16 (incl. the
  slash), so it is dropped from the panel by the length filter while remaining
  typeable by hand (and listed in `/help`).
- **A schedule's `receiver` is a *delivery* list; `identity` is the *run* identity.**
  A fire runs the agent **once**, under the channel named by `identity` (which fixes
  the memory scope and the approval-card channel), and then delivers the one result
  to **every** channel present in `receiver`. `identity`'s receiver must be present,
  so a run always has a well-formed home identity. The two are independent: an
  `identity="telegram"` schedule with both receivers runs as the Telegram user but
  still pushes the result to the QQ `user_openid` too.
- **A scheduled QQ delivery needs the QQ channel actually running.** If a schedule has
  a `receiver.qq` but the QQ channel is off (no `QQ_APP_ID`, or a login failure), the
  QQ leg is skipped with a safe warning by schedule name (the `user_openid` is never
  logged) and the Telegram leg (if any) is unaffected — it is a runtime degradation,
  **not** a `ConfigError` (consistent with how the QQ channel itself degrades). The
  QQ send is a **proactive** C2C message (no `msg_id`/`msg_seq`), outside the 5-min
  passive window.

---

## Extending (not yet)

The tool runtime (with its **tool-security gate**), multimodal input, persistent
image attachment storage, **attachment-aware context management**, **explicit
long-term memory**, the **MCP tool provider (Streamable HTTP + stdio)**, **user-level
OAuth for MCP**, and **read-only SSH infrastructure observation** are **now built and
stable**; the next additions should slot in as **Tool Providers** behind the same
`Tool`/`ToolRegistry` interface rather than touching the service, LLM client, or
Telegram layer:

- **MCP — now built (phase 4 + 4.x).** The `mcp/` provider connects to
  operator-configured MCP servers over **Streamable HTTP** (remote endpoint) or
  **stdio** (a local process the backend spawns from an operator-configured
  `command`/`args`/`env`/`cwd`, no shell) at startup, discovers tools via
  `tools/list`, wraps each as a namespaced `Tool` (`mcp_<server>__<remote>`, default
  `ask`), and `registry.add(...)`s them — so each wrapped tool rides the entire
  phase-3 gate. The stdio process is fault-isolated (a spawn failure = just one
  `unavailable` server), its spec is operator config never surfaced in
  `status()`/logs, and the SDK's `stdio_client` context handles its teardown.
  **User-level OAuth is now built** (phase 4.x): an `authentication` object on a
  **http** server config enables the provider-agnostic authorization-code flow,
  per-Telegram-user credential storage, auto token refresh, and the `/mcp` /
  `/mcp auth` commands. What remains out of scope for the MCP provider:
  **reconnect / re-discovery** (discovery is startup-only; a later drop — or a stdio
  process exiting — surfaces as `mcp_unavailable` until a restart), **resources /
  prompts / sampling** (tool calls only, text results only), and **group/shared/global
  OAuth, multi-account, account switching, and a Web dashboard** (OAuth is
  http-only + user-level only; a stdio server carries credentials in its process
  `env`, not an auth header).
- **Read-only infrastructure observation via SSH — now built (phase 5.1).** The
  `infrastructure/` provider builds, per operator-configured target, three fixed,
  argument-free, read-only tools
  (`infra_<target>__{host,disk,service}_status`) that ride the entire phase-3 gate
  over host-key-pinned, key-only AsyncSSH, default to `allow` (strictly read-only, so
  they run **without** a per-call approval like `get_current_time`/`echo` — an
  operator may still pin one `deny`), and connect **only** when the tool is called;
  `/infra_status` shows configuration metadata only, and startup performs no
  SSH/network probe. **What remains out of scope**: any **state-changing** SSH /
  Docker / Pi operation, **arbitrary command/path/host/service** (the three tools are
  fixed and argument-free), **persistent connections / auto-reconnect**, and a
  **reconnect / re-probe** of a target that later becomes unreachable (a failed call
  is just `infra_unavailable` for that invocation).
- **State-changing SSH / Docker / Pi**: same pattern — each a `Tool` (or a small
  provider that yields several), stdlib/subprocess kept *inside* the tool, never in
  the loop. Read-only SSH observation is already built (phase 5.1); those three tools
  are strictly read-only and **default to `allow`** (like `get_current_time`/`echo`).
  Future **state-changing** SSH/Docker/Pi tools require the owner's `Approve`/`Deny`
  gate before every call and must **default to `ask`** (never `allow`).
- **RAG / Web Search**: add as tools (a `search` tool) so the model decides when to
  use them; keep retrieval out of the loop.

**Multimodal extensions** build on the phase-2.2 `AgentMessage` / `ContentPart`
foundation *and* the phase-2.3 attachment store — add a new `ContentPart` subtype + a
`telegram/media.py` branch for the source + a renderer in
`llm/message_converter.py`, and nothing else changes (agent, tool loop, service all
stay put). Persistence, SHA-256 dedup, atomic writes, restart-safe re-hydration, and
`/new` reclamation already work generically for any `attachments.content_type`, so a
new media type reuses that whole path:

- **File / Audio / Video / Sticker**: a `FileContent`/`AudioContent`/… plus a
  `Message` branch (e.g. `message.document` / `message.audio`) that downloads it in
  memory, an OpenAI mapping for that part type, and a matching
  `attachments.content_type` — the blob store and re-hydration are already
  content-type-agnostic.
- **Tool Security (phase 3) — now built.** The tool runtime already exposes a full
  execution gate: `allow`/`ask`/`deny` policy, JSON-Schema argument validation, a
  one-time Telegram approval for `ask` tools, a per-tool `asyncio.wait_for` timeout,
  and an append-only metadata audit (`/tool_audit`). Any new tool (state-changing SSH
  / Docker / Pi) plugs into it by simply *not* declaring `allow`. No further security
  work is required before adding such a tool; the gate is in place (MCP and read-only
  SSH observation already ride it).

---

## Deployment internals

The "what and how" of the Docker + CI setup (the *rules* — no ports by default,
README stays in sync, two-commit release unit — live in `CLAUDE.md`; this is the
mechanics).

- **`Dockerfile`**: `python:3.14-slim` + `uv` (pinned to the lockfile's generator
  version), builds from the committed `uv.lock` for exact locked deps. Runs as an
  unprivileged user; `/app/data` is the only writable path. **No `EXPOSE`** — the app
  is outbound-only by default (Telegram long polling + LLM API); the only conditional
  inbound listener (the phase-4.x OAuth callback) is published by a default-commented
  `ports:` entry in `docker-compose.yaml`, never baked into the image.
  `UV_PROJECT_ENVIRONMENT=/opt/venv` pins the venv (uv's `sync` ignores `--python` for
  venv selection — verified).
- **`docker-compose.yaml`**: single service. Config from the **same `.env`** the local
  `uv run` uses (`env_file: .env`, git-ignored, never baked into the image). Persists
  SQLite via a **bind mount** `./data:/app/data` (shares the `data/` dir with the
  local run). `restart: unless-stopped`. **Publishes NO ports by default** — the
  image stays "no inbound ports" (correct while OAuth is off). Because Compose cannot
  publish a port *conditionally* on an env var (the `ports` mapping has no
  `when`/`condition` field — that only exists on `depends_on`), the phase-4.x OAuth
  callback port is written in this file but **commented out**: a static `ports:` would
  open an inbound port for every deployment, even purely-outbound ones. To enable it
  (i.e. `OAUTH_CALLBACK_BASE_URL` is set), **uncomment** the `ports:` block (it
  publishes `OAUTH_CALLBACK_PORT`, default `8090`, where the provider must reach
  `GET /oauth/callback`); behind a reverse proxy/tunnel the commented example binds it
  to `127.0.0.1` instead. **Keep it commented while OAuth is off** — uncommenting
  genuinely opens an inbound port. (There is no separate
  `docker-compose.oauth.yaml` anymore; the block is inlined and default-commented.)
- **`.dockerignore`**: keeps `.env*`, `data/`, `.venv`, `tests/`, `.git`, etc. out of
  the image. **`README.md` is deliberately NOT ignored** — `uv sync` reads it
  (`pyproject` `readme = README.md`) and the build fails without it.
- **Host-uid mount**: the container runs as the host user
  (`user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`, from `.env`) so the bind-mounted
  `./data` keeps normal host permissions (owned by you, `755`) **and** is writable by
  the container — no `chown` needed. A bind mount takes the host dir's ownership over
  the image's `/app/data`, so a non-owning uid would hit `EACCES` on first DB create.
- **CI** (`.github/workflows/build-image.yml`): on every `v*` tag push, builds the
  image and pushes to **ghcr.io**
  (`ghcr.io/fibrecase/fibreagent-backend:<tag>` + `:<short-sha>`) using the built-in
  `GITHUB_TOKEN` with `packages: write`. Actions are on their Node-24 majors.
  Bump version in `pyproject.toml` + `src/fibrecase_agent_backend/__init__.py`, run
  `uv lock`, commit, `git tag -a vX.Y.Z`, `git push` + `git push origin vX.Y.Z`.
  **Also update `README.md` to match the release** (see *Documentation & release
  conventions* in `CLAUDE.md`) — a bump that leaves the README stale is an incomplete
  release.
