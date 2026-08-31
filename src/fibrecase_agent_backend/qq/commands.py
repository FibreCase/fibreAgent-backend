"""QQ (C2C) slash-commands — channel-agnostic command logic, botpy-free.

This module holds the *logic* of the QQ bot's slash-commands: it reuses the same
channel-agnostic :class:`~..agent.service.AgentService` methods the Telegram
adapter uses and produces the same plain-text replies. It is deliberately
**independent** of :mod:`..telegram` (a channel must not import another channel)
and of ``botpy`` (this package's only botpy imports live in
:mod:`.bot`), so it can be unit-tested with lightweight fakes and no live client.

The transport half — parsing the leading ``/token`` off an incoming C2C message,
deciding *which* command ran, delivering the returned string over the QQ
websocket, and (for ``/stop``) the QQ-local in-flight registry — lives in
:mod:`.bot`. Everything here returns the reply string (or ``None`` for "send
nothing") and never sends anything itself.

Privacy mirrors the Telegram layer: none of these commands exposes a key, token,
endpoint, host, mount path, service name, command, ``prompt``/``chat_id``/
``user_id``, or the raw ``user_openid``. They operate on the opaque
``memory_scope`` string and the (synthetic) integer conversation id only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import __version__
from ..agent.service import AgentError, AgentService, _user_safe_for
from ..automation.cron import CronError, parse_cron
from ..config import Config
from ..database.repository import ConversationRepository
from ..infrastructure import local_tool_name

logger = logging.getLogger("qq")

# The QQ command set: the Telegram core set + read-only ``/mcp_status``, minus
# ``/start`` (no QQ concept) and ``/mcp`` / ``/mcp auth`` (the OAuth login is
# Telegram-bound — it returns an inline login *button* and a proactive
# completion notification, both of which QQ cannot reliably deliver). Single
# source of truth for both ``cmd_help`` and the native command panel.
# ``(command, short_description)``.
_QQ_COMMANDS: list[tuple[str, str]] = [
    ("new", "Start a new conversation"),
    ("stop", "Stop the current reply"),
    ("help", "Show this help"),
    ("status", "Show run status"),
    ("context", "Show context budget"),
    ("remember", "Save a long-term memory"),
    ("memories", "List your memories"),
    ("forget", "Forget a memory or all"),
    ("tool_audit", "Show tool audit log"),
    ("mcp_status", "Show remote MCP tool status"),
    ("infra_status", "Show configured infra targets"),
    ("schedule_status", "Show configured schedules"),
]

# The native command panel caps each item's ``name`` at 14 characters, and the
# whole panel at 20 items. Commands longer than the name cap are still
# *dispatched* (typeable by hand) — they just cannot appear in the panel. This
# is what keeps ``/schedule_status`` (16 chars incl. the slash) out of the panel
# while every shorter command stays in it. Encoded as a filter, not a hard-coded
# drop, so adding a command never requires editing the panel logic.
_PANEL_NAME_MAX = 14
_PANEL_ITEMS_MAX = 20


def build_c2c_panel_items(commands: list[tuple[str, str]] = _QQ_COMMANDS) -> list[dict]:
    """Build the ``items`` list for ``POST /v2/panels`` from the command table.

    Pure (no I/O, no botpy): each command becomes a ``command``-type panel item
    ``{"type": "command", "name": "/<cmd>", "desc": <description>}`` whose click
    fills the ``/<cmd>`` into the input box. Items are filtered to those whose
    ``name`` fits the 14-char panel cap (``/schedule_status`` drops out here)
    and the panel to at most 20 items (the API maximum), in the command table's
    order. Returns ``[]`` if nothing qualifies.
    """
    items: list[dict] = []
    for name, desc in commands:
        label = f"/{name}"
        if len(label) > _PANEL_NAME_MAX:
            continue
        items.append({"type": "command", "name": label, "desc": desc})
        if len(items) >= _PANEL_ITEMS_MAX:
            break
    return items


def _utc_stamp(dt) -> str:
    """A short UTC timestamp for display (time only — never memory content)."""
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # pragma: no cover - defensive (dt is always tz-aware)
        return str(dt)


async def cmd_new(service: AgentService, conversation_id: int) -> str:
    """/new — start a fresh conversation for this chat (drops all its history)."""
    await service.reset(conversation_id, conversation_id)
    return "**New conversation started** (history cleared)."


async def cmd_stop(in_flight: dict[int, "object"], conversation_id: int) -> str | None:
    """/stop — interrupt this chat's in-flight reply.

    Pops the in-flight task registered for ``conversation_id`` (by
    :mod:`.bot`, which runs each C2C turn as its own ``asyncio.Task``) and, if
    one is running, cancels it — the cancelled turn posts its own "已停止" notice.
    Returns ``None`` (send nothing) in that case, or the "nothing to stop" reply
    when there is no live task. Cancelling a *generation* is all this does — it
    does not drop the conversation or memory (that is ``/new``).
    """
    task = in_flight.pop(conversation_id, None)
    if task is None or task.done():
        return "**Nothing to stop.**"
    task.cancel()
    # Do not await the cancelled task here: it is unwinding in its own task and
    # posts its own notice. Awaiting it could deadlock on the very
    # per-conversation lock it is releasing.
    return None


async def cmd_help() -> str:
    """List the available commands (generated from ``_QQ_COMMANDS``)."""
    lines = ["**Available commands:**", ""]
    for cmd, desc in _QQ_COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines += ["", "Any other text message is sent to the agent."]
    return "\n".join(lines)


async def cmd_status(service: AgentService, repo: ConversationRepository, config: Config,
                     conversation_id: int) -> str:
    """/status — run status: version, model, and (if any) the current
    conversation and its message count."""
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        lines = [
            "**Agent Backend:**",
            "**Status:** OK",
            "",
            f"**Version:** {__version__}",
            f"**Model:** {config.openai_model}",
            "**Conversation:** (none yet — send a message)",
            "**Database:** OK",
        ]
    else:
        status = await service.conversation_status(conversation.id)
        lines = [
            "**Agent Backend:**",
            "**Status:** OK",
            "",
            f"**Version:** {__version__}",
            f"**Model:** {config.openai_model}",
            f"**Conversation:** {conversation.id}",
            f"**Messages:** {status['messages']}",
            "**Database:** OK",
        ]
    # None of the above exposes keys, tokens, or file paths.
    return "\n".join(lines)


async def cmd_context(service: AgentService, repo: ConversationRepository,
                      conversation_id: int) -> str:
    """/context — a read-only preview of how much stored history (and its images)
    would fit both the message cap and the estimated-token budget. It never
    reads an attachment blob (planning is metadata-only)."""
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        return "**No conversation yet** — send a message first."

    s = await service.context_status(conversation.id)
    free_tokens = s["budget"] - s["estimated_cost"]
    free_messages = s["cap"] - (s["history_messages"] + 1)
    images_downgraded = s["images_in_store"] - s["images_kept"]
    lines = [
        "**Context:**",
        f"**Conversation:** {s['conversation_id']}",
        "",
        f"**Message cap:** {s['cap']}",
        f"**Stored:** {s['stored_messages']} messages",
        f"**Kept this turn:** {s['history_messages']} (+1 current)",
        f"**Room left:** ~{free_messages} messages",
        "",
        f"**Estimated budget:** {s['budget']} units",
        f"**Used:** ~{s['estimated_cost']} units (system {s['system_cost']})",
        f"**Free:** ~{free_tokens} units",
        "",
        f"**History images kept:** {s['images_kept']} / {s['images_in_store']}"
        + (f" ({images_downgraded} downgraded to text)" if images_downgraded > 0 else ""),
        "",
        "(Conservative estimate, not exact tokens.)",
    ]
    # None of the above exposes message text, keys, tokens, digests, or paths.
    return "\n".join(lines)


async def cmd_remember(service: AgentService, memory_scope: str, args: str) -> str:
    """/remember <content> — save one explicit long-term memory."""
    record = await service.remember_memory(memory_scope, args)
    return f"**Memory saved.**\n**ID:** {record.id}\n\n{record.content}"


async def cmd_memories(service: AgentService, memory_scope: str) -> str:
    """/memories — list the caller's own memories."""
    records = await service.list_memories(memory_scope)
    if not records:
        return "**No memories saved yet.** Use /remember <text> to save one."

    lines = [f"**Your memories:** ({len(records)} total)", ""]
    for r in records:
        lines.append(f"**#{r.id}** (saved {_utc_stamp(r.created_at)})")
        lines.append(r.content)
        lines.append("")
    return "\n".join(lines).rstrip()


