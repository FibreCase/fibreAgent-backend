"""QQ (C2C) slash-commands — channel-agnostic command logic, botpy-free.

This module holds the *logic* of the QQ bot's slash-commands: it reuses the same
channel-agnostic :class:`~..agent.service.AgentService` methods the Telegram
adapter uses, but renders the replies **in Chinese** (the QQ channel's
user-facing language; the Telegram adapter renders the same commands in English).
It is deliberately
**independent** of :mod:`..telegram` (a channel must not import another channel)
and of ``botpy`` (this package's only botpy imports live in
:mod:`.bot`), so it can be unit-tested with lightweight fakes and no live client.

**Formatting is split by shape, not by command — and the split is carried to
delivery:** a *simple* one-line receipt (``/new``, the ``/stop`` "nothing running"
reply, ``/remember``, the ``/forget`` outcomes, and the disabled / "none yet"
notices) is **plain text** — no markdown markers, delivered by the channel as a
``msg_type=0`` message. A *structured* display (``/help``, ``/status``,
``/context``, ``/memories``, ``/tool_audit``, and the enabled ``/mcp_status`` /
``/infra_status`` / ``/schedule_status``) keeps **Markdown** for its headers /
fields / lists, delivered as a ``msg_type=2`` message. Each handler reports the
shape of *its* reply via :class:`CommandReply`, so the same command can be plain
in one branch and Markdown in another (e.g. ``/memories`` is a plain "none yet"
notice when empty, a Markdown list when it has memories).

The transport half — parsing the leading ``/token`` off an incoming C2C message,
deciding *which* command ran, delivering the returned string over the QQ
websocket, and (for ``/stop``) the QQ-local in-flight registry — lives in
:mod:`.bot`. Everything here returns a :class:`CommandReply` (or ``None`` for
"send nothing") and never sends anything itself.

Privacy mirrors the Telegram layer: none of these commands exposes a key, token,
endpoint, host, mount path, service name, command, ``prompt``/``chat_id``/
``user_id``, or the raw ``user_openid``. They operate on the opaque
``memory_scope`` string and the (synthetic) integer conversation id only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

from .. import __version__
from ..agent.service import AgentError, AgentService
from ..automation.cron import CronError, parse_cron
from ..config import Config
from ..database.repository import ConversationRepository
from ..infrastructure import local_tool_name

logger = logging.getLogger("qq")


class CommandReply(NamedTuple):
    """A command's reply: its delivery ``text`` and whether it is ``markdown``.

    ``markdown`` picks the delivery *type* in :mod:`.bot`: ``True`` sends a
    ``msg_type=2`` Markdown message (a *structured* display — ``/help``,
    ``/status``, ``/context``, the ``/memories`` / ``/tool_audit`` lists, and the
    enabled ``/mcp_status`` / ``/infra_status`` / ``/schedule_status``); ``False``
    sends a ``msg_type=0`` plain-text message (a *simple* one-line receipt —
    ``/new``, the ``/stop`` "nothing running" reply, ``/remember``, the
    ``/forget`` outcomes, and the disabled / "none yet" notices). The decision is
    made per branch in each handler, not inferred from the text.
    """

    text: str
    markdown: bool = False

# The QQ command set: the Telegram core set + read-only ``/mcp_status``, minus
# ``/start`` (no QQ concept) and ``/mcp`` / ``/mcp auth`` (the OAuth login is
# Telegram-bound — it returns an inline login *button* and a proactive
# completion notification, both of which QQ cannot reliably deliver). Each
# entry is ``(command, description)`` where the description is **Chinese** — the
# QQ channel's user-facing language. It is the single source of truth for *both*
# user-facing surfaces that describe a command: the ``/help`` reply (a QQ command
# *reply*) and the native command *panel* (the ``/v2/panels`` 指令面板) — so the
# command set, its order, and its wording stay in lockstep across the two.
_QQ_COMMANDS: list[tuple[str, str]] = [
    ("new", "开始新的会话"),
    ("stop", "停止当前回复"),
    ("help", "显示帮助"),
    ("status", "显示运行状态"),
    ("context", "显示上下文预算"),
    ("remember", "保存一条长期记忆"),
    ("memories", "查看你的记忆"),
    ("forget", "删除一条记忆或全部"),
    ("tool_audit", "查看工具审计日志"),
    ("mcp_status", "查看远程 MCP 工具状态"),
    ("infra_status", "查看已配置的基础设施目标"),
    ("schedule_status", "查看已配置的定时任务"),
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
    fills the ``/<cmd>`` into the input box. The ``desc`` is the command's
    **Chinese** description (the native command *panel* is rendered in the channel's
    user-facing language, exactly like the ``/help`` reply) — the same value the
    ``/help`` list shows, so the two surfaces never drift. Items are filtered to
    those whose ``name`` fits the 14-char panel cap (``/schedule_status`` drops out
    here) and the panel to at most 20 items (the API maximum), in the command
    table's order. Returns ``[]`` if nothing qualifies.
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


async def cmd_new(service: AgentService, conversation_id: int) -> CommandReply:
    """/new — start a fresh conversation for this chat (drops all its history)."""
    await service.reset(conversation_id, conversation_id)
    return CommandReply("已开始新会话（历史已清空）。")


async def cmd_stop(in_flight: dict[int, "object"], conversation_id: int) -> CommandReply | None:
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
        return CommandReply("没有正在进行的回复。")
    task.cancel()
    # Do not await the cancelled task here: it is unwinding in its own task and
    # posts its own notice. Awaiting it could deadlock on the very
    # per-conversation lock it is releasing.
    return None


async def cmd_help() -> CommandReply:
    """List the available commands (generated from ``_QQ_COMMANDS``).

    The ``/help`` reply is a QQ command *reply*, so it is rendered in **Chinese**
    (the channel's user-facing language) — the same descriptions the native command
    *panel* (``/v2/panels``) shows.
    """
    lines = ["**可用命令：**", ""]
    for cmd, desc in _QQ_COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines += ["", "其他文字消息都会发送给 Agent。"]
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_status(service: AgentService, repo: ConversationRepository, config: Config,
                     conversation_id: int) -> CommandReply:
    """/status — run status: version, model, and (if any) the current
    conversation and its message count."""
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        lines = [
            "**Agent 后端：**",
            "**状态：** 正常",
            "",
            f"**版本：** {__version__}",
            f"**模型：** {config.openai_model}",
            "**会话：** （暂无——发一条消息即可）",
            "**数据库：** 正常",
        ]
    else:
        status = await service.conversation_status(conversation.id)
        lines = [
            "**Agent 后端：**",
            "**状态：** 正常",
            "",
            f"**版本：** {__version__}",
            f"**模型：** {config.openai_model}",
            f"**会话：** {conversation.id}",
            f"**消息数：** {status['messages']}",
            "**数据库：** 正常",
        ]
    # None of the above exposes keys, tokens, or file paths.
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_context(service: AgentService, repo: ConversationRepository,
                      conversation_id: int) -> CommandReply:
    """/context — a read-only preview of how much stored history (and its images)
    would fit both the message cap and the estimated-token budget. It never
    reads an attachment blob (planning is metadata-only)."""
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        return CommandReply("还没有会话——请先发一条消息。")

    s = await service.context_status(conversation.id)
    free_tokens = s["budget"] - s["estimated_cost"]
    free_messages = s["cap"] - (s["history_messages"] + 1)
    images_downgraded = s["images_in_store"] - s["images_kept"]
    lines = [
        "**上下文：**",
        f"**会话：** {s['conversation_id']}",
        "",
        f"**消息上限：** {s['cap']}",
        f"**已存：** {s['stored_messages']} 条",
        f"**本回合保留：** {s['history_messages']} 条（+1 当前）",
        f"**剩余空间：** 约 {free_messages} 条",
        "",
        f"**估算预算：** {s['budget']} 单位",
        f"**已用：** 约 {s['estimated_cost']} 单位（系统 {s['system_cost']}）",
        f"**剩余：** 约 {free_tokens} 单位",
        "",
        f"**保留的历史图片：** {s['images_kept']} / {s['images_in_store']}"
        + (f"（{images_downgraded} 张已降级为纯文本）" if images_downgraded > 0 else ""),
        "",
        "（保守估算，非精确 token 数。）",
    ]
    # None of the above exposes message text, keys, tokens, digests, or paths.
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_remember(service: AgentService, memory_scope: str, args: str) -> CommandReply:
    """/remember <content> — save one explicit long-term memory."""
    record = await service.remember_memory(memory_scope, args)
    return CommandReply(f"记忆已保存。\n编号：{record.id}\n\n{record.content}")


async def cmd_memories(service: AgentService, memory_scope: str) -> CommandReply:
    """/memories — list the caller's own memories."""
    records = await service.list_memories(memory_scope)
    if not records:
        return CommandReply("还没有保存任何记忆。用 /remember <文字> 保存一条。")

    lines = [f"**你的记忆：**（共 {len(records)} 条）", ""]
    for r in records:
        lines.append(f"**#{r.id}**（保存于 {_utc_stamp(r.created_at)}）")
        lines.append(r.content)
        lines.append("")
    return CommandReply("\n".join(lines).rstrip(), markdown=True)


async def cmd_forget(service: AgentService, memory_scope: str, args: str) -> CommandReply:
    """/forget <id> — delete one memory; /forget all CONFIRM — delete all.

    ``/forget all`` without the exact ``CONFIRM`` token only shows the
    confirmation format and changes nothing. A foreign/missing id is reported
    exactly as a missing one (no existence leak).
    """
    tokens = args.split()
    if not tokens:
        raise AgentError("用法：/forget <id> 或 /forget all CONFIRM", "memory_invalid")

    if tokens[0].lower() == "all":
        if len(tokens) >= 2 and tokens[1] == "CONFIRM":
            removed = await service.forget_all_memories(memory_scope)
            return CommandReply(f"已清除全部记忆。（删除 {removed} 条）")
        return CommandReply("要清除全部记忆，请发送：/forget all CONFIRM")

    try:
        memory_id = int(tokens[0])
    except ValueError:
        raise AgentError("用法：/forget <id> 或 /forget all CONFIRM", "memory_invalid")
    await service.forget_memory(memory_scope, memory_id)
    return CommandReply(f"记忆已删除。（编号：{memory_id}）")


_TOOL_AUDIT_MAX_LIMIT = 50


async def cmd_tool_audit(service: AgentService, memory_scope: str, args: str) -> CommandReply:
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
            return CommandReply("用法：/tool_audit [limit]  （limit 为 1-50）")

    records = await service.list_tool_audit_events(memory_scope, limit)
    if not records:
        return CommandReply("还没有工具活动。工具调用会在这里显示。")

    lines = [f"**工具审计：**（最近 {len(records)} 条，最新在前）", ""]
    for r in records:
        line = f"**#{r.id}** {_utc_stamp(r.created_at)} — {r.tool_name} / {r.event_type}"
        if r.code:
            line += f" / {r.code}"
        if r.latency_ms is not None:
            line += f" / {r.latency_ms}ms"
        lines.append(line)
    lines += ["", "（码为稳定的可读状态标签——不显示参数与结果。）"]
    # None of the above exposes tool args, results, exception text, raw scope,
    # the user id, or any secret.
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_mcp_status(config: Config, mcp_manager) -> CommandReply:
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
        return CommandReply("MCP：未启用")

    lines = ["**远程 MCP 服务器：**", ""]
    for entry in mcp_manager.status():
        state = "可用" if entry["available"] else "不可用"
        lines.append(f"**{entry['name']}** — {state}（{entry['tool_count']} 个工具）")
    lines += ["", f"**可用 MCP 工具总数：** {mcp_manager.total_tools}"]
    # None of the above exposes a URL, host, header, token, description, schema,
    # server instructions, or any failure detail.
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_infra_status(config: Config) -> CommandReply:
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
        return CommandReply("基础设施观测：未启用")

    lines = ["**基础设施观测目标（只读）：**", ""]
    for target in config.infra_ssh_targets:
        tool_names = ", ".join(
            f"`{local_tool_name(target.name, obs)}`"
            for obs in ("host_status", "disk_status", "service_status")
        )
        lines.append(f"**{target.name}** — 已配置（3 个工具，只读）：{tool_names}")
    lines += [
        "",
        f"**已配置工具总数：** {len(config.infra_ssh_targets) * 3}",
        "",
        "（仅显示已配置项——不反映可达性；只有真正调用对应工具时才读取状态。）",
    ]
    # None of the above exposes a host, port, username, key path, known_hosts
    # path, mount path, service name, or command — only the target name and the
    # (operator-named) local tool names.
    return CommandReply("\n".join(lines), markdown=True)


async def cmd_schedule_status(config: Config) -> CommandReply:
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
        return CommandReply("定时任务：未启用（未配置）")

    tz = ZoneInfo(config.schedule_timezone) if config.schedule_timezone else datetime.now().astimezone().tzinfo
    now = datetime.now(tz)
    lines = ["**定时任务：**", ""]
    for spec in config.schedules:
        try:
            nxt = parse_cron(spec.cron).next_fire(now, tz)
        except CronError:
            # Cannot happen — the cron was validated at startup — but stay safe.
            nxt = None
        if nxt is None:
            fire = "永不（无法触发）"
        else:
            fire = nxt.strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"**{spec.name}** — `{spec.cron}` → 下次触发：{fire}")
    lines += ["", f"（时间为 {config.schedule_timezone or '本地时区'}。只读——此命令不触发任何运行。）"]
    # Name + cron + next-fire only; never prompt / chat_id / user_id.
    return CommandReply("\n".join(lines), markdown=True)


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
) -> CommandReply | None:
    """Run the named QQ command and return its reply.

    Returns a :class:`CommandReply` (whose ``markdown`` flag tells :mod:`.bot`
    whether to deliver the text as a ``msg_type=2`` Markdown message or a
    ``msg_type=0`` plain-text message), or ``None`` for "send nothing" (a
    successful ``/stop`` that cancelled a live turn — the cancelled turn posts its
    own notice). **Unknown commands return ``None`` too**, so the caller in
    :mod:`.bot` can fall through to a normal agent turn — an unknown ``/foo`` is
    never swallowed (matching Telegram, where unmatched ``/…`` text reaches the
    message handler).

    Errors are contained here, mirroring the Telegram handlers: an
    :class:`AgentError` (usage / memory / audit problems) becomes its
    user-safe message (as plain text — a short receipt, never a display); any
    other exception is logged by class and surfaced as a fixed generic notice.
    The dispatcher itself never raises.
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
        return CommandReply(exc.user_safe)
    except Exception:
        # Log by class only (exc_info carries the type, not the body); never the
        # args, the scope, or any secret.
        logger.exception("qq command failed", extra={"conversation_id": conversation_id})
        return CommandReply(_GENERIC_COMMAND_ERROR)
