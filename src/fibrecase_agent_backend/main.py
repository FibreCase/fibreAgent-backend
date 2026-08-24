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
import sys

from .agent.service import AgentService
from .config import Config, ConfigError, load_config
from .database.repository import ConversationRepository
from .database.session import create_engine, create_session_factory, init_db
from .llm.client import OpenAIClient
from .logging_setup import configure_logging
from .telegram.bot import build_application

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
        application = build_application(config, self.service, self.repository)
        application.post_init = self._post_init
        application.post_shutdown = self._post_shutdown
        self.application = application

    # PTB lifecycle hooks (run inside the application's own event loop) ------
    async def _post_init(self, application) -> None:
        await init_db(self.engine)
        logger.info(
            "agent backend initialised",
            extra={"model": self.config.openai_model, "allowed_users": sorted(self.config.allowed_user_ids)},
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