async def cmd_forget(service: AgentService, memory_scope: str, args: str) -> str:
    """/forget <id> — delete one memory; /forget all CONFIRM — delete all.

    ``/forget all`` without the exact ``CONFIRM`` token only shows the
    confirmation format and changes nothing. A foreign/missing id is reported
    exactly as a missing one (no existence leak).
    """
    tokens = args.split()
    if not tokens:
        raise AgentError("Usage: /forget <id> or /forget all CONFIRM", "memory_invalid")

    if tokens[0].lower() == "all":
        if len(tokens) >= 2 and tokens[1] == "CONFIRM":
            removed = await service.forget_all_memories(memory_scope)
            return f"**All memories cleared.** ({removed} deleted)"
        return _user_safe_for("memory_clear_confirmation")

    try:
        memory_id = int(tokens[0])
    except ValueError:
        raise AgentError("Usage: /forget <id> or /forget all CONFIRM", "memory_invalid")
    await service.forget_memory(memory_scope, memory_id)
    return f"**Memory deleted.** (ID: {memory_id})"


_TOOL_AUDIT_MAX_LIMIT = 50


async def cmd_tool_audit(service: AgentService, memory_scope: str, args: str) -> str:
    """/tool_audit [limit] — show the caller's own recent tool-audit events.

    Read-only and scope-isolated: it reads only the *current* principal's audit
    rows (by irreversible scope hash) and shows, per event, a timestamp, the
    event id, the tool name, the event type, a stable result code, and (where
    recorded) the latency. It never shows tool arguments, tool results, exception
    text, the raw scope/user id, or any secret.
    """
    arg = args.strip()
    limit = 20
    if arg:
        try:
            limit = max(1, min(_TOOL_AUDIT_MAX_LIMIT, int(arg)))
        except ValueError:
            return "Usage: /tool_audit [limit]  (limit is 1-50)"

    records = await service.list_tool_audit_events(memory_scope, limit)
    if not records:
        return "**No tool activity yet.** Tool calls will appear here as they run."

    lines = [f"**Tool audit:** (last {len(records)} events, most recent first)", ""]
    for r in records:
        line = f"**#{r.id}** {_utc_stamp(r.created_at)} — {r.tool_name} / {r.event_type}"
        if r.code:
            line += f" / {r.code}"
        if r.latency_ms is not None:
            line += f" / {r.latency_ms}ms"
        lines.append(line)
    lines += ["", "(Codes are stable, human-readable status tags — arguments and results are not shown.)"]
    # None of the above exposes tool args, results, exception text, raw scope,
    # the user id, or any secret.
    return "\n".join(lines)


