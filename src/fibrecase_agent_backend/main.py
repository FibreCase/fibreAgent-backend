"""Application entrypoint and composition root.

Wires the low-coupling pieces together and runs the long-polling bot:

    config ─┐
    database ─┼─▶ AgentService ─▶ Telegram Application (long polling)
    llm ──────┤            ▲
    tools ────┘            └─ tool registry (only active when ENABLE_TOOLS)

This module is the *only* place that constructs the concrete LLM client,
engine, repository, tool registry and service; everything else is passed down.
"""

from __future__ import annotations

import logging
import os
import sys

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .agent.service import AgentService
from .attachments import AttachmentStore
from .config import Config, ConfigError, McpServer, load_config
from .database.audit import RepositoryToolAuditor
from .database.oauth import OAuthStorageImpl
from .database.repository import ConversationRepository
from .database.session import create_engine, create_session_factory, init_db
from .infrastructure import build_infra_tools
from .llm.client import OpenAIClient
from .logging_setup import configure_logging
from .mcp import McpManager
from .mcp.auth import (
    GoogleOAuthProvider,
    McpOAuthAuth,
    OAuthManager,
    OAuthProvider,
    build_oauth_callback_server,
)
from .telegram.approval import TelegramApprovalBroker
from .telegram.bot import build_application, compose_startup_hooks, register_command_menu
from .tools import FileBackedToolPolicy, build_policy, reconcile_permissions_file
from .tools.builtin import build_default_tools

logger = logging.getLogger("main")

# Phase 4.x: the *only* place that knows a provider's concrete env-var names.
# This is the single provider registry — the rest of the codebase (config,
# manager, storage, the Telegram layer) stays provider-agnostic and never
# special-cases "google".
_GOOGLE_CLIENT_ID_ENV = "GOOGLE_OAUTH_CLIENT_ID"
_GOOGLE_CLIENT_SECRET_ENV = "GOOGLE_OAUTH_CLIENT_SECRET"
_GOOGLE_SCOPES_ENV = "GOOGLE_OAUTH_SCOPES"


