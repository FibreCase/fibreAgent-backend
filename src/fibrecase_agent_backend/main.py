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
from .database.repository import ConversationRepository
from .database.session import create_engine, create_session_factory, init_db
from .llm.client import OpenAIClient
from .logging_setup import configure_logging
from .telegram.bot import build_application, compose_startup_hooks, register_command_menu
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
        )
        application = build_application(config, self.service, self.repository)
        # Chain the Telegram adapter's command-menu registration with our own
        # DB init into a single post_init (both run inside the app's loop).
        application.post_init = compose_startup_hooks(register_command_menu, self._post_init)
        application.post_shutdown = self._post_shutdown
        self.application = application

    # PTB lifecycle hooks (run inside the application's own event loop) ------
    async def _post_init(self, application) -> None:
        await init_db(self.engine)
        logger.info(
            "agent backend initialised",
            extra={
                "model": self.config.openai_model,
                "allowed_users": sorted(self.config.allowed_user_ids),
                "tools_enabled": self.config.enable_tools,
                "tools": self.registry.names() if self.registry else [],
            },
        )

    async def _post_shutdown(self, application) -> None:
        logger.info("shutting down agent backend")
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

    configure_logging(config.log_level)
    backend = AgentBackend(config)
    try:
        backend.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