async def cmd_mcp_status(config: Config, mcp_manager) -> str:
    """/mcp_status — show which configured remote MCP servers are up and how many
    tools each exposes.

    Read-only and non-mutating: it reads the in-memory ``McpManager`` state set
    at startup. It does **not** connect, refresh, re-discover, or call the LLM or
    any MCP server. The reply shows only each server's *name*, an
    ``available``/``unavailable`` flag, and its discovered-tool count, plus the
    total — it never exposes a URL, host, header, token, tool description or
    schema, server instructions, or a failure detail.
    """
    # No manager (no servers configured, or tools disabled) → MCP is disabled.
    if mcp_manager is None or not getattr(config, "enable_tools", True) or len(mcp_manager) == 0:
        return "**MCP:** disabled"

    lines = ["**Remote MCP servers:**", ""]
    for entry in mcp_manager.status():
        state = "available" if entry["available"] else "unavailable"
        lines.append(f"**{entry['name']}** — {state} ({entry['tool_count']} tools)")
    lines += ["", f"**Total MCP tools available:** {mcp_manager.total_tools}"]
    # None of the above exposes a URL, host, header, token, description, schema,
    # server instructions, or any failure detail.
    return "\n".join(lines)


async def cmd_infra_status(config: Config) -> str:
    """/infra_status — show which read-only infrastructure targets are configured.

    Read-only and non-mutating: it renders only the **configuration** — each
    target's *name* and its three fixed, argument-free, read-only tool names. It
    does **not** connect over SSH, refresh anything, probe reachability, or call
    the LLM or any target. The reply shows **only** the target name and the local
    tool names; it never exposes a host, port, username, key path, known_hosts
    path, mount path, service name, or any command — and it draws **no**
    conclusion about whether a target is reachable.
    """
    if not getattr(config, "enable_tools", True) or not config.infra_ssh_targets:
        return "**Infrastructure:** disabled"

    lines = ["**Infrastructure observation targets (read-only):**", ""]
    for target in config.infra_ssh_targets:
        tool_names = ", ".join(
            f"`{local_tool_name(target.name, obs)}`"
            for obs in ("host_status", "disk_status", "service_status")
        )
        lines.append(f"**{target.name}** — configured (3 tools, read-only): {tool_names}")
    lines += [
        "",
        f"**Total configured tools:** {len(config.infra_ssh_targets) * 3}",
        "",
        "(Configured only — this shows nothing about reachability; a status is "
        "read only when the corresponding tool is actually called.)",
    ]
    # None of the above exposes a host, port, username, key path, known_hosts
    # path, mount path, service name, or command — only the target name and the
    # (operator-named) local tool names.
    return "\n".join(lines)


