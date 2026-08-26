"""Phase 4.x — per-user OAuth token injection on the MCP http client.

Proves the *minimal* MCP-client integration: the ``auth=`` hook (an
:class:`~..mcp.auth.oauth_auth.McpOAuthAuth` ``httpx2.Auth``) attaches the
**requesting user's** access token to each request — resolved from the
``active_principal`` contextvar that the tool loop sets — and attaches nothing
for no principal / an unparseable scope / a user with no credential / a manager
failure (the request still goes out; the server's rejection is the stable
fallback). The token is never logged. Also covers the manager-level wiring:
an OAuth server without the auth factory fails startup with a stable code
(and never opens a connection), with the factory it connects, and the tool
loop sets the principal contextvar around tool execution so the request
carries the right user's token. All HTTP is faked with ``httpx2.MockTransport``
— no network.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from fibrecase_agent_backend.agent.context import ChatMessage
from fibrecase_agent_backend.agent.tool_loop import run_tool_loop
from fibrecase_agent_backend.llm.client import LLMResult
from fibrecase_agent_backend.mcp import McpManager, CODE_OAUTH_NOT_CONFIGURED
from fibrecase_agent_backend.mcp.auth.manager import OAuthManager
from fibrecase_agent_backend.mcp.auth.principal import (
    active_principal,
    telegram_user_id_from_scope,
)
from fibrecase_agent_backend.mcp.auth.models import CredentialRecord, TokenResponse, utcnow
from fibrecase_agent_backend.mcp.wrapper import McpTool
from fibrecase_agent_backend.tools import ToolRegistry, build_policy, ToolPermission

from test_oauth_manager import _FakeProvider, _FakeStorage

from datetime import timedelta

from fibrecase_agent_backend.config import McpServer


# ---------------------------------------------------------------------------
# scope parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "scope,expected",
    [
        ("telegram:12345", 12345),
        ("telegram:1", 1),
        (None, None),
        ("", None),
        ("web:123", None),
        ("telegram:", None),
        ("telegram:12a", None),
        ("telegram:-5", None),
        ("telegram", None),
    ],
)
def test_telegram_user_id_from_scope(scope, expected):
    assert telegram_user_id_from_scope(scope) == expected


# ---------------------------------------------------------------------------
# the auth hook against a MockTransport (no network)
# ---------------------------------------------------------------------------
def _auth_manager(**kw):
    storage = kw.pop("storage", None) or _FakeStorage()
    provider = kw.pop("provider", None) or _FakeProvider()
    mgr = OAuthManager(
        storage=storage,
        providers={"google": provider},
        server_providers={"gcal": "google"},
        callback_base_url="https://ex.com",
    )
    return mgr, storage, provider


def _auth(*, manager=None):
    from fibrecase_agent_backend.mcp.auth.oauth_auth import McpOAuthAuth

    manager = manager or _auth_manager()[0]
    return McpOAuthAuth(manager=manager, mcp_server="gcal")


async def _send(auth, *, principal=None, base="https://g.example/mcp"):
    """One request through an httpx2 client carrying ``auth``; returns the
    Authorization header the server saw (or None)."""
    seen: dict[str, str | None] = {"auth": None}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=auth) as client:
        token = active_principal.set(principal) if principal is not None else None
        try:
            await client.get(base)
        finally:
            if token is not None:
                active_principal.reset(token)
    return seen["auth"]


async def test_no_principal_no_token_header():
    assert await _send(_auth(), principal=None) is None


async def test_requesting_user_token_attached():
    mgr, storage, _ = _auth_manager()
    from fibrecase_agent_backend.mcp.auth.models import CredentialRecord

    now = utcnow()
    await storage.save_credential(CredentialRecord(
        telegram_user_id=12345, provider="google", mcp_server="gcal",
        access_token="AT-USER-12345", refresh_token="RT", expires_at=now + timedelta(hours=1),
        scopes="s", updated_at=now,
    ))
    auth = _auth(manager=mgr)
    assert await _send(auth, principal="telegram:12345") == "Bearer AT-USER-12345"


async def test_foreign_user_gets_no_other_users_token():
    mgr, storage, _ = _auth_manager()
    now = utcnow()
    await storage.save_credential(CredentialRecord(
        telegram_user_id=1, provider="google", mcp_server="gcal",
        access_token="AT-USER-1", refresh_token="RT", expires_at=now + timedelta(hours=1),
        scopes="s", updated_at=now,
    ))
    auth = _auth(manager=mgr)
    # User 2 has no credential of their own → no header (and never user 1's).
    assert await _send(auth, principal="telegram:2") is None


async def test_non_telegram_scope_gets_no_token():
    assert await _send(_auth(), principal="web:123") is None


async def test_no_credential_no_header():
    assert await _send(_auth(), principal="telegram:99") is None


async def test_expired_token_is_refreshed_and_rotation_persisted():
    mgr, storage, provider = _auth_manager()
    provider.refresh_result = TokenResponse(
        access_token="AT-FRESH", refresh_token="RT-ROTATED",
        expires_at=utcnow() + timedelta(hours=1), scopes=None,
    )
    now = utcnow()
    await storage.save_credential(CredentialRecord(
        telegram_user_id=7, provider="google", mcp_server="gcal",
        access_token="AT-STALE", refresh_token="RT-OLD", expires_at=now - timedelta(seconds=120),
        scopes="s", updated_at=now,
    ))
    auth = _auth(manager=mgr)
    header = await _send(auth, principal="telegram:7")
    assert header == "Bearer AT-FRESH"
    # The rotated refresh token is persisted for the next time.
    assert storage.credentials[(7, "google", "gcal")].refresh_token == "RT-ROTATED"
    assert storage.credentials[(7, "google", "gcal")].access_token == "AT-FRESH"


async def test_manager_failure_sends_request_without_token_and_never_logs_it():
    class _BoomStorage(_FakeStorage):
        async def get_credential(self, **_kw):
            raise RuntimeError("db exploded: AT-LEAK-DETAIL")

    mgr, _, _ = _auth_manager(storage=_BoomStorage())
    auth = _auth(manager=mgr)

    with caplog_capture() as records:  # see helper below
        header = await _send(auth, principal="telegram:7")
    assert header is None  # the request still went out
    for rec in records:
        assert "AT-LEAK-DETAIL" not in rec.getMessage()
        assert "telegram:7" not in rec.getMessage()


class _Caplog:
    def __init__(self, name="mcp.auth"):
        self.name = name

    def __enter__(self):
        import logging

        self.records = []
        self.h = logging.Handler()
        self.h.emit = self.records.append
        self.logger = logging.getLogger(self.name)
        self.old = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.h)
        return self.records

    def __exit__(self, *exc):
        self.logger.removeHandler(self.h)
        self.logger.setLevel(self.old)
        return False


def caplog_capture(name="mcp"):
    return _Caplog(name)


# ---------------------------------------------------------------------------
# manager wiring: OAuth server +/− the auth factory
# ---------------------------------------------------------------------------
async def test_oauth_server_without_factory_fails_stable_and_never_connects(monkeypatch):
    connected = []

    import fibrecase_agent_backend.mcp.manager as mgr_mod
    from test_mcp import _make_session, _list_result, _tool_dict

    sess, _ = _make_session(list_result=_list_result(_tool_dict("cal")))

    class _Http:
        def __init__(self, **_kw):
            connected.append(True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_streamable(url, http_client=None, terminate_on_close=False):
        class _Streams:
            async def __aenter__(self):
                return (_Stream(), _Stream())

            async def __aexit__(self, *exc):
                return False

        return _Streams()

    monkeypatch.setattr(mgr_mod, "create_mcp_http_client", lambda **kw: _Http(**kw))
    monkeypatch.setattr(mgr_mod, "streamable_http_client", fake_streamable)

    mgr = McpManager(
        [McpServer(name="gcal", url="https://g.example/mcp", auth_type="oauth", auth_provider="google")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
        oauth_auth_factory=None,  # OAuth infra not wired
    )
    with caplog_capture() as records:
        await mgr.start()
    state = mgr.status()[0]
    assert state["available"] is False
    # The stable code is logged (the status view intentionally exposes only
    # name/available/tool_count — never a failure detail).
    logged = {getattr(r, "code", None) for r in records}
    assert CODE_OAUTH_NOT_CONFIGURED in logged
    # No http client was even constructed — nothing connected.
    assert connected == []
    assert mgr.tools() == []
    await mgr.close()


async def test_oauth_server_with_factory_connects_and_passes_auth(monkeypatch):
    import fibrecase_agent_backend.mcp.manager as mgr_mod
    from test_mcp import _make_session, _list_result, _tool_dict

    seen_auth = []
    sess, _ = _make_session(list_result=_list_result(_tool_dict("cal")))

    class _Http:
        def __init__(self, auth=None):
            seen_auth.append(auth)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_streamable(url, http_client=None, terminate_on_close=False):
        class _Streams:
            async def __aenter__(self):
                return (_Stream(), _Stream())

            async def __aexit__(self, *exc):
                return False

        return _Streams()

    fake_http = monkeypatch.setattr(mgr_mod, "create_mcp_http_client", lambda **kw: _Http(**kw))
    monkeypatch.setattr(mgr_mod, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(
        mgr_mod,
        "ClientSession",
        lambda read, write, read_timeout_seconds=None: sess(read, write, read_timeout_seconds),
    )

    mgr = McpManager(
        [McpServer(name="gcal", url="https://g.example/mcp", auth_type="oauth", auth_provider="google")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
        oauth_auth_factory=lambda spec: _auth(manager=_auth_manager()[0]),
    )
    await mgr.start()
    assert mgr.status()[0]["available"] is True
    assert mgr.tools()[0].name == "mcp_gcal__cal"
    # The http client was built carrying the per-user auth hook.
    assert len(seen_auth) == 1
    from fibrecase_agent_backend.mcp.auth.oauth_auth import McpOAuthAuth

    assert isinstance(seen_auth[0], McpOAuthAuth)
    await mgr.close()


# ---------------------------------------------------------------------------
# the tool loop sets the principal contextvar around tool execution
# ---------------------------------------------------------------------------
class _ScriptedLLM:
    def __init__(self, results):
        self.results = list(results)

    async def complete(self, messages, *, tools=None):
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tc(name, arguments, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


class _NoopAuditor:
    async def record_pre(self, event):
        return True

    async def record(self, event):
        return True



async def test_tool_loop_sets_principal_around_execute(monkeypatch):
    """End-to-end: a scoped tool loop puts the *right* user's token on the MCP
    request — via a real ``McpTool`` whose fake session issues a real httpx2
    request through an ``McpOAuthAuth`` + ``MockTransport``."""
    mgr, storage, _ = _auth_manager()
    now = utcnow()
    for uid, tok in ((1, "AT-USER-1"), (2, "AT-USER-2")):
        await storage.save_credential(CredentialRecord(
            telegram_user_id=uid, provider="google", mcp_server="gcal",
            access_token=tok, refresh_token="RT", expires_at=now + timedelta(hours=1),
            scopes="s", updated_at=now,
        ))
    auth = _auth(manager=mgr)
    seen: dict[str, str | None] = {"auth": None}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx2.Response(200, json={"ok": True})

    class _FakeSession:
        """A session whose ``call_tool`` issues one real httpx2 request (faked
        transport) so the auth hook runs exactly as in production."""

        async def call_tool(self, name, arguments=None):
            transport = httpx2.MockTransport(handler)
            async with httpx2.AsyncClient(transport=transport, auth=auth) as client:
                await client.get("https://g.example/mcp")
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(content=[TextContent(type="text", text="done")], is_error=False)

    tool = McpTool(
        server_name="gcal",
        remote_name="cal",
        description="d",
        parameters={"type": "object", "properties": {}},
        session=_FakeSession(),
        max_result_chars=1000,
    )
    registry = ToolRegistry().add(tool)
    policy = build_policy({"mcp_gcal__cal": ToolPermission.ALLOW}, registry=registry)
    llm = _ScriptedLLM([
        LLMResult(content=None, tool_calls=[_tc("mcp_gcal__cal", "{}")]),
        LLMResult(content="ok"),
    ])
    await run_tool_loop(
        llm,
        [ChatMessage("system", "S"), ChatMessage("user", "u")],
        registry,
        max_iterations=3,
        policy=policy,
        approval_provider=None,
        auditor=_NoopAuditor(),
        tool_timeout_seconds=5,
        approval_timeout_seconds=5,
        conversation_id=1,
        scope="telegram:1",
    )
    # The request carried **user 1's** token — not user 2's, not none.
    assert seen["auth"] == "Bearer AT-USER-1"
