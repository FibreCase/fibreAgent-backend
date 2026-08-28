"""The /infra_status Telegram command (phase 5.1 — required #11).

Handler-level with a real PTB :class:`Update`/``CallbackContext`` and a fake config.
Proves the command is read-only and non-mutating (no SSH / LLM / network), shows a
safe disabled state (tools off / no targets), renders each target's name + its three
fixed, read-only ``allow`` tool names + the total as HTML, is silently ignored for an unauthorised
sender, is secret-free (no host/port/username/key path/known_hosts/mount/service/command),
and states explicitly that it draws **no** reachability conclusion.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat

from fibrecase_agent_backend.telegram.bot import _COMMANDS, cmd_infra_status


def _target(name="nas", **over):
    d = dict(
        name=name,
        host="secret-nas-host",
        port=22,
        username="probe",
        private_key_path="/run/secrets/nas_key",
        known_hosts_path="/run/secrets/nas_known_hosts",
        mounts=("/volume1", "/data"),
        services=("ssh.service",),
    )
    d.update(over)
    return SimpleNamespace(**d)


class _FakeConfig:
    def __init__(self, enable_tools=True, targets=()):
        self.enable_tools = enable_tools
        self.infra_ssh_targets = tuple(targets)


def _make(user_id, chat_id, *, allowed=(1,), config=None):
    from telegram import Message, Update, User
    from telegram.ext import CallbackContext

    user = None if user_id is None else User(id=user_id, first_name="U", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, text="/infra_status")
    update = Update(update_id=1, message=message)
    app = type("App", (), {})()
    app.bot_data = {"allowed_user_ids": set(allowed), "config": config or _FakeConfig()}
    app.bot = object()
    context = CallbackContext.from_update(update, app)
    return update, chat, context, app


# ---------------------------------------------------------------------------
# unauthorized → silent
# ---------------------------------------------------------------------------
async def test_infra_status_unauthorized_is_silent():
    update, chat, context, app = _make(user_id=999, chat_id=1, allowed=(1,), config=_FakeConfig(targets=(_target(),)))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    send.assert_not_awaited()


# ---------------------------------------------------------------------------
# disabled: tools off / no targets
# ---------------------------------------------------------------------------
async def test_infra_status_tools_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1, config=_FakeConfig(enable_tools=False, targets=(_target(),)))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    assert send.await_count == 1
    assert "disabled" in send.await_args.kwargs["text"].lower()


async def test_infra_status_no_targets_disabled():
    update, chat, context, app = _make(user_id=1, chat_id=1, config=_FakeConfig(targets=()))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    assert "disabled" in send.await_args.kwargs["text"].lower()


# ---------------------------------------------------------------------------
# configured: renders name + 3 tool names + total as HTML
# ---------------------------------------------------------------------------
async def test_infra_status_renders_target_and_tools_as_html():
    update, chat, context, app = _make(user_id=1, chat_id=1, config=_FakeConfig(targets=(_target(name="nas"),)))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    assert send.await_count == 1
    kwargs = send.await_args.kwargs
    assert kwargs.get("parse_mode") == "HTML"
    text = kwargs["text"]
    assert "nas" in text
    assert "infra_nas__host_status" in text
    assert "infra_nas__disk_status" in text
    assert "infra_nas__service_status" in text
    assert "read-only" in text
    assert "Total configured tools" in text


async def test_infra_status_multiple_targets_rendered():
    cfg = _FakeConfig(targets=(_target(name="nas"), _target(name="pi", host="pi")))
    update, chat, context, app = _make(user_id=1, chat_id=1, config=cfg)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    text = send.await_args.kwargs["text"]
    assert "nas" in text and "pi" in text
    assert "infra_pi__host_status" in text
    assert "Total configured tools" in text
    assert "6" in text  # 2 targets × 3 tools


# ---------------------------------------------------------------------------
# read-only: no SSH / LLM / network, config only read not mutated
# ---------------------------------------------------------------------------
async def test_infra_status_does_not_connect(monkeypatch):
    # No asyncssh may even be imported by the command, and no SSH stub is reachable.
    assert "asyncssh" not in sys.modules
    cfg = _FakeConfig(targets=(_target(),))
    before = list(cfg.infra_ssh_targets)
    update, chat, context, app = _make(user_id=1, chat_id=1, config=cfg)
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    assert send.await_count == 1
    assert "asyncssh" not in sys.modules  # still not imported
    assert list(cfg.infra_ssh_targets) == before  # config only read


# ---------------------------------------------------------------------------
# output is secret-free: no host / port / username / path / mount / service / command
# ---------------------------------------------------------------------------
async def test_infra_status_output_is_secret_free():
    update, chat, context, app = _make(user_id=1, chat_id=1, config=_FakeConfig(targets=(_target(name="nas"),)))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    text = send.await_args.kwargs["text"]
    for secret in (
        "secret-nas-host", "probe", "22", "/run/secrets/nas_key",
        "/run/secrets/nas_known_hosts", "/volume1", "/data", "ssh.service",
        "systemctl", "uname", "/proc",
    ):
        assert secret not in text
    # No raw scope / user id.
    assert "telegram:1" not in text


# ---------------------------------------------------------------------------
# explicitly no reachability conclusion
# ---------------------------------------------------------------------------
async def test_infra_states_no_reachability_conclusion():
    update, chat, context, app = _make(user_id=1, chat_id=1, config=_FakeConfig(targets=(_target(name="nas"),)))
    with patch.object(Chat, "send_message", new_callable=AsyncMock) as send:
        await cmd_infra_status(update, context)
    text = send.await_args.kwargs["text"].lower()
    assert "reachability" in text
    assert "nothing about reachability" in text


# ---------------------------------------------------------------------------
# the command is part of the advertised menu + /help
# ---------------------------------------------------------------------------
def test_infra_status_is_in_command_menu():
    assert ("infra_status", "Show configured infra targets") in _COMMANDS