async def cmd_schedule_status(config: Config) -> str:
    """/schedule_status — show the configured cron schedules and their next fire time.

    Read-only and non-mutating: it renders the startup-configured schedule list
    (name + cron expression) and, for each, the next fire time computed by the
    pure cron parser in ``SCHEDULE_TIMEZONE``. It does **not** trigger a run or
    call the LLM. The reply shows **only** each schedule's *name*, its *cron*
    expression, and its *next fire time* — never its ``prompt``, ``chat_id``, or
    ``user_id``. A schedule with no fire time in the window is shown as
    "never (untriggerable)".
    """
    if not config.schedules:
        return "**Schedules:** disabled (none configured)"

    tz = ZoneInfo(config.schedule_timezone) if config.schedule_timezone else datetime.now().astimezone().tzinfo
    now = datetime.now(tz)
    lines = ["**Scheduled tasks:**", ""]
    for spec in config.schedules:
        try:
            nxt = parse_cron(spec.cron).next_fire(now, tz)
        except CronError:
            # Cannot happen — the cron was validated at startup — but stay safe.
            nxt = None
        if nxt is None:
            fire = "never (untriggerable)"
        else:
            fire = nxt.strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"**{spec.name}** — `{spec.cron}` → next: {fire}")
    lines += ["", f"(Times in {config.schedule_timezone or 'local tz'}. Read-only — this does not trigger anything.)"]
    # Name + cron + next-fire only; never prompt / chat_id / user_id.
    return "\n".join(lines)


# The generic notice for an *unexpected* (non-AgentError) command failure. It is
# deliberately fixed and content-free — an unexpected error must not echo the
# exception text (which can carry a path or a body) back to the user.
_GENERIC_COMMAND_ERROR = "出现了一个意外错误，请稍后重试。"

# name → handler. The dispatch table; its keys are exactly the command names in
# ``_QQ_COMMANDS`` (a test asserts the two stay in lockstep).
_DISPATCH: dict[str, "object"] = {
    "new": cmd_new,
    "stop": cmd_stop,
    "help": cmd_help,
    "status": cmd_status,
    "context": cmd_context,
    "remember": cmd_remember,
    "memories": cmd_memories,
    "forget": cmd_forget,
    "tool_audit": cmd_tool_audit,
    "mcp_status": cmd_mcp_status,
    "infra_status": cmd_infra_status,
    "schedule_status": cmd_schedule_status,
}


def known_command_names() -> frozenset[str]:
    """The set of command names (without the leading ``/``) that dispatch handles.

    Exposed so :mod:`.bot` can tell a *known* command (intercept and handle) from
    an *unknown* ``/…`` string (fall through to a normal agent turn). Derived from
    the dispatch table, which is exactly the command set in ``_QQ_COMMANDS``.
    """
    return frozenset(_DISPATCH)


async def dispatch(
    command: str,
    args: str,
    *,
    service: AgentService,
    repo: ConversationRepository,
    config: Config,
    mcp_manager,
    conversation_id: int,
    memory_scope: str,
    in_flight: dict[int, "object"],
) -> str | None:
    """Run the named QQ command and return its reply string.

    Returns the reply string (Markdown) to deliver, or ``None`` for "send
    nothing" (a successful ``/stop`` that cancelled a live turn — the cancelled
    turn posts its own notice). **Unknown commands return ``None`` too**, so the
    caller in :mod:`.bot` can fall through to a normal agent turn — an unknown
    ``/foo`` is never swallowed (matching Telegram, where unmatched ``/…`` text
    reaches the message handler).

    Errors are contained here, mirroring the Telegram handlers: an
    :class:`AgentError` (usage / memory / audit problems) becomes its
    user-safe message; any other exception is logged by class and surfaced as a
    fixed generic notice. The dispatcher itself never raises.
    """
    handler = _DISPATCH.get(command)
    if handler is None:
        return None

    try:
        if command == "new":
            return await handler(service, conversation_id)
        if command == "stop":
            return await handler(in_flight, conversation_id)
        if command == "help":
            return await handler()
        if command == "status":
            return await handler(service, repo, config, conversation_id)
        if command == "context":
            return await handler(service, repo, conversation_id)
        if command == "remember":
            return await handler(service, memory_scope, args)
        if command == "memories":
            return await handler(service, memory_scope)
        if command == "forget":
            return await handler(service, memory_scope, args)
        if command == "tool_audit":
            return await handler(service, memory_scope, args)
        if command == "mcp_status":
            return await handler(config, mcp_manager)
        if command == "infra_status":
            return await handler(config)
        # command == "schedule_status"
        return await handler(config)
    except AgentError as exc:
        return exc.user_safe
    except Exception:
        # Log by class only (exc_info carries the type, not the body); never the
        # args, the scope, or any secret.
        logger.exception("qq command failed", extra={"conversation_id": conversation_id})
        return _GENERIC_COMMAND_ERROR