class AgentBackend:
    """Owns the runtime objects and their lifecycle (startup/shutdown).

    The Telegram ``Application.run_polling()`` call is *blocking* and owns its
    own event loop (it must not itself be awaited inside ``asyncio.run``). So
    we drive the whole program from a plain synchronous function and do our
    own DB/LLM work inside the application's ``post_init`` / ``post_shutdown``
    hooks, which PTB runs inside that loop.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.engine = create_engine(config.database_url)
        self.session_factory = create_session_factory(self.engine)
        self.repository = ConversationRepository(self.session_factory)
        # Content-addressed blob store for persistent image attachments. The root
        # directory is created on demand by the store (Docker's ./data mount
        # covers the default ./data/attachments path).
        self.attachment_store = AttachmentStore(config.attachment_storage_path)
        self.llm = OpenAIClient(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key,
            model=config.openai_model,
            timeout=config.openai_timeout,
        )
        # The tool registry is built only when tools are enabled; when disabled
        # the service degrades to the phase-one single-completion path. The two
        # state-changing tools are added only when their opt-ins are on (a default
        # deployment stays subprocess-free and touch-free).
        if config.enable_tools:
            registry = build_default_tools(
                enable_exec=config.enable_exec_tool,
                max_exec_output_chars=config.max_exec_tool_result_chars,
                exec_workdir=config.exec_workdir,
                exec_policy_deny_patterns=config.exec_policy_deny_patterns,
                enable_edit=config.enable_edit_tool,
                edit_workdir=config.edit_workdir,
                max_edit_string_chars=config.max_edit_string_chars,
                max_edit_read_chars=config.max_edit_read_chars,
            )
        else:
            registry = None
        self.registry = registry
        # Phase 3: the tool-security runtime. All three are built *only* when
        # tools are enabled — with tools off there is nothing to advertise,
        # approve, or audit, so the service stays on the bare phase-one path.
        #   * policy — resolves each tool to allow/ask/deny. When
        #     ``MCP_PERMISSIONS_FILE`` is set this is a *file-backed* policy:
        #     MCP-tool overrides come from that (backend-maintained, hot-reloaded)
        #     file, and built-ins always ride their declared defaults. Without
        #     a file, a plain policy with no overrides (built-in defaults only).
        #   * auditor — the concrete SQLite-backed auditor (fail-closed on the
        #     pre-execution write).
        #   * broker — the in-memory Telegram Approve/Deny provider, also used
        #     by the adapter for the callback + /tool_audit.
        if registry is None:
            policy = None
        elif config.mcp_permissions_file is not None:
            policy = FileBackedToolPolicy(config.mcp_permissions_file, registry)
        else:
            policy = build_policy({}, registry=registry)
        auditor = RepositoryToolAuditor(self.repository) if registry else None
        broker = TelegramApprovalBroker(self.repository) if registry else None
        self.approval_broker = broker
        # Phase 4.x: user-level OAuth for MCP. The manager is built **only**
        # when a callback base URL is configured *and* at least one provider's
        # client credentials are present *and* at least one server declares
        # user-level OAuth — otherwise it does not exist, no callback server
        # starts, and ``/mcp auth`` simply reports "not configured". The
        # provider's client id/secret are read from the environment **here and
        # only here** (in-memory; never stored on config, never logged).
        self.oauth_manager: OAuthManager | None = None
        self.oauth_callback_server = None
        self._oauth_providers: dict[str, OAuthProvider] = {}
        self._has_oauth = False
        self._setup_oauth()
        # Phase 4: remote MCP tool provider. Built **only** when tools are
        # enabled *and* at least one server is configured — with no servers
        # there is nothing to connect, so the manager does not exist and no MCP
        # network connection is ever made. The manager holds no reference to the
        # registry here: the discovered tools are ``add``ed to the *same*
        # registry inside ``_post_init`` (after discovery), so they ride the
        # existing phase-3 gate exactly like a built-in.
        self.mcp_manager = (
            McpManager(
                config.mcp_servers,
                connect_timeout_seconds=config.mcp_connect_timeout_seconds,
                max_result_chars=config.max_mcp_tool_result_chars,
                oauth_auth_factory=self._mcp_oauth_auth if self._has_oauth else None,
            )
            if (config.enable_tools and config.mcp_servers)
            else None
        )
        # Phase 5.1: read-only infrastructure observation over SSH. Built **only**
        # when tools are enabled *and* at least one target is configured — with no
        # targets there is nothing to observe, so no tools are built and (because
        # the provider lazy-imports asyncssh) no SSH machinery is loaded. Like the
        # MCP tools, the infra tools are not registered here: they are ``add``ed
        # to the *same* registry in ``_post_init`` (after the built-ins and MCP),
        # so they ride the existing phase-3 gate exactly like a built-in. Each is
        # a *local* read-only tool that declares ``allow`` (strictly read-only, so
        # it runs without a per-call approval, like ``get_current_time``/``echo``);
        # the declared default is final.
        self.infra_tools = (
            build_infra_tools(
                config.infra_ssh_targets,
                connect_timeout_seconds=config.infra_ssh_connect_timeout_seconds,
                max_result_chars=config.max_infra_tool_result_chars,
            )
            if (config.enable_tools and config.infra_ssh_targets)
            else []
        )
        self.service = AgentService(
            self.repository,
            self.llm,
            system_prompt=config.system_prompt,
            max_context_messages=config.max_context_messages,
            max_context_estimated_tokens=config.max_context_estimated_tokens,
            context_image_estimated_tokens=config.context_image_estimated_tokens,
            registry=registry,
            enable_tools=config.enable_tools,
            max_tool_iterations=config.max_tool_iterations,
            attachment_store=self.attachment_store,
            max_memories_per_scope=config.max_memories_per_scope,
            max_memory_chars=config.max_memory_chars,
            max_retrieved_memories=config.max_retrieved_memories,
            max_memory_estimated_tokens=config.max_memory_estimated_tokens,
            policy=policy,
            approval_provider=broker,
            auditor=auditor,
            tool_timeout_seconds=config.tool_timeout_seconds,
            tool_approval_timeout_seconds=config.tool_approval_timeout_seconds,
        )
        application = build_application(
            config,
            self.service,
            self.repository,
            approval_broker=broker,
            mcp_manager=self.mcp_manager,
            oauth_manager=self.oauth_manager,
        )
        # Chain the Telegram adapter's command-menu registration with our own
        # DB init into a single post_init (both run inside the app's loop).
        application.post_init = compose_startup_hooks(register_command_menu, self._post_init)
        application.post_shutdown = self._post_shutdown
        self.application = application

    # Phase 4.x: user-level OAuth setup (provider registry + manager) -----------
    def _setup_oauth(self) -> None:
        """Build the OAuth provider registry and :class:`OAuthManager`, if at all.

        OAuth is activated only when **all** of these hold: a callback base URL
        is configured, at least one MCP server declares ``auth_type == "oauth"``,
        and every referenced provider's client credentials are present in the
        environment. A missing provider credential leaves that provider out of
        the registry (its server reports ``provider_not_configured``) rather
        than failing the whole startup — the bot must never fail to boot because
        an optional credential is absent. A provider is referenced but has
        *both* credentials missing vs present is decided purely on the env; the
        *name* → env mapping lives only here.
        """
        config = self.config
        if config.oauth_callback_base_url is None:
            return
        oauth_servers = [s for s in config.mcp_servers if s.auth_type == "oauth"]
        if not oauth_servers:
            return
        # Build each referenced provider from its env credentials (in-memory).
        for spec in oauth_servers:
            provider = self._build_provider(spec.auth_provider)
            if provider is not None and provider.name not in self._oauth_providers:
                self._oauth_providers[provider.name] = provider
        server_providers = {spec.name: spec.auth_provider for spec in oauth_servers}
        if not self._oauth_providers:
            # No provider could be built (missing credentials): leave OAuth
            # off. ``/mcp auth`` then reports "OAuth not configured" for every
            # OAuth server, and the servers fail to start with a stable code.
            return
        self._has_oauth = True
        self.oauth_manager = OAuthManager(
            storage=OAuthStorageImpl(self.session_factory),
            providers=self._oauth_providers,
            server_providers=server_providers,
            callback_base_url=config.oauth_callback_base_url,
            state_ttl_seconds=config.oauth_state_ttl_seconds,
            notifier=self._oauth_notifier,
        )

    def _build_provider(self, provider_name: str) -> OAuthProvider | None:
        """The single, explicit provider registry (env names live **here only**).

        A future GitHub / Microsoft provider is a new branch here plus a new
        env pair — nothing elsewhere learns about it.
        """
        if provider_name == "google":
            client_id = os.environ.get(_GOOGLE_CLIENT_ID_ENV, "").strip()
            client_secret = os.environ.get(_GOOGLE_CLIENT_SECRET_ENV, "").strip()
            if not client_id or not client_secret:
                return None
            scopes_raw = os.environ.get(_GOOGLE_SCOPES_ENV, "").strip()
            scopes = tuple(s.strip() for s in scopes_raw.split() if s.strip())
            return GoogleOAuthProvider(client_id=client_id, client_secret=client_secret, scopes=scopes)
        return None

    def _mcp_oauth_auth(self, spec: "McpServer") -> McpOAuthAuth:
        """The per-user token hook for one OAuth MCP server (phase 4.x)."""
        return McpOAuthAuth(manager=self.oauth_manager, mcp_server=spec.name)

    async def _oauth_notifier(self, telegram_user_id: int, chat_id: int, mcp_server: str, ok: bool) -> None:
        """Notify the user in Telegram after an OAuth outcome (same loop).

        Runs inside the application's event loop (the callback server is a task
        on it), so it can drive the bot directly. Never sends a token, code,
        secret, or the callback URL — only the fixed, secret-free outcome text.
        A send failure is logged and swallowed (the callback still succeeded).
        """
        if self.application is None or self.application.bot is None:
            return
        if ok:
            text = f"✓ **{mcp_server}** connected.\n\nYour account is now available to the Agent."
        else:
            text = f"✗ **{mcp_server}** authorization was not completed."
        try:
            await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except BadRequest:
            try:
                await self.application.bot.send_message(chat_id=chat_id, text=text.replace("**", ""))
            except TelegramError:
                logger.warning("oauth notifier send failed", extra={"server": mcp_server})
        except TelegramError:
            logger.warning("oauth notifier send failed", extra={"server": mcp_server})

    # PTB lifecycle hooks (run inside the application's own event loop) ------
    async def _post_init(self, application) -> None:
        await init_db(self.engine)
        # Phase 4: connect + discover the configured remote MCP servers, then
        # register their tools into the *same* registry (after the built-ins).
        # This is best-effort by construction — ``start`` never raises, and a
        # failed server is simply marked unavailable — so an unreachable
        # endpoint can never stop the bot from starting. The newly added MCP
        # tools are picked up automatically: the tool loop re-resolves every
        # call and re-derives the advertised schema from ``registry.names()`` on
        # each message, and (when a permission file is configured) the
        # file-backed policy hot-reloads, so a pinned permission for a namespaced
        # name is honoured without a restart.
        mcp_tool_count = 0
        if self.mcp_manager is not None and self.registry is not None:
            await self.mcp_manager.start(existing_names=self.registry.names())
            discovered = self.mcp_manager.tools()
            if discovered:
                self.registry.add(*discovered)
                mcp_tool_count = len(discovered)
        # Phase 5.1: register the read-only infrastructure tools, after the
        # built-ins and MCP. This is a startup, collision-checked registration:
        # the infra names (``infra_<target>__<obs>``) are disjoint from the
        # built-ins and the MCP ``mcp_`` namespace, so a collision can only come
        # from a target name colliding with an already-registered name. A
        # duplicate is a startup ConfigError — the names are operator-chosen and
        # non-secret, so echoing one in the error is safe (never the
        # host/path/key, which are not in the tool name). No SSH connection is
        # opened here; a tool is reached only when it is called and passes the gate.
        infra_tool_count = 0
        if self.registry is not None and self.infra_tools:
            try:
                self.registry.add(*self.infra_tools)
                infra_tool_count = len(self.infra_tools)
            except ValueError as exc:
                raise ConfigError(f"cannot register infrastructure tools: {exc}") from exc
        # Phase 4.x: seed/sync the dedicated MCP-permissions file to the current
        # tool set (backend → file). New tools appear unfilled (default), entries
        # the operator filled in are preserved, and unfilled entries for tools
        # that no longer exist are pruned. Runs only when a file is configured;
        # a failure here is logged and never blocks boot (config-load already
        # validated a pre-existing file — this is a race guard, and the file is
        # hot-reloaded on read).
        if self.config.mcp_permissions_file is not None and self.mcp_manager is not None:
            try:
                reconcile_permissions_file(
                    self.config.mcp_permissions_file, [t.name for t in self.mcp_manager.tools()]
                )
            except Exception as exc:
                # A seed failure never blocks boot; the file is hot-reloaded on
                # read, so a bad write just means the current run keeps the
                # last-known permissions. Log only the path + exception class —
                # never the message (atomic_write already logs I/O failures).
                logger.error(
                    "failed to seed MCP permissions file",
                    extra={"path": str(self.config.mcp_permissions_file), "error": type(exc).__name__},
                )
        # Phase 4.x: start the minimal OAuth callback server (only when OAuth is
        # configured). It runs as a task on *this* loop, so the callback handler
        # and the Telegram notifier share the polling bot's loop. A failure to
        # bind (e.g. the port is taken) never stops the bot — it is logged and
        # OAuth degrades to "unavailable".
        if self.oauth_manager is not None:
            self.oauth_callback_server = build_oauth_callback_server(
                self.oauth_manager, port=self.config.oauth_callback_port
            )
            await self.oauth_callback_server.start()
        logger.info(
            "agent backend initialised",
            extra={
                "model": self.config.openai_model,
                "allowed_users": sorted(self.config.allowed_user_ids),
                "tools_enabled": self.config.enable_tools,
                "tools": self.registry.names() if self.registry else [],
                "mcp_tools": mcp_tool_count,
                "infra_tools": infra_tool_count,
            },
        )

    async def _post_shutdown(self, application) -> None:
        logger.info("shutting down agent backend")
        # Cancel any outstanding approvals first so a turn blocked on a human
        # decision resolves (expired) instead of hanging the shutdown.
        if self.approval_broker is not None:
            await self.approval_broker.shutdown()
        # Stop the OAuth callback listener before the MCP sessions (idempotent,
        # never raises) — an in-flight callback after this is rejected by the
        # manager as invalid/expired state, not by a dead loop.
        if self.oauth_callback_server is not None:
            await self.oauth_callback_server.stop()
        # Close the MCP sessions (and their HTTP transports/clients) before the
        # LLM client and engine — ``close`` is idempotent and never raises.
        if self.mcp_manager is not None:
            await self.mcp_manager.close()
        try:
            await self.llm.aclose()
        finally:
            await self.engine.dispose()

    def run(self) -> None:
        """Start long polling and block until the process is stopped (Ctrl+C)."""
        logger.info("starting telegram long polling")
        self.application.run_polling(drop_pending_updates=True)


def main() -> None:
    """Synchronous entrypoint (used by the console script and ``-m``).

    ``load_config`` may raise :class:`ConfigError` for missing secrets; that is
    a configuration problem, not a crash, so we print a clean message.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Hint: cp .env.example .env and fill in the values, then re-run.", file=sys.stderr)
        sys.exit(2)

    configure_logging(config.log_level, color=config.log_color)
    backend = AgentBackend(config)
    try:
        backend.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
