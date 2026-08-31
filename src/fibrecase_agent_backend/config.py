"""Runtime configuration.

All external configuration is read from environment variables (optionally
loaded from a local ``.env`` file). Secrets (the Telegram bot token, the OpenAI
API key, and the QQ client secret) come *only* from the environment and must
never be committed. The QQ client secret is read directly from the environment
at QQ-client build time in the composition root (never stored on
:class:`Config`), mirroring the Google OAuth client-secret rule.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .tools.permissions_file import PermissionsFileError, parse_permissions_json


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class McpServer:
    """One validated, operator-configured MCP server (Streamable HTTP **or** stdio).

    ``transport`` is ``"http"`` (the default) or ``"stdio"``. The two transports
    are mutually exclusive in their config:

    * ``http`` — ``url`` is required and guaranteed, at config-parse time, to be
      an absolute ``https://`` URL (``http://`` only under an explicit insecure
      opt-in) with no userinfo, fragment, query, or missing host.
      ``bearer_token_env`` / ``authentication`` (http-only) may be present.
    * ``stdio`` — ``command`` is required (the executable, PATH-resolved or
      absolute); ``args`` / ``env`` / ``cwd`` are optional. ``url`` and any
      auth (``bearer_token_env`` / ``authentication``) must be **absent** — a
      spawned process has no HTTP request to carry a header on; a credential it
      needs belongs in the process ``env``.

    ``bearer_token_env`` (http-only) is the *name* of an environment variable
    holding the bearer token — **never the token itself** (the token stays
    env-only, out of the frozen config and out of logs). It is ``None`` when the
    server needs no operator bearer auth.

    ``auth_type`` / ``auth_provider`` (phase 4.x, http-only) declare *user-level*
    OAuth: ``auth_type`` is ``"none"`` (the default) or ``"oauth"``; when
    ``"oauth"`` the ``auth_provider`` (e.g. ``"google"``) names the OAuth
    provider whose *per-user* access token is attached to this server's requests
    at request time. The provider's client id/secret are read from the
    environment and are never stored here. Both are ``"none"``/``None`` on a
    stdio server.

    The stdio ``command`` / ``args`` / ``cwd`` are **operator config** and must
    never be reachable from the model, chat input, memory, or tool arguments —
    the same guarantee the http ``url`` already has.
    """

    name: str
    transport: str = "http"
    url: str = ""
    bearer_token_env: str | None = None
    auth_type: str = "none"
    auth_provider: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None


def _load_env() -> None:
    # Load an uncommitted .env from the current working directory. Existing
    # environment variables always take precedence over values in the file.
    load_dotenv(override=False)


@dataclass(frozen=True)
class InfraSshTarget:
    """One validated, operator-configured SSH observation target (phase 5.1).

    This is a *read-only observation* endpoint, not a control one: the backend
    may only run the provider's fixed host/disk/service status commands over a
    host-key-pinned, key-only SSH connection. There is deliberately no password,
    keyboard-interactive, agent, forwarding, or SFTP field — those cannot be
    expressed here, so they cannot be enabled.

    ``host`` is a hostname or an IPv4/IPv6 *literal* (validated at parse time,
    never carrying a userinfo/port/path). ``private_key_path`` and
    ``known_hosts_path`` are paths — **absolute, or relative to the working
    directory** — to *existing, readable, non-symlink* regular files the operator
    mounts read-only (e.g. a Docker secret, or a local ``config/id_…`` /
    ``config/ssh_known_hosts``); both are validated to exist at startup so a
    botched key/known_hosts setup fails fast instead of surfacing as a runtime
    ``infra_unavailable``.

    ``mounts`` / ``services`` are the *only* paths and unit names the disk /
    service tools may observe — fixed at config time, never reachable from the
    model, chat input, or tool arguments. The values live only in this process's
    memory; they are never logged, never written to a file, and never echoed to
    the model, the approval card, the audit table, or ``/infra_status`` (those
    see only ``name`` + a stable code).
    """

    name: str
    host: str
    port: int
    username: str
    private_key_path: str
    known_hosts_path: str
    mounts: tuple[str, ...]
    services: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleTelegramReceiver:
    """The Telegram half of a schedule's ``receiver`` (optional).

    * ``chat_id`` — the **delivery** Telegram chat id (a positive int): where the
      schedule's result / failure notice is sent.
    * ``user_id`` — the **owner** Telegram user id (a positive int). It is the
      memory scope principal (``telegram:<user_id>``) when the schedule's
      ``identity`` is ``"telegram"``, and the ``telegram_user_id`` stored on the
      dedicated conversation row. (A ``"qq"``-identity schedule may still carry
      this to also deliver to Telegram; then the row stores the QQ synthetic id,
      so ``user_id`` is only *read* for the scope/principal when the identity is
      ``"telegram"``.)
    """

    chat_id: int
    user_id: int


@dataclass(frozen=True)
class ScheduleQQReceiver:
    """The QQ half of a schedule's ``receiver`` (optional).

    * ``user_openid`` — the C2C per-app identity (a non-empty string) of the QQ
      user the result / failure notice is sent to. When the schedule's
      ``identity`` is ``"qq"`` this is also the memory scope principal
      (``qq:<user_openid>``) and the owner of the dedicated conversation.
    """

    user_openid: str


@dataclass(frozen=True)
class ScheduleSpec:
    """One validated, operator-configured cron schedule (phase 9 — Automation).

    A schedule fires the Agent on a time basis, in a *dedicated fresh
    conversation*, running ``prompt`` through the normal
    ``AgentService.process_message()`` **once**, and then delivering a formatted
    notification (task name + result) to **every** channel named in ``receiver``.

    Every field is **startup operator config**: the model, chat input, memory,
    and tool arguments can never create / modify / trigger a schedule.

    * ``name`` — unique, matching the MCP/infra name charset
      (``[a-z][a-z0-9_-]{0,31}``); the only schedule attribute that is ever
      logged, shown in ``/schedule_status``, or echoed in a notification.
    * ``cron`` — a strict 5-field cron expression, validated at parse time by
      :func:`.automation.cron.parse_cron` (a bad cron is a startup
      ``ConfigError`, never a silent "never fires"). The expression is stored as
      a string and re-parsed by the scheduler; it is never logged.
    * ``prompt`` — the fixed, non-empty prompt (≤ 2000 chars) the schedule runs.
      It is never logged, shown in ``/schedule_status``, or written anywhere but
      the dedicated conversation (which is deleted after each run).
    * ``identity`` — the channel the run **executes** under: ``"telegram"`` or
      ``"qq"``. It decides (a) the memory scope — ``telegram:<user_id>`` vs
      ``qq:<user_openid>`` — and therefore which saved long-term memories are
      injected; and (b) the approval routing — a ``"telegram"`` run carries a
      ``delivery_chat_id`` (the Telegram card's target), a ``"qq"`` run carries
      none (the QQ approval broker routes by the ``qq:`` scope prefix). ``identity``
      must name a **present** receiver, which is also what guarantees at least
      one receiver is present for every schedule.
    * ``telegram`` / ``qq`` — the optional :class:`ScheduleTelegramReceiver` /
      :class:`ScheduleQQReceiver` the result is **delivered** to. At least one is
      present (the identity's, at minimum; the other may be added so the result
      reaches both channels). A present receiver whose channel is not actually
      running (a ``qq`` receiver on a Telegram-only deployment) is *skipped with a
      warning* at delivery time — never a startup error.

    ``memory_scope()`` and ``approval_delivery_chat_id()`` expose the identity's
    derived values so the composition-root runner stays free of channel logic.
    """

    name: str
    cron: str
    prompt: str
    identity: str
    telegram: ScheduleTelegramReceiver | None = None
    qq: ScheduleQQReceiver | None = None

    def memory_scope(self) -> str:
        """The memory-scope principal this run executes under.

        ``"telegram"`` → ``telegram:<receiver.telegram.user_id>``; ``"qq"`` →
        ``qq:<receiver.qq.user_openid>``. Both are valid for the identity (its
        receiver must be present), so this never raises for a parsed schedule.
        """
        if self.identity == "telegram":
            return f"telegram:{self.telegram.user_id}"  # type: ignore[union-attr]
        return f"qq:{self.qq.user_openid}"  # type: ignore[union-attr]

    def approval_delivery_chat_id(self) -> int | None:
        """The real Telegram chat id an in-run approval card targets, or ``None``.

        A ``"telegram"`` run returns its receiver's ``chat_id`` (the synthetic
        schedule venue has no real chat, so the card goes there). A ``"qq"`` run
        returns ``None`` — the QQ approval broker routes by the ``qq:`` scope
        prefix, not a chat id.
        """
        if self.identity == "telegram":
            return self.telegram.chat_id  # type: ignore[union-attr]
        return None


@dataclass(frozen=True)
class Config:
    """Immutable, validated view of everything the backend needs to run."""

    telegram_bot_token: str
    allowed_user_ids: frozenset[int]

    openai_base_url: str
    openai_api_key: str
    openai_model: str
    openai_timeout: float

    database_url: str
    system_prompt_path: Path
    max_context_messages: int
    max_context_estimated_tokens: int
    context_image_estimated_tokens: int

    enable_tools: bool
    max_tool_iterations: int

    max_image_size_mb: float

    attachment_storage_path: Path

    # Phase 2.5: explicit long-term memory. All positive integers; the memory
    # estimated-token sub-budget must not exceed the total context budget.
    # Defaults keep an existing .env (without these keys) working.
    max_memories_per_scope: int = 200
    max_memory_chars: int = 1000
    max_retrieved_memories: int = 5
    max_memory_estimated_tokens: int = 3000

    # Phase 3/4.x: tool security. ``mcp_permissions_file`` (a CWD-relative path,
    # ``None`` when unset) is the dedicated JSON file that holds the MCP-tool
    # permission overrides (``mcp_<server>__<remote>``) — it replaces the old
    # inline ``TOOL_PERMISSION_OVERRIDES`` env var. The backend maintains the
    # file (seeds it with the discovered tools at startup and hot-reloads it),
    # and built-ins are *not* in it — they always use their declared defaults.
    # A present-but-malformed file is a startup ConfigError; a missing/blank one
    # means "no overrides" (all MCP tools default ``ask``). Both timeouts are seconds.
    mcp_permissions_file: Path | None = None
    tool_approval_timeout_seconds: float = 60.0
    tool_timeout_seconds: float = 30.0

    # Phase 4: MCP tool provider (Streamable HTTP + stdio). ``mcp_servers`` is the
    # parsed, validated list, read from ``MCP_SERVERS_FILE`` (a JSON array in a
    # separate file, the preferred source) or, when that is unset, the inline
    # ``MCP_SERVERS`` JSON string. Empty = no MCP servers → the manager never
    # starts and no MCP connection / process is ever opened. The two numeric
    # knobs are seconds / max-chars and both must be positive;
    # ``mcp_allow_insecure_http`` is a hard opt-in to ``http://`` (default off →
    # https-only; http-only).
    mcp_servers: tuple = field(default_factory=tuple)
    mcp_connect_timeout_seconds: float = 10.0
    max_mcp_tool_result_chars: int = 10000
    mcp_allow_insecure_http: bool = False

    # Phase 4.x: user-level OAuth for MCP servers. ``oauth_callback_base_url``
    # is the public base (no trailing slash, validated) that the OAuth provider
    # redirects to at ``<base>/oauth/callback``; ``None`` = OAuth is not
    # configured and the callback server never starts. ``oauth_state_ttl_seconds``
    # bounds a pending authorization's lifetime (single-use state). The Google
    # client id/secret are read from the *environment* at provider-build time in
    # the composition root — they are never stored on this frozen config and
    # never logged. ``oauth_callback_port`` is where the minimal callback HTTP
    # server listens when OAuth is configured.
    oauth_callback_base_url: str | None = None
    oauth_callback_port: int = 8090
    oauth_state_ttl_seconds: float = 600.0

    # Phase 5.1: read-only infrastructure observation over SSH. ``infra_ssh_targets``
    # is the parsed, validated list, read from the JSON array in the **default**
    # ``config/infra_ssh_targets.json`` when that file is present, from the explicit
    # ``INFRA_SSH_TARGETS_FILE`` when set (which wins over both the default file and
    # the inline value; a set-but-missing/blank file is a ConfigError), or from the
    # inline ``INFRA_SSH_TARGETS`` when no file is present; empty means no
    # infrastructure provider (and no SSH connection is ever opened). Each tool it
    # yields is a *local* ``ask`` tool (like the built-ins) that rides the whole
    # phase-3 gate.
    # ``infra_ssh_connect_timeout_seconds`` must be ``<= tool_timeout_seconds``
    # (it bounds connect + handshake *inside* the loop's per-call timeout);
    # ``max_infra_tool_result_chars`` bounds the normalised text a single infra
    # tool may return to the model (over it → ``infra_result_too_large``).
    infra_ssh_targets: tuple = field(default_factory=tuple)
    infra_ssh_connect_timeout_seconds: float = 10.0
    max_infra_tool_result_chars: int = 8000

    # Exec shell tool. ``enable_exec_tool`` is an explicit opt-in (default off)
    # that keeps the default deployment subprocess-free — when off, ``exec`` is
    # never registered, never advertised, and its other knobs are not validated
    # (mirroring how the infra/MCP optional providers are config-gated, not
    # on-by-default). When on, ``max_exec_tool_result_chars`` tail-truncates one
    # command's stdout/stderr (unlike MCP/infra, exec *truncates* rather than
    # erroring, because the exact command was already human-approved);
    # ``exec_workdir`` is the fixed CWD a command runs in (``None`` = the
    # process cwd) and must be an existing directory; ``exec_policy_deny_patterns``
    # is the operator's add-only list of catastrophic-command regexes layered on
    # top of the core denylist (the core list is compiled in code and always
    # active — it cannot be removed, only extended). ``exec`` always defaults to
    # ``ask``: every call is human-approved before it runs (CLAUDE.md).
    enable_exec_tool: bool = False
    max_exec_tool_result_chars: int = 8000
    exec_workdir: str | None = None
    exec_policy_deny_patterns: tuple = field(default_factory=tuple)

    # File toolset. ``enable_file_tool`` is an explicit opt-in (default off) —
    # when off the file tools are never registered or advertised. When on it
    # adds eleven confined file/directory tools (``file_read`` / ``file_ls`` /
    # ``file_edit`` / ``file_write`` / ``file_append`` / ``file_mv`` /
    # ``file_cp`` / ``file_rm`` / ``file_mkdir`` / ``file_rmdir`` /
    # ``file_touch``). The read-only pair (``file_read`` / ``file_ls``) declares
    # ``allow``; every mutating tool declares ``ask``. ``file_workdir`` is the
    # *root all file operations are confined to* (every path must resolve inside
    # it, symlinks included) — unlike ``exec_workdir`` it is **required** when
    # enabled, because the confinement is the toolset's core safety property (a
    # missing/misconfigured root would defeat the confinement, so it refuses to
    # start).
    # ``max_file_string_chars`` bounds a ``file_edit`` ``old_string`` /
    # ``new_string`` (it also caps the approval card's Arguments block);
    # ``max_file_read_chars`` tail-truncates a ``file_read`` result;
    # ``max_file_list_entries`` caps a ``file_ls`` listing (exec-style marker /
    # flag, not an error). ``max_file_content_chars`` bounds the whole-file
    # writers (``file_write`` / ``file_append``): it caps the ``content``
    # argument (baked into the schema ``maxLength``) and, for ``file_append``,
    # the size of the resulting file after appending (the write side the
    # ``content`` cap alone would not cover). It is separate from
    # ``max_file_string_chars`` (a larger default) because a whole file is
    # bigger than a single replace string.
    enable_file_tool: bool = False
    file_workdir: str | None = None
    max_file_string_chars: int = 2000
    max_file_read_chars: int = 8000
    max_file_list_entries: int = 1000
    max_file_content_chars: int = 20000

    # Streaming replies: when on, *private* chats get a live "draft" compose-box
    # preview (Bot API ``sendMessageDraft``) that updates as the model generates.
    # Group / channel chats always degrade to the normal chunked reply (no
    # draft), and a disabled value makes every chat behave exactly as before.
    # Stopping a generation still uses the existing ``/stop`` command; the
    # Bot API 10.3 in-message Stop button is a later phase.
    enable_streaming: bool = True

    # Phase 10 (multi-channel): QQ bot. ``qq_app_id`` is the QQ open-platform
    # app id ("" = the QQ channel is not configured → no client is built, no
    # websocket is opened — isomorphic to the other optional providers). There is
    # deliberately **no** allow-list: the channel is the owner's *personal* bot and
    # QQ's C2C is a one-to-one private chat, so anyone who can DM (or @ in a
    # group) the app reaches the agent — access is bounded by the fact that only
    # the owner has the bot's app id + a QQ account that can be added to it.
    # The client *secret* is also deliberately **not** stored here — it is read
    # from the environment only when the composition root builds the QQ client, so
    # no Config object ever carries it and it can never be logged from one (the
    # same rule the Google OAuth client secret follows).
    qq_app_id: str = ""

    # Phase 9 (Automation, first slice): time-triggered scheduling. ``schedules``
    # is the parsed, validated list, read from the **default**
    # ``config/schedules.json`` when that file is present, the explicit
    # ``SCHEDULES_FILE`` when set (which wins over both the default file and the
    # inline value; a set-but-missing/blank file is a ConfigError), or the inline
    # ``SCHEDULES`` when no file is present — the same file-over-inline rule as
    # MCP/infra. Empty means no automation: no scheduler task, no scheduled
    # runs (isomorphic to empty ``MCP_SERVERS`` / ``INFRA_SSH_TARGETS``).
    # ``schedule_timezone`` is the IANA tz name the cron wall clock is evaluated
    # in ("" = the process-local tz, which Docker sets via ``TZ``); validated to
    # be a parseable IANA name at startup.
    schedules: tuple = field(default_factory=tuple)
    schedule_timezone: str = ""

    log_level: str = "INFO"
    log_color: str = "auto"  # "auto" | "true" | "false" — see logging_setup
    system_prompt_override: str | None = field(default=None)

    def __post_init__(self) -> None:
        # OpenAI SDK appends "/chat/completions" to base_url, so base_url must
        # point at the API *prefix* (e.g. …/v1), not the full completions URL.
        object.__setattr__(self, "openai_base_url", self.openai_base_url.rstrip("/"))
        if not self.allowed_user_ids:
            raise ConfigError("TELEGRAM_ALLOWED_USER_IDS must list at least one Telegram user id")
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
        if not self.openai_base_url:
            raise ConfigError("OPENAI_BASE_URL is not set — the API prefix, e.g. https://host/v1 (the SDK appends /chat/completions)")
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set")
        if not self.openai_model:
            raise ConfigError("OPENAI_MODEL is not set")
        if self.max_context_messages < 1:
            raise ConfigError("MAX_CONTEXT_MESSAGES must be >= 1")
        if self.max_context_estimated_tokens < 1:
            raise ConfigError("MAX_CONTEXT_ESTIMATED_TOKENS must be >= 1")
        if self.context_image_estimated_tokens < 1:
            raise ConfigError("CONTEXT_IMAGE_ESTIMATED_TOKENS must be >= 1")
        if self.max_tool_iterations < 1:
            raise ConfigError("MAX_TOOL_ITERATIONS must be >= 1")
        if self.max_image_size_mb < 1:
            raise ConfigError("MAX_IMAGE_SIZE_MB must be >= 1")
        if self.max_memories_per_scope < 1:
            raise ConfigError("MAX_MEMORIES_PER_SCOPE must be >= 1")
        if self.max_memory_chars < 1:
            raise ConfigError("MAX_MEMORY_CHARS must be >= 1")
        if self.max_retrieved_memories < 1:
            raise ConfigError("MAX_RETRIEVED_MEMORIES must be >= 1")
        if self.max_memory_estimated_tokens < 1:
            raise ConfigError("MAX_MEMORY_ESTIMATED_TOKENS must be >= 1")
        if self.max_memory_estimated_tokens > self.max_context_estimated_tokens:
            raise ConfigError(
                "MAX_MEMORY_ESTIMATED_TOKENS must be <= MAX_CONTEXT_ESTIMATED_TOKENS "
                "(the memory sub-budget cannot exceed the total context budget)"
            )
        # Phase 3: tool-security timeouts must be positive. (The MCP-permissions
        # file is validated in load_config when it is a real, non-blank file.)
        if self.tool_approval_timeout_seconds <= 0:
            raise ConfigError("TOOL_APPROVAL_TIMEOUT_SECONDS must be > 0")
        if self.tool_timeout_seconds <= 0:
            raise ConfigError("TOOL_TIMEOUT_SECONDS must be > 0")
        # Phase 4: MCP knobs. The server list itself is parsed + validated in
        # load_config (``_parse_mcp_servers``); here we guard the numeric knobs
        # so a direct Config(...) construction is safe too.
        if self.mcp_connect_timeout_seconds <= 0:
            raise ConfigError("MCP_CONNECT_TIMEOUT_SECONDS must be > 0")
        if self.max_mcp_tool_result_chars < 1:
            raise ConfigError("MAX_MCP_TOOL_RESULT_CHARS must be >= 1")
        # Phase 4.x: OAuth knobs. The callback base URL (when set) must be an
        # absolute http(s) URL with a host and no trailing slash / userinfo /
        # fragment / query — it is the public redirect base the OAuth provider
        # is told, so a malformed one must fail at startup, never at callback.
        if self.oauth_callback_base_url is not None:
            _validate_oauth_callback_base(self.oauth_callback_base_url)
        if not 1 <= self.oauth_callback_port <= 65535:
            raise ConfigError("OAUTH_CALLBACK_PORT must be a port in 1..65535")
        if self.oauth_state_ttl_seconds <= 0:
            raise ConfigError("OAUTH_STATE_TTL_SECONDS must be > 0")
        # Phase 5.1: infra knobs. The connect/handshake timeout must fit *inside*
        # the loop's per-call tool timeout (the whole SSH call is wrapped in
        # TOOL_TIMEOUT_SECONDS, so a longer connect timeout could never fire).
        if self.infra_ssh_connect_timeout_seconds <= 0:
            raise ConfigError("INFRA_SSH_CONNECT_TIMEOUT_SECONDS must be > 0")
        if self.infra_ssh_connect_timeout_seconds > self.tool_timeout_seconds:
            raise ConfigError(
                "INFRA_SSH_CONNECT_TIMEOUT_SECONDS must be <= TOOL_TIMEOUT_SECONDS "
                "(the SSH connect/handshake runs inside the per-call tool timeout)"
            )
        if self.max_infra_tool_result_chars < 1:
            raise ConfigError("MAX_INFRA_TOOL_RESULT_CHARS must be >= 1")
        # Exec tool: validated only when enabled, so the default (off) deploy
        # never requires its knobs — consistent with the other optional
        # providers. The deny patterns are compiled at load (fail-closed); here
        # we guard the numeric cap and the fixed working directory.
        if self.enable_exec_tool:
            if self.max_exec_tool_result_chars < 1:
                raise ConfigError("MAX_EXEC_TOOL_RESULT_CHARS must be >= 1")
            if self.exec_workdir is not None and not Path(self.exec_workdir).is_dir():
                raise ConfigError("EXEC_WORKDIR must be an existing directory")
        # File toolset: validated only when enabled. Unlike exec, the working
        # directory is *required* when on — the confinement root is the toolset's
        # core safety property, so a missing root refuses to start rather than
        # fall back to an unrestricted (or the process) cwd.
        if self.enable_file_tool:
            if not self.file_workdir or not Path(self.file_workdir).is_dir():
                raise ConfigError("FILE_WORKDIR must be set to an existing directory when the file toolset is enabled")
            if self.max_file_string_chars < 1:
                raise ConfigError("MAX_FILE_STRING_CHARS must be >= 1")
            if self.max_file_read_chars < 1:
                raise ConfigError("MAX_FILE_READ_CHARS must be >= 1")
            if self.max_file_list_entries < 1:
                raise ConfigError("MAX_FILE_LIST_ENTRIES must be >= 1")
            if self.max_file_content_chars < 1:
                raise ConfigError("MAX_FILE_CONTENT_CHARS must be >= 1")
        # Phase 9 (Automation): SCHEDULE_TIMEZONE, when set, must be a name the
        # stdlib can resolve — a bad timezone is a startup failure, never a silent
        # fallback to the wrong wall clock. ("" means "use the process-local tz"
        # and is always valid; no ZoneInfo call is made for it.)
        if self.schedule_timezone:
            try:
                ZoneInfo(self.schedule_timezone)
            except Exception as exc:
                raise ConfigError(
                    f"SCHEDULE_TIMEZONE must be a valid IANA timezone name (got {self.schedule_timezone!r}): "
                    f"{type(exc).__name__}"
                ) from exc

    @property
    def max_image_size_bytes(self) -> int:
        """The ``MAX_IMAGE_SIZE_MB`` cap expressed in bytes."""
        return int(self.max_image_size_mb * 1_000_000)

    @property
    def system_prompt(self) -> str:
        """The effective system prompt (override > file > built-in fallback)."""
        if self.system_prompt_override:
            return self.system_prompt_override
        if self.system_prompt_path.exists():
            return self.system_prompt_path.read_text(encoding="utf-8").strip()
        return (
            "你是一个运行在用户私人服务器上的个人 AI Agent。"
            "你需要准确、简洁地回答用户问题。"
            "你可以调用可用工具来回答问题（例如查询当前时间、获取系统信息）。"
        )


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"invalid Telegram user id in TELEGRAM_ALLOWED_USER_IDS: {part!r}") from exc
    return frozenset(ids)


def _parse_bool(raw: str, default: bool) -> bool:
    raw = raw.strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y", "t"):
        return True
    if raw in ("0", "false", "no", "off", "n", "f"):
        return False
    raise ConfigError(f"invalid boolean value: {raw!r}")


def _normalize_log_color(raw: str, default: str = "auto") -> str:
    """Normalise ``LOG_COLOR`` to ``"auto"``, ``"true"``, or ``"false"``.

    ``auto`` (the default) means "colour only when stdout is a terminal"; the
    other two force it on/off regardless. Unknown values are a config error.
    """
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in ("auto", "tty"):
        return "auto"
    if value in ("true", "1", "yes", "on"):
        return "true"
    if value in ("false", "0", "no", "off"):
        return "false"
    raise ConfigError(f"invalid LOG_COLOR value: {raw!r} (expected auto/true/false)")


def _parse_float(raw: str, default: float) -> float:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid float value: {raw!r}") from exc


def _parse_int(raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid int value: {raw!r}") from exc


# ---------------------------------------------------------------------------
# Phase 4: remote MCP Streamable HTTP server configuration
# ---------------------------------------------------------------------------
# A server name must look like a tool-namespace fragment: lowercase start, then
# at most 31 more lowercase alphanumerics, ``_`` or ``-``. It must also stay a
# valid tool-name fragment (``[A-Za-z0-9_-]+``) because it is embedded in the
# namespaced local tool name ``mcp_<server>__<tool>``.
_MCP_SERVER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# ``bearer_token_env`` names an environment variable (token is read from the
# env at startup; the *name* is validated here).
_MCP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The only fields a server entry may carry. Any other key is rejected — a typo
# in the config is a startup error, never silently dropped. ``url`` + auth
# (``bearer_token_env`` / ``authentication``) are http-transport fields;
# ``command`` / ``args`` / ``env`` / ``cwd`` are stdio-transport fields. The
# transport itself is named by the optional ``transport`` key ("http"/"stdio",
# default "http"); which of these apply is decided by that key, not their
# mere presence.
_MCP_SERVER_FIELDS = frozenset(
    {"name", "transport", "url", "command", "args", "env", "cwd", "bearer_token_env", "authentication"}
)
# The only fields the optional ``authentication`` object may carry.
_MCP_AUTH_FIELDS = frozenset({"type", "provider"})
# ``auth_type`` values; ``oauth`` is the only authenticated kind this phase
# implements (api_key / basic / custom are out of scope).
_MCP_AUTH_TYPES = frozenset({"none", "oauth"})
# ``transport`` values. ``http`` is the default (remote Streamable HTTP);
# ``stdio`` spawns a local process.
_MCP_TRANSPORTS = frozenset({"http", "stdio"})
# A stdio ``command`` — the executable only (args go in ``args``). No shell is
# ever invoked, so this must be a bare executable name or an absolute/relative
# path: letters, digits, and ``_ ./ -`` (a leading ``/`` or ``./`` is how a
# relative/absolute path reads). No whitespace, no metacharacters.
_MCP_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")
# A provider id (e.g. "google") — lowercase letters/digits/dashes. Validated
# here so a typo'd provider fails at startup; *which providers actually exist*
# is resolved at composition time against the built providers, not here.
_MCP_AUTH_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _validate_oauth_callback_base(base: str) -> None:
    """Raise :class:`ConfigError` unless ``base`` is a safe, absolute callback base.

    ``OAUTH_CALLBACK_BASE_URL`` is the public origin the OAuth provider
    redirects to (``<base>/oauth/callback``). Rules (startup, fail-fast):
      * absolute, scheme ``http`` or ``https``, with a non-empty host;
      * no trailing slash, no userinfo, no fragment, no query — the redirect
        path is appended by the manager, and anything else would make the
        redirect URI the provider is told differ from the real endpoint.

    The full URL is **never** echoed in the error message (it is an operator
    secret-adjacent endpoint); only the field name is named.
    """
    try:
        parsed = urlparse(base)
    except ValueError as exc:
        raise ConfigError(f"invalid OAUTH_CALLBACK_BASE_URL: {type(exc).__name__}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ConfigError("OAUTH_CALLBACK_BASE_URL must be an absolute http(s) URL")
    if not parsed.netloc or not parsed.hostname:
        raise ConfigError("OAUTH_CALLBACK_BASE_URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("OAUTH_CALLBACK_BASE_URL must not embed userinfo (credentials)")
    if parsed.path or parsed.fragment or parsed.query:
        raise ConfigError("OAUTH_CALLBACK_BASE_URL must be a bare origin (no path, query, or fragment)")
    if base.rstrip() != base or base.endswith("/"):
        raise ConfigError("OAUTH_CALLBACK_BASE_URL must not have a trailing slash")


def _parse_mcp_authentication(entry: dict, *, where: str) -> tuple[str, str | None]:
    """Parse + validate one server's optional ``authentication`` object.

    Returns ``(auth_type, auth_provider)``. ``None``/absent → ``("none", None)``.
    An ``oauth`` type **requires** a provider id and **forbids**
    ``bearer_token_env`` (a server is either operator-bearer-authenticated or
    user-OAuth-authenticated, never both). Every violation is a startup
    :class:`ConfigError` naming the server and the field.
    """
    auth = entry.get("authentication")
    if auth is None:
        return "none", None
    if not isinstance(auth, dict):
        raise ConfigError(f"{where} 'authentication' must be a JSON object")
    unknown = set(auth) - _MCP_AUTH_FIELDS
    if unknown:
        raise ConfigError(f"{where} 'authentication' has unknown field(s): {', '.join(sorted(unknown))}")
    auth_type = auth.get("type")
    if not isinstance(auth_type, str) or auth_type not in _MCP_AUTH_TYPES:
        raise ConfigError(f"{where} 'authentication.type' must be one of {sorted(_MCP_AUTH_TYPES)}")
    provider = auth.get("provider")
    if auth_type == "none":
        if provider is not None:
            raise ConfigError(f"{where} 'authentication' with type 'none' must not set 'provider'")
        return "none", None
    # oauth
    if not isinstance(provider, str) or not provider:
        raise ConfigError(
            f"{where} 'authentication' with type 'oauth' requires a 'provider' (non-empty string)"
        )
    if not _MCP_AUTH_PROVIDER_RE.match(provider):
        raise ConfigError(
            f"{where} 'authentication.provider' {provider!r} is invalid "
            "(lowercase letters, digits, '-'; must start with a letter)"
        )
    if entry.get("bearer_token_env") is not None:
        raise ConfigError(f"{where} cannot set both 'bearer_token_env' and user-level 'authentication'")
    return "oauth", provider


def _validate_mcp_url(url: str, *, allow_insecure_http: bool) -> None:
    """Raise :class:`ConfigError` unless ``url`` is a safe, absolute endpoint.

    Rules (all enforced at startup, never at connect time):
      * absolute, with a non-empty host;
      * scheme ``https`` always, ``http`` **only** under the explicit
        ``MCP_ALLOW_INSECURE_HTTP`` opt-in (a typo'd ``http`` is a hard error by
        default);
      * no userinfo (``https://user:pass@…``), no fragment, and no non-empty
        query — the bearer token is carried by the ``Authorization`` header
        (from the referenced env var), never by the URL.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ConfigError(f"invalid MCP server URL: {type(exc).__name__}") from exc
    if parsed.scheme not in ("https", "http"):
        raise ConfigError(f"invalid MCP server URL scheme: {parsed.scheme!r} (expected https)")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ConfigError(
            "MCP server URL uses http — refusing (set MCP_ALLOW_INSECURE_HTTP=true "
            "only for a trusted local/private endpoint)"
        )
    if not parsed.netloc or not parsed.hostname:
        raise ConfigError("MCP server URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("MCP server URL must not embed userinfo (credentials)")
    if parsed.fragment:
        raise ConfigError("MCP server URL must not include a fragment")
    if parsed.query:
        raise ConfigError("MCP server URL must not include a query string")


def _parse_mcp_stdio_args(entry: dict, *, where: str) -> tuple[str, ...]:
    """Parse a stdio server's optional ``args`` (a JSON array of strings).

    ``None``/absent → empty tuple. The values are passed verbatim to the child
    process (no shell, so no expansion) — an empty element or a non-string is a
    startup :class:`ConfigError`.
    """
    args = entry.get("args")
    if args is None:
        return ()
    if not isinstance(args, list):
        raise ConfigError(f"{where} 'args' must be a JSON array of strings")
    parsed: list[str] = []
    for index, value in enumerate(args):
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where} 'args[{index}]' must be a non-empty string")
        parsed.append(value)
    return tuple(parsed)


def _parse_mcp_stdio_env(entry: dict, *, where: str) -> tuple[tuple[str, str], ...]:
    """Parse a stdio server's optional ``env`` (an object of name → value).

    ``None``/absent → empty tuple. Keys must be valid env-var names and values
    non-empty strings; the *value* may hold a credential, so a validation error
    names only the offending **key**, never the value.
    """
    env = entry.get("env")
    if env is None:
        return ()
    if not isinstance(env, dict):
        raise ConfigError(f"{where} 'env' must be a JSON object (env-var name → value)")
    if not env:
        raise ConfigError(f"{where} 'env' must not be empty when present")
    pairs: list[tuple[str, str]] = []
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{where} 'env' keys must be non-empty strings")
        if not _MCP_ENV_NAME_RE.match(key):
            raise ConfigError(f"{where} 'env' key {key!r} is not a valid env-var name")
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where} 'env' value for {key!r} must be a non-empty string")
        pairs.append((key, value))
    return tuple(pairs)


def _load_mcp_servers_text() -> str:
    """Return the raw MCP-servers JSON *text* to validate, choosing its source.

    ``MCP_SERVERS_FILE`` (a path, relative to the working directory) is the
    preferred source for multiple / stdio servers: when set and non-empty, the
    JSON array is read from that file and the inline ``MCP_SERVERS`` is ignored
    (the file wins — not an error). When ``MCP_SERVERS_FILE`` is absent/blank the
    inline ``MCP_SERVERS`` value is used, exactly as before (full backward
    compatibility). A configured-but-missing or unreadable file, or a
    file that is blank (0-byte / whitespace-only), is a startup
    :class:`ConfigError` naming the path — a set-but-empty file must not silently
    disable servers (an explicit ``[]`` is still valid and means "none").
    """
    file_path = os.environ.get("MCP_SERVERS_FILE", "").strip()
    if file_path:
        try:
            text = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read MCP_SERVERS_FILE '{file_path}': {exc.strerror or exc}") from exc
        if not text.strip():
            raise ConfigError(
                f"MCP_SERVERS_FILE '{file_path}' is empty; it must contain a JSON array "
                'of server objects (use "[]" for none, or unset MCP_SERVERS_FILE)'
            )
        return text
    return os.environ.get("MCP_SERVERS", "")


# Default location of the infra-SSH-targets file (CWD-relative, like
# ``config/system_prompt.txt``), consulted when ``INFRA_SSH_TARGETS_FILE`` is not
# set. A module global so tests can monkeypatch it to a per-test path (keeping the
# developer's real ``config/infra_ssh_targets.json`` out of unrelated config tests).
_INFRA_TARGETS_DEFAULT_FILE = "config/infra_ssh_targets.json"

# Default location of the schedules file (CWD-relative, like the other config
# files), consulted when ``SCHEDULES_FILE`` is not set. A module global so tests
# can monkeypatch it to a per-test path (keeping a developer's real
# ``config/schedules.json`` out of unrelated config tests).
_SCHEDULES_DEFAULT_FILE = "config/schedules.json"


def _load_infra_targets_text() -> str:
    """Return the raw infra-SSH-targets JSON *text* to validate, choosing its source.

    Source selection (the same file-over-inline idea as ``MCP_SERVERS_FILE``, with a
    well-known **default path** so the common single-file setup needs no env var):

    * ``INFRA_SSH_TARGETS_FILE`` **set** (a path, CWD-relative) — it is the strict
      source of truth and **wins** over both the default file and the inline
      ``INFRA_SSH_TARGETS`` (both ignored). A set-but-**missing** or **blank**
      (0-byte / whitespace-only) file is a startup :class:`ConfigError` naming the
      path — an operator who pointed the provider at a file must not silently get
      "no targets" (an explicit ``[]`` in the file is still valid and means none).
    * ``INFRA_SSH_TARGETS_FILE`` **unset** — the **default file**
      (:data:`_INFRA_TARGETS_DEFAULT_FILE`, ``config/infra_ssh_targets.json``) is
      used **when it exists**; a present-but-**blank** default file is a
      :class:`ConfigError`. When the default file is **absent**, it falls back to
      the inline ``INFRA_SSH_TARGETS`` value, exactly as before (so inline-only
      config keeps working and the default-off case needs no file).
    """
    explicit = os.environ.get("INFRA_SSH_TARGETS_FILE", "").strip()
    if explicit:
        try:
            text = Path(explicit).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read INFRA_SSH_TARGETS_FILE '{explicit}': {exc.strerror or exc}") from exc
        if not text.strip():
            raise ConfigError(
                f"INFRA_SSH_TARGETS_FILE '{explicit}' is empty; it must contain a JSON array "
                'of target objects (use "[]" for none, or unset INFRA_SSH_TARGETS_FILE)'
            )
        return text
    default_path = Path(_INFRA_TARGETS_DEFAULT_FILE)
    if default_path.exists():
        try:
            text = default_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read default infra targets file '{_INFRA_TARGETS_DEFAULT_FILE}': {exc.strerror or exc}") from exc
        if not text.strip():
            raise ConfigError(
                f"default infra targets file '{_INFRA_TARGETS_DEFAULT_FILE}' is empty; it must contain a "
                'JSON array of target objects (use "[]" for none, or remove the file)'
            )
        return text
    return os.environ.get("INFRA_SSH_TARGETS", "")


def _load_mcp_permissions_file(enable_tools: bool) -> Path | None:
    """Resolve ``MCP_PERMISSIONS_FILE`` to a :class:`Path` (or ``None``) and
    apply the **fail-to-start** gate.

    * Unset / blank → ``None`` (no permissions file; all MCP tools default
      ``ask``) — never an error.
    * Set but the file is **missing or blank** → ``Path`` (fine — "no
      overrides"; the backend seeds it at startup).
    * Set, a real non-blank file, and it is **malformed** (invalid JSON /
      non-array / bad entry / duplicate) → :class:`ConfigError`. A botched
      security setting is never silently ignored (and would otherwise weaken a
      pinned ``deny`` to ``ask``).

    The malformed check runs only when tools are enabled (with tools off there
    is no policy to gate, so a stale file must not block startup). The path is
    CWD-relative by construction (like ``MCP_SERVERS_FILE``); only the offending
    field/tool is named, never the file's other contents.
    """
    raw = os.environ.get("MCP_PERMISSIONS_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise ConfigError(f"cannot read MCP_PERMISSIONS_FILE '{raw}': {exc.strerror or exc}") from exc
    if text.strip():
        if not enable_tools:
            return path
        try:
            parse_permissions_json(text)
        except PermissionsFileError as exc:
            raise ConfigError(f"invalid MCP_PERMISSIONS_FILE '{raw}': {exc}") from exc
    return path


def _parse_mcp_servers(raw: str, *, allow_insecure_http: bool) -> tuple[McpServer, ...]:
    """Parse + strictly validate the raw MCP-servers JSON *text* into a tuple of
    :class:`McpServer`. The text is a JSON *array* (read from ``MCP_SERVERS_FILE``
    or the inline ``MCP_SERVERS`` — see :func:`_load_mcp_servers_text`); each
    element is an object with ``name`` (unique, matching the name charset) and an
    optional ``transport`` (``"http"`` default or ``"stdio"``). The two transports
    are mutually exclusive in their fields:

    * **http** — ``url`` is required (a safe absolute URL, see
      :func:`_validate_mcp_url`), plus the optional http-only auth:
      ``bearer_token_env`` (an env-var *name* whose value must be present and
      non-empty at startup) and/or ``authentication`` (user-level OAuth).
    * **stdio** — ``command`` is required (a bare executable name or path, no
      shell), plus optional ``args`` (array of strings), ``env`` (object), and
      ``cwd`` (string). ``url`` and any auth must be absent.

    An empty / blank value yields an empty tuple (no MCP servers). Anything
    malformed — invalid JSON, a non-array, a non-object entry, an unknown field,
    a bad name, a bad transport, a transport field on the wrong transport, a
    bad url/command/args/env, a duplicate name, a malformed env-var name, or a
    referenced env var that is missing/empty — is a startup :class:`ConfigError`.
    Error messages name the *server* and the *field*, never a token or the full
    URL.
    """
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid MCP server configuration JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ConfigError("MCP_SERVERS must be a JSON array of server objects")

    servers: list[McpServer] = []
    seen: set[str] = set()
    for index, entry in enumerate(data):
        where = f"server #{index + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} in MCP_SERVERS must be a JSON object")
        unknown = set(entry) - _MCP_SERVER_FIELDS
        if unknown:
            raise ConfigError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where} is missing a valid 'name' (non-empty string)")
        where = f"server {name!r}"
        if not _MCP_SERVER_NAME_RE.match(name):
            raise ConfigError(
                f"{where} name must match [a-z][a-z0-9_-]{{0,31}} "
                "(lowercase start; lowercase letters, digits, '_', '-')"
            )
        if name in seen:
            raise ConfigError(f"duplicate MCP server name: {name!r}")
        seen.add(name)

        transport = entry.get("transport")
        if transport is None:
            transport = "http"
        if not isinstance(transport, str) or transport not in _MCP_TRANSPORTS:
            raise ConfigError(
                f"{where} 'transport' must be one of {sorted(_MCP_TRANSPORTS)} (got {transport!r})"
            )

        if transport == "http":
            # Remote Streamable HTTP — the pre-stdio behaviour, unchanged.
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                raise ConfigError(f"{where} is missing a valid 'url' (non-empty string)")
            _validate_mcp_url(url, allow_insecure_http=allow_insecure_http)
            for field in ("command", "args", "env", "cwd"):
                if entry.get(field) is not None:
                    raise ConfigError(f"{where} is an http server and must not set '{field}' (a stdio field)")
            token_env = entry.get("bearer_token_env")
            if token_env is not None:
                if not isinstance(token_env, str) or not token_env:
                    raise ConfigError(f"{where} 'bearer_token_env' must be a non-empty string (an env-var name)")
                if not _MCP_ENV_NAME_RE.match(token_env):
                    raise ConfigError(f"{where} 'bearer_token_env' {token_env!r} is not a valid env-var name")
                # The *value* must exist and be non-empty at startup; the token itself
                # is read only when the client is built and is never stored on the spec.
                if not os.environ.get(token_env, "").strip():
                    raise ConfigError(
                        f"{where} references bearer token env {token_env!r}, which is not set (or is empty)"
                    )
            auth_type, auth_provider = _parse_mcp_authentication(entry, where=where)
            servers.append(
                McpServer(
                    name=name,
                    transport="http",
                    url=url,
                    bearer_token_env=token_env,
                    auth_type=auth_type,
                    auth_provider=auth_provider,
                )
            )
            continue

        # transport == "stdio" — spawn a local process.
        if entry.get("url") is not None:
            raise ConfigError(f"{where} is a stdio server and must not set 'url' (an http field)")
        # Auth (bearer / user-level OAuth) is http-only — there is no request
        # for a spawned process to attach a header to. A credential it needs
        # belongs in the process 'env' instead.
        if entry.get("bearer_token_env") is not None or entry.get("authentication") is not None:
            raise ConfigError(
                f"{where} is a stdio server and cannot use 'bearer_token_env'/'authentication' "
                "(put any credential in its 'env')"
            )
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"{where} is a stdio server and requires a valid 'command' (non-empty string)")
        if not _MCP_COMMAND_RE.match(command):
            raise ConfigError(
                f"{where} 'command' {command!r} is invalid "
                "(a bare executable name or path; letters, digits, '_', '.', '/', '-'; no whitespace)"
            )
        args = _parse_mcp_stdio_args(entry, where=where)
        env = _parse_mcp_stdio_env(entry, where=where)
        cwd = entry.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd:
                raise ConfigError(f"{where} 'cwd' must be a non-empty string when present")
        servers.append(
            McpServer(
                name=name,
                transport="stdio",
                url="",
                command=command,
                args=args,
                env=env,
                cwd=cwd,
            )
        )
    return tuple(servers)


# ---------------------------------------------------------------------------
# Phase 5.1: read-only infrastructure observation over SSH
# ---------------------------------------------------------------------------
# A target name is the same charset as an MCP server name (a lowercase,
# tool-namespace fragment) because it is embedded in the namespaced local tool
# name ``infra_<target>__<observation>``.
_INFRA_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# An SSH login name: letter or underscore start, then letters/digits/dot/underscore/dash.
_INFRA_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
# A systemd unit name: the conservative set systemd itself accepts for the
# common ``.service``/`.socket`/... forms we observe.
_INFRA_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
# ``..`` is rejected in a path by checking each *segment* against this — a
# trailing/interior dot is allowed for a mount point (e.g. ``/volume1``) but a
# ``..`` segment is a traversal and must never reach a command.
_INFRA_MAX_TARGETS = 16
_INFRA_MAX_MOUNTS = 32
_INFRA_MAX_SERVICES = 32
_INFRA_MOUNT_MAX_LEN = 256
_INFRA_SERVICE_MAX_LEN = 128
# The only fields a target entry may carry. Any other key is a startup error —
# notably a password, ``password_env``, keyboard-interactive, agent, forwarding,
# or SFTP field is *not* in this set, so it is refused, never silently dropped.
_INFRA_TARGET_FIELDS = frozenset(
    {"name", "host", "port", "username", "private_key_path", "known_hosts_path", "mounts", "services"}
)


def _validate_infra_host(host: str, *, where: str) -> None:
    """Raise :class:`ConfigError` unless ``host`` is a safe SSH destination.

    Accepts a hostname or an IPv4/IPv6 *literal* — never a form with a userinfo,
    an embedded port, or a path (``user@host``, ``host:22``, ``host/path`` are
    all refused). The host is an operator secret-adjacent endpoint and is never
    echoed in the error (only the field is named).
    """
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{where} is missing a valid 'host' (non-empty string)")
    if any(ch.isspace() for ch in host):
        raise ConfigError(f"{where} 'host' must not contain whitespace")
    if host.startswith("["):
        # A bare IPv6 literal may be bracketed (``[::1]``) for direct use.
        inner = host
        if inner.endswith("]") and len(inner) > 2:
            inner = inner[1:-1]
        else:
            raise ConfigError(f"{where} 'host' is an invalid IPv6 literal")
        if ":" not in inner:
            raise ConfigError(f"{where} 'host' is an invalid IPv6 literal")
        try:
            ipaddress.IPv6Address(inner)
        except ipaddress.AddressValueError as exc:
            raise ConfigError(f"{where} 'host' is an invalid IPv6 literal: {type(exc).__name__}") from exc
        return
    # No userinfo, no trailing path, no explicit port.
    if "@" in host:
        raise ConfigError(f"{where} 'host' must not embed a username")
    if host.startswith("/") or host.startswith("."):
        raise ConfigError(f"{where} 'host' must not be a path")
    if ":" in host:
        # A single colon is a legal (if rare) hostname label, but an IPv6 literal
        # without brackets is unambiguous — accept it, otherwise refuse (a colon
        # here is most likely a stray ``:port``).
        if host.count(":") == 1:
            raise ConfigError(f"{where} 'host' must not include a port (set 'port' separately)")
        # Multiple colons → try an IPv6 literal.
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ConfigError(f"{where} 'host' is not a valid hostname or IPv6 literal: {type(exc).__name__}") from exc
        return
    if host.count(".") > 4:
        raise ConfigError(f"{where} 'host' is not a valid hostname")
    # Hostname label charset check (RFC-952/1123-ish): alphanumeric + hyphen,
    # labels must not start/end with a hyphen.
    for label in host.split("."):
        if not label:
            raise ConfigError(f"{where} 'host' has an empty hostname label")
        if label.startswith("-") or label.endswith("-"):
            raise ConfigError(f"{where} 'host' has a malformed hostname label")
        if not re.fullmatch(r"[A-Za-z0-9-]+", label):
            raise ConfigError(f"{where} 'host' has an invalid hostname label")
    # An IPv4 literal is also accepted (it passes the hostname-label check).


def _validate_infra_file(path: str, *, where: str, field: str, require_nonempty: bool) -> None:
    """Raise :class:`ConfigError` unless ``path`` is a safe, existing credential file.

    ``field`` is the JSON field name (``private_key_path`` / ``known_hosts_path``)
    for the error message. The path may be **absolute or relative to the working
    directory** (the same convention as ``SYSTEM_PROMPT_PATH`` /
    ``ATTACHMENT_STORAGE_PATH`` / the MCP/infra config files — the app is run from
    the repo root, so ``config/id_…`` works). It must have **no ``~``** (no
    expansion — the value is used verbatim) and **no ``..`` segment**, and it must
    point at an *existing, readable, non-symlink regular file*. A symlink,
    directory, or missing file is a startup error so a botched key/known_hosts
    mount fails fast. The path itself is **never** echoed in the error (it is
    secret-adjacent) — only the field name is.
    """
    if not isinstance(path, str) or not path:
        raise ConfigError(f"{where} is missing a valid '{field}' (non-empty string)")
    if path.startswith("~"):
        raise ConfigError(f"{where} '{field}' must not contain a '~' (no expansion)")
    p = Path(path)
    if ".." in p.parts:
        raise ConfigError(f"{where} '{field}' must not contain a '..' path segment")
    try:
        if p.is_symlink():
            raise ConfigError(f"{where} '{field}' must not be a symbolic link")
        if not p.is_file():
            raise ConfigError(f"{where} '{field}' must point at an existing regular file")
        if not os.access(p, os.R_OK):
            raise ConfigError(f"{where} '{field}' must be a readable file")
        if require_nonempty and os.path.getsize(p) == 0:
            raise ConfigError(f"{where} '{field}' must not be an empty file")
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(f"{where} '{field}' is not a usable file: {exc.strerror or exc}") from exc


def _parse_infra_mounts(entry: dict, *, where: str) -> tuple[str, ...]:
    """Parse a target's ``mounts`` (a non-empty JSON array of absolute POSIX paths).

    Each entry must be an absolute POSIX path with no ``..`` segment, at most
    ``_INFRA_MOUNT_MAX_LEN`` chars. Entries are de-duplicated (order-preserving)
    and the list must stay within ``[1, _INFRA_MAX_MOUNTS]``. A validation error
    names the field and index — **never** the path value (a mount path is an
    operator filesystem detail that must not leak into a startup error).
    """
    raw = entry.get("mounts")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where} 'mounts' must be a non-empty JSON array of absolute paths")
    if len(raw) > _INFRA_MAX_MOUNTS:
        raise ConfigError(f"{where} 'mounts' may contain at most {_INFRA_MAX_MOUNTS} entries")
    seen: set[str] = set()
    out: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where} 'mounts[{index}]' must be a non-empty string")
        if value.startswith("~"):
            raise ConfigError(f"{where} 'mounts[{index}]' must be an absolute path (no '~')")
        if not value.startswith("/"):
            raise ConfigError(f"{where} 'mounts[{index}]' must be an absolute POSIX path")
        if ".." in value.split("/"):
            raise ConfigError(f"{where} 'mounts[{index}]' must not contain a '..' segment")
        if len(value) > _INFRA_MOUNT_MAX_LEN:
            raise ConfigError(f"{where} 'mounts[{index}]' exceeds {_INFRA_MOUNT_MAX_LEN} characters")
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    if not out:
        raise ConfigError(f"{where} 'mounts' must contain at least one distinct path")
    return tuple(out)


def _parse_infra_services(entry: dict, *, where: str) -> tuple[str, ...]:
    """Parse a target's ``services`` (a non-empty JSON array of systemd unit names).

    Each entry must match :data:`_INFRA_SERVICE_RE` and stay within
    ``_INFRA_SERVICE_MAX_LEN`` chars. De-duplicated (order-preserving), bounded by
    ``[1, _INFRA_MAX_SERVICES]``. The offending unit *name* **is** echoed (it is an
    operator-chosen unit, not a secret); an index is included for a type error.
    """
    raw = entry.get("services")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where} 'services' must be a non-empty JSON array of systemd unit names")
    if len(raw) > _INFRA_MAX_SERVICES:
        raise ConfigError(f"{where} 'services' may contain at most {_INFRA_MAX_SERVICES} entries")
    seen: set[str] = set()
    out: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where} 'services[{index}]' must be a non-empty string")
        if len(value) > _INFRA_SERVICE_MAX_LEN:
            raise ConfigError(f"{where} 'services[{index}]' exceeds {_INFRA_SERVICE_MAX_LEN} characters")
        if not _INFRA_SERVICE_RE.match(value):
            raise ConfigError(
                f"{where} 'services' entry {value!r} is not a valid systemd unit name "
                "(letters, digits, '_', '.', '@', '-')"
            )
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    if not out:
        raise ConfigError(f"{where} 'services' must contain at least one distinct unit")
    return tuple(out)


def _parse_infra_targets(raw: str) -> tuple[InfraSshTarget, ...]:
    """Parse + strictly validate the raw infra-SSH-targets JSON *text*.

    The text is a JSON *array* of target objects, resolved from the **default**
    ``config/infra_ssh_targets.json`` when present, the explicit
    ``INFRA_SSH_TARGETS_FILE`` when set (winning over both), or the inline
    ``INFRA_SSH_TARGETS`` when no file is present (see
    :func:`_load_infra_targets_text`); each
    carries exactly the :data:`_INFRA_TARGET_FIELDS` fields. An empty / blank
    value yields an empty tuple (no infrastructure provider). Anything malformed
    — invalid JSON, a non-array, a non-object entry, an unknown field, a bad/duplicate
    name, a bad host/port/username, a missing/unsafe key or known_hosts path, or an
    empty / over-long ``mounts`` / ``services`` — is a startup :class:`ConfigError`.

    Error messages name the *target* (once its name is known) or its *index*, and
    the *field* — **never** a host, the key path, the known_hosts path, a
    credential, or a mount path. The file *existence* checks mean a botched secret
    mount fails at startup rather than at the first tool call.
    """
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid INFRA_SSH_TARGETS configuration JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ConfigError("INFRA_SSH_TARGETS must be a JSON array of target objects")
    if len(data) > _INFRA_MAX_TARGETS:
        raise ConfigError(f"INFRA_SSH_TARGETS may contain at most {_INFRA_MAX_TARGETS} targets")

    targets: list[InfraSshTarget] = []
    seen: set[str] = set()
    for index, entry in enumerate(data):
        where = f"target #{index + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} in INFRA_SSH_TARGETS must be a JSON object")
        unknown = set(entry) - _INFRA_TARGET_FIELDS
        if unknown:
            raise ConfigError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where} is missing a valid 'name' (non-empty string)")
        where = f"target {name!r}"
        if not _INFRA_NAME_RE.match(name):
            raise ConfigError(
                f"{where} name must match [a-z][a-z0-9_-]{{0,31}} "
                "(lowercase start; lowercase letters, digits, '_', '-')"
            )
        if name in seen:
            raise ConfigError(f"duplicate infrastructure target name: {name!r}")
        seen.add(name)

        _validate_infra_host(entry.get("host"), where=where)
        host = entry["host"]

        port = entry.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"{where} 'port' must be an integer in 1..65535")

        username = entry.get("username")
        if not isinstance(username, str) or not _INFRA_USERNAME_RE.match(username):
            raise ConfigError(
                f"{where} 'username' must match [A-Za-z_][A-Za-z0-9_.-]{{0,31}}"
            )

        _validate_infra_file(entry.get("private_key_path"), where=where, field="private_key_path", require_nonempty=False)
        private_key_path = entry["private_key_path"]
        # The known_hosts file must also be non-empty — an empty pin is the same
        # as no pin, and no pin is exactly what we must never fall back to.
        _validate_infra_file(entry.get("known_hosts_path"), where=where, field="known_hosts_path", require_nonempty=True)
        known_hosts_path = entry["known_hosts_path"]

        mounts = _parse_infra_mounts(entry, where=where)
        services = _parse_infra_services(entry, where=where)

        targets.append(
            InfraSshTarget(
                name=name,
                host=host,
                port=port,
                username=username,
                private_key_path=private_key_path,
                known_hosts_path=known_hosts_path,
                mounts=mounts,
                services=services,
            )
        )
    return tuple(targets)


# ---------------------------------------------------------------------------
# Phase 9 (Automation, first slice): cron schedules
# ---------------------------------------------------------------------------
# A schedule name reuses the MCP/infra name charset — it is a stable identifier
# used in logs, the reserved-range synthetic conversation id, and the
# notification header.
_SCHEDULE_MAX_PROMPT_CHARS = 2000
_SCHEDULE_MAX_COUNT = 16
# The only fields a schedule entry may carry. Any other key is a startup error —
# a typo is a config error, never silently dropped (so an operator cannot
# accidentally ship a schedule that behaves differently from what they wrote).
_SCHEDULE_FIELDS = frozenset({"name", "cron", "prompt", "identity", "receiver"})
# The channel a schedule's ``identity`` may name, and the keys a ``receiver``
# object may carry. Both are a fixed two-channel vocabulary; an unknown value or
# key is a startup error (not a silent drop).
_SCHEDULE_IDENTITY_VALUES = frozenset({"telegram", "qq"})
_SCHEDULE_RECEIVER_CHANNELS = frozenset({"telegram", "qq"})
_SCHEDULE_TELEGRAM_RECEIVER_FIELDS = frozenset({"chat_id", "user_id"})
_SCHEDULE_QQ_RECEIVER_FIELDS = frozenset({"user_openid"})


def _load_schedules_text() -> str:
    """Return the raw schedules JSON *text* to validate, choosing its source.

    Source selection (the same file-over-inline idea as infra targets, with a
    well-known **default path** so the common single-file setup needs no env var):

    * ``SCHEDULES_FILE`` **set** (a path, CWD-relative) — it is the strict source
      of truth and **wins** over both the default file and the inline ``SCHEDULES``
      (both ignored). A set-but-**missing** or **blank** (0-byte / whitespace-only)
      file is a startup :class:`ConfigError` naming the path — an operator who
      pointed the provider at a file must not silently get "no schedules" (an
      explicit ``[]`` in the file is still valid and means none).
    * ``SCHEDULES_FILE`` **unset** — the **default file** (:data:`_SCHEDULES_DEFAULT_FILE`,
      ``config/schedules.json``) is used **when it exists**; a present-but-**blank**
      default file is a :class:`ConfigError`. When the default file is **absent**,
      it falls back to the inline ``SCHEDULES`` value (so inline-only config keeps
      working and the default-off case needs no file).
    """
    explicit = os.environ.get("SCHEDULES_FILE", "").strip()
    if explicit:
        try:
            text = Path(explicit).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read SCHEDULES_FILE '{explicit}': {exc.strerror or exc}") from exc
        if not text.strip():
            raise ConfigError(
                f"SCHEDULES_FILE '{explicit}' is empty; it must contain a JSON array "
                'of schedule objects (use "[]" for none, or unset SCHEDULES_FILE)'
            )
        return text
    default_path = Path(_SCHEDULES_DEFAULT_FILE)
    if default_path.exists():
        try:
            text = default_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read default schedules file '{_SCHEDULES_DEFAULT_FILE}': {exc.strerror or exc}") from exc
        if not text.strip():
            raise ConfigError(
                f"default schedules file '{_SCHEDULES_DEFAULT_FILE}' is empty; it must contain a "
                'JSON array of schedule objects (use "[]" for none, or remove the file)'
            )
        return text
    return os.environ.get("SCHEDULES", "")


def _parse_schedule_telegram_receiver(where: str, raw: object) -> ScheduleTelegramReceiver:
    """Validate a ``receiver.telegram`` object into a :class:`ScheduleTelegramReceiver`.

    Both ``chat_id`` and ``user_id`` must be present positive ints (bools
    rejected). Error text names the schedule + field, never a value.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 'receiver.telegram' must be a JSON object")
    unknown = set(raw) - _SCHEDULE_TELEGRAM_RECEIVER_FIELDS
    if unknown:
        raise ConfigError(f"{where} 'receiver.telegram' has unknown field(s): {', '.join(sorted(unknown))}")
    for id_field in ("chat_id", "user_id"):
        value = raw.get(id_field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{where} 'receiver.telegram.{id_field}' must be a positive integer")
    return ScheduleTelegramReceiver(chat_id=raw["chat_id"], user_id=raw["user_id"])


def _parse_schedule_qq_receiver(where: str, raw: object) -> ScheduleQQReceiver:
    """Validate a ``receiver.qq`` object into a :class:`ScheduleQQReceiver`.

    ``user_openid`` must be a non-empty string. Error text names the field, never
    the openid value (an openid is a per-app identity, not to be echoed in a
    startup error).
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 'receiver.qq' must be a JSON object")
    unknown = set(raw) - _SCHEDULE_QQ_RECEIVER_FIELDS
    if unknown:
        raise ConfigError(f"{where} 'receiver.qq' has unknown field(s): {', '.join(sorted(unknown))}")
    openid = raw.get("user_openid")
    if not isinstance(openid, str) or not openid.strip():
        raise ConfigError(f"{where} 'receiver.qq.user_openid' must be a non-empty string")
    return ScheduleQQReceiver(user_openid=openid)


def _parse_schedules(raw: str) -> tuple[ScheduleSpec, ...]:
    """Parse + strictly validate the raw schedules JSON *text* into a tuple of
    :class:`ScheduleSpec`. The text is a JSON *array* (resolved from the default
    ``config/schedules.json`` when present, the explicit ``SCHEDULES_FILE`` when
    set, or the inline ``SCHEDULES`` — see :func:`_load_schedules_text`); each
    element carries exactly the :data:`_SCHEDULE_FIELDS` fields. An empty / blank
    value yields an empty tuple (no automation). Anything malformed is a startup
    :class:`ConfigError`:

    * invalid JSON, a non-array, a non-object entry, an unknown field,
    * a missing / bad / duplicate ``name`` (must match the name charset),
    * more than :data:`_SCHEDULE_MAX_COUNT` schedules,
    * a missing / non-string ``cron`` or a cron that :func:`.automation.cron.parse_cron`
      rejects (a bad cron is a startup failure, never a silent "never fires"),
    * a missing ``identity`` or one not in :data:`_SCHEDULE_IDENTITY_VALUES`,
    * a missing / non-object / empty ``receiver``, an unknown ``receiver`` key,
      or — within a present receiver — a bad / missing ``chat_id`` / ``user_id``
      (a positive int, bools rejected) or a bad / missing ``user_openid``
      (a non-empty string),
    * an ``identity`` whose ``receiver`` is **not** present (e.g. ``identity:
      "qq"`` with no ``receiver.qq``), which is what enforces "at least one
      receiver present",
    * a missing / empty ``prompt`` or a ``prompt`` over
      :data:`_SCHEDULE_MAX_PROMPT_CHARS` chars.

    Error messages name the *schedule* (once its name is known) or its *index*,
    and the *field* — **never** the ``prompt`` body or a ``user_openid`` value,
    mirroring the "don't echo the value" rule for the other providers.
    """
    # Imported here (not at module top) so the pure cron parser stays importable
    # without pulling in the rest of the config module's runtime dependencies.
    from .automation.cron import CronError, parse_cron

    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid SCHEDULES configuration JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ConfigError("SCHEDULES must be a JSON array of schedule objects")
    if len(data) > _SCHEDULE_MAX_COUNT:
        raise ConfigError(f"SCHEDULES may contain at most {_SCHEDULE_MAX_COUNT} schedules")

    schedules: list[ScheduleSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(data):
        where = f"schedule #{index + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} in SCHEDULES must be a JSON object")
        unknown = set(entry) - _SCHEDULE_FIELDS
        if unknown:
            raise ConfigError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where} is missing a valid 'name' (non-empty string)")
        where = f"schedule {name!r}"
        if not _INFRA_NAME_RE.match(name):
            raise ConfigError(
                f"{where} name must match [a-z][a-z0-9_-]{{0,31}} "
                "(lowercase start; lowercase letters, digits, '_', '-')"
            )
        if name in seen:
            raise ConfigError(f"duplicate schedule name: {name!r}")
        seen.add(name)

        cron = entry.get("cron")
        if not isinstance(cron, str) or not cron.strip():
            raise ConfigError(f"{where} is missing a valid 'cron' (non-empty string)")
        try:
            parse_cron(cron)
        except CronError as exc:
            raise ConfigError(f"{where} 'cron' is invalid: {exc}") from exc

        identity = entry.get("identity")
        if not isinstance(identity, str) or identity not in _SCHEDULE_IDENTITY_VALUES:
            raise ConfigError(
                f"{where} 'identity' must be one of: {', '.join(sorted(_SCHEDULE_IDENTITY_VALUES))}"
            )

        receiver = entry.get("receiver")
        if not isinstance(receiver, dict) or not receiver:
            raise ConfigError(
                f"{where} is missing a valid 'receiver' (a non-empty object with at "
                f'least one of: {", ".join(sorted(_SCHEDULE_RECEIVER_CHANNELS))})'
            )
        unknown_recv = set(receiver) - _SCHEDULE_RECEIVER_CHANNELS
        if unknown_recv:
            raise ConfigError(f"{where} 'receiver' has unknown channel(s): {', '.join(sorted(unknown_recv))}")

        telegram_recv = (
            _parse_schedule_telegram_receiver(where, receiver["telegram"]) if "telegram" in receiver else None
        )
        qq_recv = _parse_schedule_qq_receiver(where, receiver["qq"]) if "qq" in receiver else None

        # The identity must name a present receiver. This is what guarantees at
        # least one receiver is present (the identity's, at minimum).
        if identity == "telegram" and telegram_recv is None:
            raise ConfigError(f"{where} identity is 'telegram' but 'receiver.telegram' is missing")
        if identity == "qq" and qq_recv is None:
            raise ConfigError(f"{where} identity is 'qq' but 'receiver.qq' is missing")

        prompt = entry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError(f"{where} is missing a valid 'prompt' (non-empty string)")
        if len(prompt) > _SCHEDULE_MAX_PROMPT_CHARS:
            raise ConfigError(
                f"{where} 'prompt' exceeds {_SCHEDULE_MAX_PROMPT_CHARS} characters"
            )

        schedules.append(
            ScheduleSpec(
                name=name,
                cron=cron,
                prompt=prompt,
                identity=identity,
                telegram=telegram_recv,
                qq=qq_recv,
            )
        )
    return tuple(schedules)


def _parse_exec_deny_patterns(raw: str) -> tuple[str, ...]:
    """Parse ``EXEC_POLICY_DENY_PATTERNS`` — an add-only JSON array of regex strings.
    non-array, a non-string element, or a pattern that does not compile is a
    startup ``ConfigError`` — a bad pattern must never be silently dropped
    (that would *weaken* the backstop). The patterns are stored as raw strings;
    they are compiled into the tool's denylist by ``compile_denylist`` at
    construction, so compilation is also covered here for fail-fast. Error text
    names only the offending index — never the pattern body.
    """
    text = raw.strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("EXEC_POLICY_DENY_PATTERNS must be a JSON array of regex strings") from None
    if not isinstance(data, list):
        raise ConfigError("EXEC_POLICY_DENY_PATTERNS must be a JSON array of regex strings")
    out: list[str] = []
    for i, pat in enumerate(data):
        if not isinstance(pat, str) or not pat.strip():
            raise ConfigError(f"EXEC_POLICY_DENY_PATTERNS[{i}] must be a non-empty regex string")
        try:
            re.compile(pat)
        except re.error:
            raise ConfigError(f"EXEC_POLICY_DENY_PATTERNS[{i}] is not a valid regex") from None
        out.append(pat)
    return tuple(out)


def load_config() -> Config:
    """Build a validated :class:`Config` from the environment.

    The LLM endpoint and model have no hardcoded default — they must be
    provided via env (``OPENAI_*``), kept out of the repo for privacy.
    """
    _load_env()
    # Phase 2.1: whether the tool loop is on at all (needed below to decide
    # whether a malformed MCP-permissions file is a startup error).
    enable_tools = _parse_bool(os.environ.get("ENABLE_TOOLS", ""), True)
    # Phase 4.x: the dedicated MCP-tool permission file. A set-but-malformed
    # file (with tools on) is a ConfigError (fail-to-start); missing/blank is
    # fine (the backend seeds it at startup). See _load_mcp_permissions_file.
    mcp_permissions_file = _load_mcp_permissions_file(enable_tools)
    # Phase 4: parse the MCP knobs. The insecure-http opt-in is read first
    # because it gates how strict the per-server URL scheme check is.
    mcp_allow_insecure_http = _parse_bool(os.environ.get("MCP_ALLOW_INSECURE_HTTP", ""), False)
    mcp_connect_timeout_seconds = _parse_float(os.environ.get("MCP_CONNECT_TIMEOUT_SECONDS", ""), 10.0)
    max_mcp_tool_result_chars = _parse_int(os.environ.get("MAX_MCP_TOOL_RESULT_CHARS", ""), 10000)
    mcp_servers = _parse_mcp_servers(
        _load_mcp_servers_text(), allow_insecure_http=mcp_allow_insecure_http
    )
    # Phase 4.x: user-level OAuth for MCP. The callback base URL is optional —
    # unset means OAuth is not configured (no callback server, and ``/mcp auth``
    # reports "not configured"). The Google client id/secret are intentionally
    # **not** read here: they are read from the environment only when the
    # composition root builds the Google provider, so a config object never
    # carries them and they can never be logged from one.
    oauth_callback_base_url = os.environ.get("OAUTH_CALLBACK_BASE_URL", "").strip() or None
    if oauth_callback_base_url is not None:
        _validate_oauth_callback_base(oauth_callback_base_url)
    oauth_callback_port = _parse_int(os.environ.get("OAUTH_CALLBACK_PORT", ""), 8090)
    oauth_state_ttl_seconds = _parse_float(os.environ.get("OAUTH_STATE_TTL_SECONDS", ""), 600.0)
    # Phase 5.1: read-only infrastructure observation over SSH. All three knobs
    # are read (the numeric ones feed the ``__post_init__`` cross-knob check);
    # the targets are parsed the same way ``mcp_servers`` is — unconditionally —
    # so a configured target set is always validated. The target source is the
    # **default** ``config/infra_ssh_targets.json`` when that file is present, or
    # the explicit ``INFRA_SSH_TARGETS_FILE`` when set (which wins over the
    # default and the inline value; a set-but-missing/blank file is a
    # ConfigError), or the inline ``INFRA_SSH_TARGETS`` when no file is present —
    # see _load_infra_targets_text. Because the target parse checks that the
    # private-key / known_hosts files exist, this is a startup error only when
    # the resolved target set is non-empty (empty = no provider, no credential
    # check, no error — mirroring empty ``MCP_SERVERS``).
    infra_ssh_connect_timeout_seconds = _parse_float(
        os.environ.get("INFRA_SSH_CONNECT_TIMEOUT_SECONDS", ""), 10.0
    )
    max_infra_tool_result_chars = _parse_int(os.environ.get("MAX_INFRA_TOOL_RESULT_CHARS", ""), 8000)
    infra_ssh_targets = _parse_infra_targets(_load_infra_targets_text())
    # Phase 9 (Automation, first slice): cron schedules. The sources are the
    # default ``config/schedules.json`` when present, the explicit SCHEDULES_FILE
    # when set (winning over the default + inline), or the inline SCHEDULES — see
    # _load_schedules_text. Parsed unconditionally (empty = no automation, no
    # scheduler task, isomorphic to empty MCP_SERVERS / INFRA_SSH_TARGETS).
    # SCHEDULE_TIMEZONE (when set) is validated in Config.__post_init__.
    schedules = _parse_schedules(_load_schedules_text())
    schedule_timezone = os.environ.get("SCHEDULE_TIMEZONE", "").strip()
    # Exec shell tool. The opt-in flag is read first (it gates the numeric
    # validation in __post_init__); the deny patterns are always parsed so a
    # configured-but-bad list fails at startup even before the tool is built.
    enable_exec_tool = _parse_bool(os.environ.get("ENABLE_EXEC_TOOL", ""), False)
    max_exec_tool_result_chars = _parse_int(os.environ.get("MAX_EXEC_TOOL_RESULT_CHARS", ""), 8000)
    exec_workdir = os.environ.get("EXEC_WORKDIR", "").strip() or None
    exec_policy_deny_patterns = _parse_exec_deny_patterns(os.environ.get("EXEC_POLICY_DENY_PATTERNS", ""))
    # File toolset (opt-in, mirrors the exec knobs). The confinement root is
    # read the same way as exec_workdir; the numeric caps default to the values
    # the tools use to build their schemas / bound read + list output.
    enable_file_tool = _parse_bool(os.environ.get("ENABLE_FILE_TOOL", ""), False)
    file_workdir = os.environ.get("FILE_WORKDIR", "").strip() or None
    max_file_string_chars = _parse_int(os.environ.get("MAX_FILE_STRING_CHARS", ""), 2000)
    max_file_read_chars = _parse_int(os.environ.get("MAX_FILE_READ_CHARS", ""), 8000)
    max_file_list_entries = _parse_int(os.environ.get("MAX_FILE_LIST_ENTRIES", ""), 1000)
    max_file_content_chars = _parse_int(os.environ.get("MAX_FILE_CONTENT_CHARS", ""), 20000)
    # Streaming replies (private chats): on by default; a bad value fails fast
    # like every other bool knob.
    enable_streaming = _parse_bool(os.environ.get("ENABLE_STREAMING", ""), True)
    # Phase 10 (multi-channel): QQ. The app id is optional ("" = channel off →
    # no client, no websocket). The client secret is intentionally **not** read
    # here — the composition root reads it from the environment when it builds
    # the QQ client, so no Config object carries it (and it can never be logged
    # from one). ``qq_app_id`` is the operator-chosen non-secret identifier and
    # is stored on the frozen config (it may appear in startup logs).
    qq_app_id = os.environ.get("QQ_APP_ID", "").strip()
    return Config(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_user_ids=_parse_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "").strip(),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_model=os.environ.get("OPENAI_MODEL", "").strip(),
        openai_timeout=_parse_float(os.environ.get("OPENAI_TIMEOUT", ""), 120.0),
        database_url=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/agent.db").strip(),
        system_prompt_path=Path(os.environ.get("SYSTEM_PROMPT_PATH", "config/system_prompt.txt")),
        max_context_messages=_parse_int(os.environ.get("MAX_CONTEXT_MESSAGES", ""), 50),
        max_context_estimated_tokens=_parse_int(os.environ.get("MAX_CONTEXT_ESTIMATED_TOKENS", ""), 200000),
        context_image_estimated_tokens=_parse_int(os.environ.get("CONTEXT_IMAGE_ESTIMATED_TOKENS", ""), 2000),
        enable_tools=enable_tools,
        max_tool_iterations=_parse_int(os.environ.get("MAX_TOOL_ITERATIONS", ""), 20),
        max_image_size_mb=_parse_float(os.environ.get("MAX_IMAGE_SIZE_MB", ""), 10.0),
        attachment_storage_path=Path(os.environ.get("ATTACHMENT_STORAGE_PATH", "./data/attachments")),
        max_memories_per_scope=_parse_int(os.environ.get("MAX_MEMORIES_PER_SCOPE", ""), 200),
        max_memory_chars=_parse_int(os.environ.get("MAX_MEMORY_CHARS", ""), 1000),
        max_retrieved_memories=_parse_int(os.environ.get("MAX_RETRIEVED_MEMORIES", ""), 5),
        max_memory_estimated_tokens=_parse_int(os.environ.get("MAX_MEMORY_ESTIMATED_TOKENS", ""), 3000),
        mcp_permissions_file=mcp_permissions_file,
        tool_approval_timeout_seconds=_parse_float(os.environ.get("TOOL_APPROVAL_TIMEOUT_SECONDS", ""), 60.0),
        tool_timeout_seconds=_parse_float(os.environ.get("TOOL_TIMEOUT_SECONDS", ""), 30.0),
        mcp_servers=mcp_servers,
        mcp_connect_timeout_seconds=mcp_connect_timeout_seconds,
        max_mcp_tool_result_chars=max_mcp_tool_result_chars,
        mcp_allow_insecure_http=mcp_allow_insecure_http,
        oauth_callback_base_url=oauth_callback_base_url,
        oauth_callback_port=oauth_callback_port,
        oauth_state_ttl_seconds=oauth_state_ttl_seconds,
        infra_ssh_targets=infra_ssh_targets,
        infra_ssh_connect_timeout_seconds=infra_ssh_connect_timeout_seconds,
        max_infra_tool_result_chars=max_infra_tool_result_chars,
        enable_exec_tool=enable_exec_tool,
        max_exec_tool_result_chars=max_exec_tool_result_chars,
        exec_workdir=exec_workdir,
        exec_policy_deny_patterns=exec_policy_deny_patterns,
        enable_file_tool=enable_file_tool,
        file_workdir=file_workdir,
        max_file_string_chars=max_file_string_chars,
        max_file_read_chars=max_file_read_chars,
        max_file_list_entries=max_file_list_entries,
        max_file_content_chars=max_file_content_chars,
        enable_streaming=enable_streaming,
        qq_app_id=qq_app_id,
        schedules=schedules,
        schedule_timezone=schedule_timezone,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        log_color=_normalize_log_color(os.environ.get("LOG_COLOR", "")),
        system_prompt_override=os.environ.get("SYSTEM_PROMPT", "").strip() or None,
    )
