"""Runtime configuration.

All external configuration is read from environment variables (optionally
loaded from a local ``.env`` file). Secrets (the Telegram bot token and the
OpenAI API key) come *only* from the environment and must never be committed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from .tools.policy import ToolPolicyError, parse_tool_permission_overrides


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class McpServer:
    """One validated, operator-configured remote MCP Streamable HTTP server.

    ``bearer_token_env`` is the *name* of an environment variable holding the
    bearer token — **never the token itself** (the token stays env-only, out of
    the frozen config and out of logs). It is ``None`` when the server needs no
    operator bearer auth. The ``url`` is guaranteed, at config-parse time, to be
    an absolute ``https://`` URL (``http://`` only under an explicit insecure
    opt-in) with no userinfo, fragment, query, or missing host.

    ``auth_type`` / ``auth_provider`` (phase 4.x) declare *user-level* OAuth:
    ``auth_type`` is ``"none"`` (the default) or ``"oauth"``; when ``"oauth"``
    the ``auth_provider`` (e.g. ``"google"``) names the OAuth provider whose
    *per-user* access token is attached to this server's requests at request
    time. The provider's client id/secret are read from the environment and are
    never stored here.
    """

    name: str
    url: str
    bearer_token_env: str | None = None
    auth_type: str = "none"
    auth_provider: str | None = None


def _load_env() -> None:
    # Load an uncommitted .env from the current working directory. Existing
    # environment variables always take precedence over values in the file.
    load_dotenv(override=False)


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

    # Phase 3: tool security. The permission map is pre-parsed into
    # name→ToolPermission (empty = use each tool's declared default, which is
    # ``ask`` unless a built-in opted into ``allow``). Both timeouts are seconds.
    tool_permission_overrides: dict = field(default_factory=dict)
    tool_approval_timeout_seconds: float = 60.0
    tool_timeout_seconds: float = 30.0

    # Phase 4: remote MCP tool provider (Streamable HTTP). ``mcp_servers`` is the
    # parsed, validated list (empty = no MCP servers → the manager never starts
    # and no MCP network connection is ever made). The two numeric knobs are
    # seconds / max-chars and both must be positive; ``mcp_allow_insecure_http``
    # is a hard opt-in to ``http://`` (default off → https-only).
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
        # Phase 3: tool-security knobs. The permission map was already parsed
        # (malformed / illegal-policy / duplicate entries raised ConfigError in
        # load_config); here we check the *names* match the allowed tool-name
        # charset and that both timeouts are positive. A botched security setting
        # is a startup error, never silently ignored.
        _tool_name_re = re.compile(r"^[A-Za-z0-9_-]+$")
        for tool_name in self.tool_permission_overrides:
            if not _tool_name_re.match(tool_name):
                raise ConfigError(
                    f"TOOL_PERMISSION_OVERRIDES tool name {tool_name!r} is invalid "
                    "(only letters, digits, '_', '-' are allowed)"
                )
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
# in the config is a startup error, never silently dropped.
_MCP_SERVER_FIELDS = frozenset({"name", "url", "bearer_token_env", "authentication"})
# The only fields the optional ``authentication`` object may carry.
_MCP_AUTH_FIELDS = frozenset({"type", "provider"})
# ``auth_type`` values; ``oauth`` is the only authenticated kind this phase
# implements (api_key / basic / custom are out of scope).
_MCP_AUTH_TYPES = frozenset({"none", "oauth"})
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


def _parse_mcp_servers(raw: str, *, allow_insecure_http: bool) -> tuple[McpServer, ...]:
    """Parse + strictly validate ``MCP_SERVERS`` into a tuple of :class:`McpServer`.

    The value is a JSON *array*; each element is an object with ``name`` (unique,
    matching the name charset), ``url`` (a safe absolute URL — see
    :func:`_validate_mcp_url`), and optional ``bearer_token_env`` (an env-var
    *name* whose value must be present and non-empty at startup). An empty /
    blank value yields an empty tuple (no MCP servers). Anything malformed —
    invalid JSON, a non-array, a non-object entry, an unknown field, a bad name
    or URL, a duplicate name, a malformed env-var name, or a referenced env var
    that is missing/empty — is a startup :class:`ConfigError`. Error messages
    name the *server* and the *field*, never a token or the full URL.
    """
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid MCP_SERVERS JSON: {exc.msg}") from exc
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

        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"{where} is missing a valid 'url' (non-empty string)")
        _validate_mcp_url(url, allow_insecure_http=allow_insecure_http)

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
                url=url,
                bearer_token_env=token_env,
                auth_type=auth_type,
                auth_provider=auth_provider,
            )
        )
    return tuple(servers)


def load_config() -> Config:
    """Build a validated :class:`Config` from the environment.

    The LLM endpoint and model have no hardcoded default — they must be
    provided via env (``OPENAI_*``), kept out of the repo for privacy.
    """
    _load_env()
    # Phase 3: parse the tool permission overrides up front. A malformed /
    # illegal / duplicate entry is a ConfigError (startup), never silently ignored.
    try:
        tool_permission_overrides = parse_tool_permission_overrides(
            os.environ.get("TOOL_PERMISSION_OVERRIDES", "")
        )
    except ToolPolicyError as exc:
        raise ConfigError(f"invalid TOOL_PERMISSION_OVERRIDES: {exc}") from exc
    # Phase 4: parse the MCP knobs. The insecure-http opt-in is read first
    # because it gates how strict the per-server URL scheme check is.
    mcp_allow_insecure_http = _parse_bool(os.environ.get("MCP_ALLOW_INSECURE_HTTP", ""), False)
    mcp_connect_timeout_seconds = _parse_float(os.environ.get("MCP_CONNECT_TIMEOUT_SECONDS", ""), 10.0)
    max_mcp_tool_result_chars = _parse_int(os.environ.get("MAX_MCP_TOOL_RESULT_CHARS", ""), 10000)
    mcp_servers = _parse_mcp_servers(
        os.environ.get("MCP_SERVERS", ""), allow_insecure_http=mcp_allow_insecure_http
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
        max_context_estimated_tokens=_parse_int(os.environ.get("MAX_CONTEXT_ESTIMATED_TOKENS", ""), 24000),
        context_image_estimated_tokens=_parse_int(os.environ.get("CONTEXT_IMAGE_ESTIMATED_TOKENS", ""), 2000),
        enable_tools=_parse_bool(os.environ.get("ENABLE_TOOLS", ""), True),
        max_tool_iterations=_parse_int(os.environ.get("MAX_TOOL_ITERATIONS", ""), 5),
        max_image_size_mb=_parse_float(os.environ.get("MAX_IMAGE_SIZE_MB", ""), 10.0),
        attachment_storage_path=Path(os.environ.get("ATTACHMENT_STORAGE_PATH", "./data/attachments")),
        max_memories_per_scope=_parse_int(os.environ.get("MAX_MEMORIES_PER_SCOPE", ""), 200),
        max_memory_chars=_parse_int(os.environ.get("MAX_MEMORY_CHARS", ""), 1000),
        max_retrieved_memories=_parse_int(os.environ.get("MAX_RETRIEVED_MEMORIES", ""), 5),
        max_memory_estimated_tokens=_parse_int(os.environ.get("MAX_MEMORY_ESTIMATED_TOKENS", ""), 3000),
        tool_permission_overrides=tool_permission_overrides,
        tool_approval_timeout_seconds=_parse_float(os.environ.get("TOOL_APPROVAL_TIMEOUT_SECONDS", ""), 60.0),
        tool_timeout_seconds=_parse_float(os.environ.get("TOOL_TIMEOUT_SECONDS", ""), 30.0),
        mcp_servers=mcp_servers,
        mcp_connect_timeout_seconds=mcp_connect_timeout_seconds,
        max_mcp_tool_result_chars=max_mcp_tool_result_chars,
        mcp_allow_insecure_http=mcp_allow_insecure_http,
        oauth_callback_base_url=oauth_callback_base_url,
        oauth_callback_port=oauth_callback_port,
        oauth_state_ttl_seconds=oauth_state_ttl_seconds,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        log_color=_normalize_log_color(os.environ.get("LOG_COLOR", "")),
        system_prompt_override=os.environ.get("SYSTEM_PROMPT", "").strip() or None,
    )
