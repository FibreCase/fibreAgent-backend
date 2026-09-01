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

import asyncio
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .agent.service import AgentError, AgentService, _user_safe_for
from .attachments import AttachmentStore
from .automation import Scheduler
from .config import Config, ConfigError, McpServer, load_config
from .database.audit import RepositoryToolAuditor
from .database.models import qq_chat_id, schedule_chat_id
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
from .qq import build_qq_client, deliver_qq_markdown
from .qq.approval import QQApprovalBroker, QQScopedApprovalRouter
from .telegram.approval import TelegramApprovalBroker
from .telegram.bot import build_application, compose_startup_hooks, deliver_markdown, register_command_menu
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

# Phase 10 (multi-channel): the *only* place that knows the QQ client secret's
# env-var name. Like the Google client secret, the value is read here (and only
# here) at QQ-client build time — it is never stored on the frozen Config and
# never logged. The app id (``QQ_APP_ID``) is non-secret and *is* stored on
# config (it is logged at startup for attribution).
_QQ_CLIENT_SECRET_ENV = "QQ_CLIENT_SECRET"


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
            reasoning_effort=config.reasoning_effort,
        )
        # The tool registry is built only when tools are enabled; when disabled
        # the service degrades to the phase-one single-completion path. The two
        # opt-in capabilities (the ``exec`` shell tool and the ``file`` toolset)
        # are added only when their flags are on (a default deployment stays
        # subprocess-free and touch-free).
        if config.enable_tools:
            registry = build_default_tools(
                enable_exec=config.enable_exec_tool,
                max_exec_output_chars=config.max_exec_tool_result_chars,
                exec_workdir=config.exec_workdir,
                exec_policy_deny_patterns=config.exec_policy_deny_patterns,
                enable_file=config.enable_file_tool,
                file_workdir=config.file_workdir,
                max_file_string_chars=config.max_file_string_chars,
                max_file_read_chars=config.max_file_read_chars,
                max_file_list_entries=config.max_file_list_entries,
                max_file_content_chars=config.max_file_content_chars,
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
        # Phase 10 (multi-channel): the QQ-side approval transport. Built whenever
        # tools are on (it is cheap and in-memory, and binds to the QQ client
        # later in ``_post_init`` only if the QQ channel is actually configured).
        # It is *not* wired into the shared service directly — the service takes
        # the scope-routing provider below, which forwards ``qq:…``-scoped turns
        # to this broker and everything else to the Telegram broker.
        self._qq_approval_broker = QQApprovalBroker() if registry else None
        # The single channel-agnostic ``approval_provider`` the shared service
        # holds: routes each ``ask`` to the broker matching the turn's scope
        # prefix (``qq:`` → QQ button card, else → Telegram inline callback).
        approval_provider = (
            QQScopedApprovalRouter(broker, self._qq_approval_broker) if registry else None
        )
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
            approval_provider=approval_provider,
            auditor=auditor,
            tool_timeout_seconds=config.tool_timeout_seconds,
            tool_approval_timeout_seconds=config.tool_approval_timeout_seconds,
        )
        # Phase 9 (Automation, first slice): time-triggered scheduling. The
        # :class:`Scheduler` is a pure, channel-agnostic background task that only
        # fires an injected ``runner`` coroutine; the Telegram-specific runner
        # (dedicated fresh conversation → ``process_message`` → formatted
        # notification → cleanup) is the ``_run_schedule`` closure below. It is
        # built **only** when schedules are configured — with an empty
        # ``SCHEDULES`` there is no automation, so the scheduler does not exist
        # and no background task is ever started (isomorphic to empty
        # ``MCP_SERVERS`` / ``INFRA_SSH_TARGETS``). The wall clock is evaluated
        # in ``SCHEDULE_TIMEZONE`` (or the process-local tz when unset). The
        # scheduler is *started* in ``_post_init`` (after ``init_db`` and MCP
        # discovery) and *stopped* in ``_post_shutdown`` (after the approval
        # broker drains, before the LLM/DB close).
        self._schedule_tz = ZoneInfo(config.schedule_timezone) if config.schedule_timezone else datetime.now().astimezone().tzinfo
        self.scheduler: Scheduler | None = (
            Scheduler(
                config.schedules,
                self._schedule_tz,
                self._run_schedule,
                now_fn=lambda: datetime.now(self._schedule_tz),
            )
            if config.schedules
            else None
        )
        # Phase 10 (multi-channel): QQ. The client is built and started in
        # ``_post_init`` (it must be constructed on the *running* PTB event loop,
        # because ``botpy.Client`` grabs the loop at construction and its
        # ``start()`` is driven as a task on that loop) — and **only** when the
        # channel is configured (an app id plus a client secret in the env). With
        # neither present, these stay ``None`` and no QQ client or websocket is
        # ever created (isomorphic to the other optional providers).
        self._qq_client = None
        self._qq_task: "asyncio.Task | None" = None
        # Task *ids* already pending on the loop *before* the QQ subsystem
        # starts. The shutdown teardown (:meth:`_qq_shutdown_tasks`) cancels
        # every QQ-spawned background task (the SDK's connection-runner, websocket
        # and heartbeat coroutines all live on our shared PTB loop, and
        # ``botpy``'s ``Client.close()`` never cancels any of them) by diffing
        # the loop's current task set against this baseline — any task whose id is
        # *not* in it was created after the baseline and is attributed to the QQ
        # subsystem. Storing ids (not task objects) keeps the snapshot small and
        # avoids holding references. Captured in ``_post_init`` immediately before
        # the task is started, so the snapshot is taken on the running loop.
        self._qq_pending_before: frozenset = frozenset()
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

    # Phase 9 (Automation): the runner the scheduler fires for each due schedule.
    # Runs as a task on the PTB event loop (created by the scheduler), so it can
    # drive ``self.service``, ``self.application.bot`` (Telegram) and
    # ``self._qq_client`` (QQ) directly.
    #
    # Execution model:
    #   1. prepare a **dedicated, fresh** venue: a synthetic ``telegram_chat_id``
    #      in the reserved range (``schedule_chat_id(spec.name)``) reuses the
    #      ``conversations`` table — no new table. ``reset_conversation`` yields an
    #      empty history and self-heals a crashed run's leftover row. The row's
    #      ``telegram_user_id`` is the identity's principal (the Telegram
    #      ``user_id`` for a ``telegram`` identity; the ``qq_chat_id`` of the
    #      openid for a ``qq`` identity — mirroring the interactive QQ path, which
    #      stores the synthetic id in both columns).
    #   2. run the fixed prompt through the *same* channel-agnostic
    #      ``AgentService.process_message()`` the interactive path uses — with
    #      ``spec.memory_scope()`` (so long-term memory retrieval + the right
    #      principal) and ``spec.approval_delivery_chat_id()`` (a ``telegram`` run
    #      sends an in-run approval card to its receiver's chat; a ``qq`` run
    #      passes ``None`` — the QQ broker routes by the ``qq:`` scope prefix).
    #      The agent runs **once** per fire.
    #   3. deliver a **formatted notification** (task name + result) to **every**
    #      channel named in ``spec.receiver`` via :meth:`_deliver_schedule_notification`;
    #      a failure sends a fixed, safe short notice to the same channels.
    #   4. always (``finally``) delete the dedicated venue, leaving no trace.
    #
    # A scheduled run never enters ``bot_data[_IN_FLIGHT]`` (that slot is the
    # interactive handler's), so ``/stop`` does not affect it — it converges via
    # its venue's per-conversation lock + tool/LLM timeouts + the scheduler's
    # per-task single-flight.
    async def _run_schedule(self, spec) -> None:
        synthetic_chat_id = schedule_chat_id(spec.name)
        # The dedicated row's principal is the identity's owner: the Telegram
        # user_id, or the QQ openid's synthetic chat id (a qq: conversation row
        # stores the synthetic id in the telegram_user_id column, like the
        # interactive path does).
        row_user_id = (
            spec.telegram.user_id if spec.identity == "telegram" else qq_chat_id(spec.qq.user_openid)
        )
        conversation = await self.repository.reset_conversation(synthetic_chat_id, row_user_id)
        try:
            reply = await self.service.process_message(
                conversation.id,
                spec.prompt,
                memory_scope=spec.memory_scope(),
                delivery_chat_id=spec.approval_delivery_chat_id(),
            )
            if reply:
                text = f"⏰ **定时任务：{spec.name}**\n\n{reply}"
                await self._deliver_schedule_notification(spec, text)
            # An empty reply carries nothing to report: no notification.
        except AgentError as exc:
            # A fixed, safe failure notice: the task name + a category-mapped
            # Chinese phrase. Never the prompt, exception text, or a stack —
            # the owner should know the 7am check failed rather than it vanishing.
            notice = f"⏰ **定时任务：{spec.name}**\n\n{exc.user_safe}"
            await self._deliver_schedule_notification(spec, notice)
        except Exception:
            # Fault isolation is already guaranteed by the scheduler, but the
            # runner must not leak venue cleanup: log by name + class only.
            logger.warning(
                "scheduled run failed",
                extra={"schedule": spec.name},
                exc_info=True,
            )
        finally:
            try:
                await self.repository.delete_conversation(conversation.id)
            except Exception:
                # A failed cleanup is left to the next run's self-heal / the
                # startup sweep. Safe log (synthetic id is safe to log).
                logger.warning(
                    "scheduled run venue cleanup failed",
                    extra={"schedule": spec.name, "conversation_id": conversation.id},
                )

    async def _deliver_schedule_notification(self, spec, text: str) -> None:
        """Best-effort delivery of a schedule's notification to every configured
        receiver.

        Delivers to each channel present in ``spec.receiver``; a failure on one
        channel (a Telegram send error, a QQ send error, or a QQ receiver on a
        Telegram-only deployment where ``self._qq_client`` is ``None``) is logged
        *by schedule name only* and never blocks the other channel or raises —
        the venue cleanup in :meth:`_run_schedule`'s ``finally`` must still run.
        The QQ ``user_openid`` is a delivery target and never appears in a log
        line.
        """
        if spec.telegram is not None:
            try:
                await deliver_markdown(self.application.bot, spec.telegram.chat_id, text)
            except TelegramError:
                logger.warning(
                    "scheduled run notification failed (telegram)",
                    extra={"schedule": spec.name},
                )
        if spec.qq is not None:
            if self._qq_client is None:
                # Configured a QQ receiver but the channel is not running (no
                # client): skip with a clear warning, mirroring the QQ
                # channel's own degradation behaviour.
                logger.warning(
                    "scheduled run QQ delivery skipped (channel not running)",
                    extra={"schedule": spec.name},
                )
            else:
                try:
                    await deliver_qq_markdown(self._qq_client, spec.qq.user_openid, text)
                except Exception:
                    logger.warning(
                        "scheduled run notification failed (qq)",
                        extra={"schedule": spec.name},
                    )

    # Phase 10 (multi-channel): QQ. The ``botpy`` client is built and started
    # here, on the running PTB loop, and driven as a task — the SDK's own
    # ``run()`` is a *blocking* wrapper around ``async with self: await
    # self.start(...)`` that owns its own loop via ``run_until_complete``, so we
    # cannot call it; instead we run the same body as a task on the loop PTB
    # already runs (mirrors the OAuth callback server's task-on-the-loop pattern).
    # The client is constructed *here* (not in ``__init__``) because
    # ``botpy.Client`` calls ``asyncio.get_event_loop()`` at construction, so it
    # must see the running loop. ``async with client`` calls the SDK's private
    # ``_async_setup_hook`` (binds ``client.loop`` + the ready event to this loop);
    # ``start`` then logs in and loops until ``close()`` breaks it (``_post_shutdown``).
    # A QQ login failure must never stop the Telegram bot from starting: ``_qq_run``
    # swallows and logs the error (by class only — never the app id/secret), leaving
    # the Telegram channel fully up.
    async def _qq_run(self, client, secret: str) -> None:
        try:
            async with client:
                await client.start(self.config.qq_app_id, secret)
        except Exception:
            logger.error("qq client start failed", exc_info=True)

    def _qq_task_done(self, task: "asyncio.Task") -> None:
        # Surface a QQ task that ended in an *exception* (not a clean ``close()``)
        # so it is never silently dropped. ``CancelledError`` / ``Exception`` are
        # already handled inside ``_qq_run`` (the clean path raises nothing); a
        # ``BaseException`` (e.g. SystemExit) here is logged by class only.
        if not task.cancelled() and task.exception() is not None:
            logger.error("qq task ended with exception", exc_info=True)

    async def _qq_shutdown_tasks(self) -> None:
        """Cancel every background task the QQ subsystem spawned, then drain.

        ``botpy``'s ``Client.close()`` only closes the HTTP client — it never
        cancels the websocket / connection / heartbeat coroutines it spawned on
        *our shared PTB loop* (the ``ConnectionSession._runner``, the
        ``BotWebSocket`` receive loop and the ``_send_heart`` heartbeat). The
        outer ``_qq_task`` (``_qq_run``) is suspended inside ``asyncio.wait`` on
        those, and ``asyncio.wait`` swallows the cancellation — so cancelling the
        outer task alone leaves the inner coroutines pending, and the loop's
        teardown later destroys them mid-``aiohttp``-teardown, surfacing as
        ``Task was destroyed but it is pending`` and
        ``RuntimeError: coroutine ignored GeneratorExit``.

        To cancel them we diff the loop's current task set against
        :attr:`_qq_pending_before` (captured just before ``_qq_task`` started), so
        we target only QQ-created tasks without touching unrelated in-flight work
        (an in-progress Telegram approval callback, a scheduled run, the OAuth
        callback server) that happens to be pending on the same loop. The task
        currently running this teardown (the ``post_shutdown`` task, created at
        shutdown and hence *after* the baseline) is excluded explicitly — it is
        what ``gather`` awaits, so cancelling it would deadlock the drain.

        Best-effort: a teardown failure is logged (by class) and swallowed so it
        can never block the LLM/DB close.
        """
        try:
            current_task = asyncio.current_task()
            current = asyncio.all_tasks()
            qq_tasks = [
                t
                for t in current
                if id(t) not in self._qq_pending_before and t is not current_task
            ]
            for t in qq_tasks:
                t.cancel()
            if qq_tasks:
                await asyncio.gather(*qq_tasks, return_exceptions=True)
        except Exception:
            logger.error("qq task teardown failed", exc_info=True)

    # PTB lifecycle hooks (run inside the application's own event loop) ------
    async def _post_init(self, application) -> None:
        await init_db(self.engine)
        # Phase 9 (Automation): sweep away any orphaned dedicated (scheduled-run)
        # conversations left in the reserved range by a killed run or a schedule
        # since removed from config. Runs *after* ``init_db`` (schema exists) and
        # *before* the scheduler starts (so the sweep and the first tick are
        # ordered). A no-op (returns 0) when there is nothing to clear — the
        # empty-``SCHEDULES`` case. Best-effort: a failure is logged and never
        # blocks boot (the per-run self-heal + next sweep cover it).
        try:
            cleared = await self.repository.clear_ephemeral_conversations()
            if cleared:
                logger.info("startup sweep cleared orphaned schedule venues", extra={"count": cleared})
        except Exception:
            logger.error("startup sweep of schedule venues failed", exc_info=True)
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
        # Phase 9 (Automation): start the scheduler, last, once the DB is ready
        # and MCP/infra discovery has registered the tools the runner's
        # ``process_message`` may dispatch. ``start`` is idempotent and never
        # raises; it recomputes every schedule's next fire from *now* (no
        # catch-up). A broken scheduler cannot crash the bot (mirrors the OAuth
        # callback server's lifecycle contract).
        if self.scheduler is not None:
            self.scheduler.start()
        # Phase 10 (multi-channel): start the QQ client, last, as a task on this
        # loop. Built *here* so the SDK binds to the running loop; started only
        # when configured (an app id **and** a client secret in the env). There is
        # no allow-list — the channel is the owner's personal bot, so any QQ user
        # who can DM / @ it is served (access is bounded by the app id + a QQ
        # account). The secret is read from the environment here and only
        # here (in-memory; never on config, never logged). A startup failure is
        # contained inside ``_qq_run`` so the Telegram bot keeps running.
        if self.config.qq_app_id:
            secret = os.environ.get(_QQ_CLIENT_SECRET_ENV, "").strip()
            if secret:
                self._qq_client = build_qq_client(
                    self.service,
                    self.repository,
                    self.config,
                    self.mcp_manager,
                    approval_broker=self._qq_approval_broker,
                )
                # Snapshot the ids of the loop's tasks *before* the QQ subsystem
                # starts, so shutdown can identify the SDK's own background tasks
                # (the connection-runner, websocket and heartbeat coroutines the
                # SDK spawns on this loop) as "created after" and cancel them.
                self._qq_pending_before = frozenset(
                    id(t) for t in asyncio.all_tasks()
                )
                self._qq_task = asyncio.create_task(self._qq_run(self._qq_client, secret))
                self._qq_task.add_done_callback(self._qq_task_done)
            else:
                # An app id with no client secret cannot log in. This is a
                # misconfiguration (config-load can't catch it — the secret is
                # deliberately not on config); degrade the channel to off with a
                # clear, secret-free warning rather than half-starting it.
                logger.warning(
                    "qq configured but %s is not set; starting without the QQ channel",
                    _QQ_CLIENT_SECRET_ENV,
                )
        logger.info(
            "agent backend initialised",
            extra={
                "model": self.config.openai_model,
                "allowed_users": sorted(self.config.allowed_user_ids),
                "tools_enabled": self.config.enable_tools,
                "tools": self.registry.names() if self.registry else [],
                "mcp_tools": mcp_tool_count,
                "infra_tools": infra_tool_count,
                "schedules": len(self.config.schedules),
                "qq_enabled": self._qq_task is not None,
            },
        )

    async def _post_shutdown(self, application) -> None:
        logger.info("shutting down agent backend")
        # Cancel any outstanding approvals first so a turn blocked on a human
        # decision resolves (expired) instead of hanging the shutdown. Both the
        # Telegram broker (inline callbacks) and the QQ broker (button cards) are
        # drained here, before the scheduler stops and before the QQ client
        # closes — so an in-flight QQ turn awaiting an approval is unblocked
        # first, not abandoned with its websocket dropped.
        if self.approval_broker is not None:
            await self.approval_broker.shutdown()
        if self._qq_approval_broker is not None:
            await self._qq_approval_broker.shutdown()
        # Phase 9 (Automation): stop the scheduler *after* the approval broker
        # (so a scheduled turn awaiting an approval is first unblocked) and
        # *before* the LLM client / engine are closed (so no scheduled turn is
        # abandoned mid-call). ``stop`` is idempotent, never raises, and lets any
        # in-flight run finish (bounded) so its venue is cleaned up.
        if self.scheduler is not None:
            await self.scheduler.stop()
        # Phase 10 (multi-channel): stop the QQ client *after* the approval
        # broker and scheduler (so any in-flight QQ turn is first unblocked) and
        # *before* the LLM client / engine close (so no QQ turn is abandoned
        # mid-call). ``close()`` only closes the HTTP client — the websocket,
        # connection-runner and heartbeat coroutines the SDK spawned on this loop
        # are cancelled by ``_qq_shutdown_tasks`` (which also drains the outer
        # ``_qq_task``), so nothing is left pending for the loop teardown to
        # destroy. Both are best-effort (swallowed) so a QQ teardown failure
        # can't stop the DB/LLM from closing.
        if self._qq_client is not None:
            try:
                await self._qq_client.close()
            except Exception:
                logger.error("qq client close failed", exc_info=True)
            await self._qq_shutdown_tasks()
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
