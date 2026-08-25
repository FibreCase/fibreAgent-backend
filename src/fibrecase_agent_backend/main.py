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
import sys

from .agent.service import AgentService
from .attachments import AttachmentStore
from .config import Config, ConfigError, load_config
from .database.audit import RepositoryToolAuditor
from .database.repository import ConversationRepository
from .database.session import create_engine, create_session_factory, init_db
from .llm.client import OpenAIClient
from .logging_setup import configure_logging
from .mcp import McpManager
from .telegram.approval import TelegramApprovalBroker
from .telegram.bot import build_application, compose_startup_hooks, register_command_menu
from .tools import build_policy
from .tools.builtin import build_default_tools

logger = logging.getLogger("main")


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
        # the service degrades to the phase-one single-completion path.
        registry = build_default_tools() if config.enable_tools else None
        self.registry = registry
        # Phase 3: the tool-security runtime. All three are built *only* when
        # tools are enabled — with tools off there is nothing to advertise,
        # approve, or audit, so the service stays on the bare phase-one path.
        #   * policy — resolves each tool to allow/ask/deny (config overrides
        #     take priority over a tool's declared default).
        #   * auditor — the concrete SQLite-backed auditor (fail-closed on the
        #     pre-execution write).
        #   * broker — the in-memory Telegram Approve/Deny provider, also used
        #     by the adapter for the callback + /tool_audit.
        policy = build_policy(config.tool_permission_overrides, registry=registry) if registry else None
        auditor = RepositoryToolAuditor(self.repository) if registry else None
        broker = TelegramApprovalBroker(self.repository) if registry else None
        self.approval_broker = broker
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
            )
            if (config.enable_tools and config.mcp_servers)
            else None
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
            config, self.service, self.repository, approval_broker=broker, mcp_manager=self.mcp_manager
        )
        # Chain the Telegram adapter's command-menu registration with our own
        # DB init into a single post_init (both run inside the app's loop).
        application.post_init = compose_startup_hooks(register_command_menu, self._post_init)
        application.post_shutdown = self._post_shutdown
        self.application = application

    # PTB lifecycle hooks (run inside the application's own event loop) ------
    async def _post_init(self, application) -> None:
        await init_db(self.engine)
        # Phase 4: connect + discover the configured remote MCP servers, then
        # register their tools into the *same* registry (after the built-ins).
        # This is best-effort by construction — ``start`` never raises, and a
        # failed server is simply marked unavailable — so an unreachable
        # endpoint can never stop the bot from starting. The policy was already
        # built; because the tool loop re-resolves every call and re-derives the
        # advertised schema from ``registry.names()`` on each message, the newly
        # added MCP tools are picked up automatically (they default to ``ask``,
        # and any ``TOOL_PERMISSION_OVERRIDES`` entry for their namespaced name
        # is honoured).
        mcp_tool_count = 0
        if self.mcp_manager is not None and self.registry is not None:
            await self.mcp_manager.start(existing_names=self.registry.names())
            discovered = self.mcp_manager.tools()
            if discovered:
                self.registry.add(*discovered)
                mcp_tool_count = len(discovered)
        logger.info(
            "agent backend initialised",
            extra={
                "model": self.config.openai_model,
                "allowed_users": sorted(self.config.allowed_user_ids),
                "tools_enabled": self.config.enable_tools,
                "tools": self.registry.names() if self.registry else [],
                "mcp_tools": mcp_tool_count,
            },
        )

    async def _post_shutdown(self, application) -> None:
        logger.info("shutting down agent backend")
        # Cancel any outstanding approvals first so a turn blocked on a human
        # decision resolves (expired) instead of hanging the shutdown.
        if self.approval_broker is not None:
            await self.approval_broker.shutdown()
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
