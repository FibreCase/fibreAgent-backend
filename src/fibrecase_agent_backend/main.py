"""Application entrypoint and composition root.

Wires the four low-coupling pieces together and runs the long-polling bot:

    config ──┐
    database ─┼─▶ AgentService ─▶ Telegram Application (long polling)
    llm ──────┘

This module is the *only* place that constructs the concrete LLM client,
engine, repository and service; everything else is passed down.
"""

from __future__ import annotations

import logging

from .agent.service import AgentService
from .config import Config, load_config
from .database.repository import ConversationRepository
from .database.session import create_engine, create_session_factory, init_db
from .llm.client import OpenAIClient
from .logging_setup import configure_logging
from .telegram.bot import build_application

logger = logging.getLogger("main")


class AgentBackend:
    """Owns the runtime objects and their lifecycle (startup/shutdown)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.engine = create_engine(config.database_url)
        self.session_factory = create_session_factory(self.engine)
        self.repository = ConversationRepository(self.session_factory)
        self.llm = OpenAIClient(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key,
            model=config.openai_model,
            timeout=config.openai_timeout,
        )
        self.service = AgentService(
            self.repository,
            self.llm,
            system_prompt=config.system_prompt,
            max_context_messages=config.max_context_messages,
        )
        self.application = build_application(config, self.service, self.repository)

    async def init(self) -> None:
        await init_db(self.engine)
        logger.info(
            "agent backend initialised",
            extra={"model": self.config.openai_model, "allowed_users": sorted(self.config.allowed_user_ids)},
        )

    async def run(self) -> None:
        """Start long polling and block until the process is stopped.

        ``run_polling`` owns the PTB lifecycle (initialize → start → poll →
        stop → shutdown). We only add our own cleanup for the objects PTB
        does not manage (the LLM client and the DB engine).
        """
        try:
            await self.application.run_polling(drop_pending_updates=True)
            logger.info("telegram long polling stopped")
        finally:
            logger.info("shutting down agent backend")
            await self.llm.aclose()
            await self.engine.dispose()


async def async_main() -> None:
    config = load_config()
    configure_logging(config.log_level)

    backend = AgentBackend(config)
    await backend.init()
    await backend.run()


def main() -> None:
    """Synchronous entrypoint (used by the console script and ``-m``)."""
    import asyncio

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
