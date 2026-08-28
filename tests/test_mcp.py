"""Phase 4 — MCP tool provider (Streamable HTTP + stdio: config + wrapper + manager).

Covers the startup-side behaviours: strict ``MCP_SERVERS`` validation (both the
http and stdio transports), the namespaced local-name mapping, per-server failure
isolation, atomic discovery, the bearer-header-from-env contract, the
``status()``/``tools()`` surface, and the fact that a pre-2.5 DB needs no
migration. No real network, stdio, or subprocess is ever touched — the MCP
transport/session/http-client (and ``stdio_client``) are all faked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config
from fibrecase_agent_backend.mcp import (
    CODE_CONNECT_FAILED,
    CODE_DISCOVERY_FAILED,
    CODE_INITIALIZE_FAILED,
    CODE_INVALID_TOOL,
    McpManager,
    is_valid_remote_tool_name,
    local_tool_name,
)
from fibrecase_agent_backend.mcp.wrapper import McpTool
from fibrecase_agent_backend.config import McpServer


# ---------------------------------------------------------------------------
# config-load helper (mirrors test_tool_policy._load)
# ---------------------------------------------------------------------------
def _env(**extra):
    base = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_USER_IDS": "1",
        "OPENAI_BASE_URL": "https://h/v1",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
    }
    base.update(extra)
    return base


def _load(monkeypatch, **extra):
    for knob in (
        "MCP_SERVERS",
        "MCP_SERVERS_FILE",
        "MCP_ALLOW_INSECURE_HTTP",
        "MCP_CONNECT_TIMEOUT_SECONDS",
        "MAX_MCP_TOOL_RESULT_CHARS",
    ):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


# ===========================================================================
# required #2 — MCP config validation covers all illegal inputs
# ===========================================================================
def test_mcp_empty_servers_is_empty_tuple(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.mcp_servers == ()
    assert cfg.mcp_connect_timeout_seconds == 10.0
    assert cfg.max_mcp_tool_result_chars == 10000
    assert cfg.mcp_allow_insecure_http is False


def test_mcp_parses_valid_servers(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_A", "secret-value-a")
    cfg = _load(
        monkeypatch,
        MCP_SERVERS=json.dumps(
            [
                {"name": "alpha", "url": "https://a.example/mcp"},
                {"name": "beta-2", "url": "https://b.example/x", "bearer_token_env": "MCP_TOKEN_A"},
            ]
        ),
    )
    assert len(cfg.mcp_servers) == 2
    assert cfg.mcp_servers[0] == McpServer(name="alpha", url="https://a.example/mcp", bearer_token_env=None)
    assert cfg.mcp_servers[1].bearer_token_env == "MCP_TOKEN_A"
    # The token *value* is never stored on the spec — only the env-var name.
    assert "secret-value-a" not in str(cfg.mcp_servers)


def test_mcp_rejects_non_array(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS='{"name": "alpha"}')


def test_mcp_rejects_non_object_entry(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS='["alpha"]')


def test_mcp_rejects_invalid_json(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS='not json [')


@pytest.mark.parametrize(
    "name",
    ["Alpha", "1alpha", "a" * 33, "has space", "a.b", "", "a/b"],
)
def test_mcp_rejects_bad_server_name(monkeypatch, name):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": name, "url": "https://a.example/mcp"}]))


def test_mcp_rejects_duplicate_server_name(monkeypatch):
    servers = [
        {"name": "alpha", "url": "https://a.example/mcp"},
        {"name": "alpha", "url": "https://b.example/mcp"},
    ]
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps(servers))


def test_mcp_rejects_unknown_field(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": "https://a.example/mcp", "bogus": 1}]))


def test_mcp_rejects_missing_name(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"url": "https://a.example/mcp"}]))


def test_mcp_rejects_missing_url(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha"}]))


def test_mcp_rejects_non_https_by_default(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": "http://a.example/mcp"}]))
    # The message names the rule, never the full URL.
    assert "http" in str(exc.value)


def test_mcp_allows_http_only_with_opt_in(monkeypatch):
    cfg = _load(
        monkeypatch,
        MCP_ALLOW_INSECURE_HTTP="true",
        MCP_SERVERS=json.dumps([{"name": "alpha", "url": "http://127.0.0.1:8080/mcp"}]),
    )
    assert cfg.mcp_allow_insecure_http is True
    assert cfg.mcp_servers[0].url == "http://127.0.0.1:8080/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@a.example/mcp",  # userinfo
        "https://a.example/mcp#frag",  # fragment
        "https://a.example/mcp?token=secret",  # query
        "https://a.example",  # no path is fine, but test hostless:
        "https://",  # no host
        "ftp://a.example/mcp",  # bad scheme
        "//a.example/mcp",  # no scheme
    ],
)
def test_mcp_rejects_unsafe_url(monkeypatch, url):
    if url == "https://a.example":
        # A valid hostless-path URL is actually legal; skip it.
        return
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": url}]))


def test_mcp_query_token_is_rejected_and_not_echoed(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": "https://a.example/mcp?token=supersecret"}]))
    assert "supersecret" not in str(exc.value)
    assert "supersecret" not in str(exc.value)


def test_mcp_token_env_must_be_nonempty(monkeypatch):
    # bearer_token_env references an env var that is not set.
    monkeypatch.delenv("MCP_TOKEN_MISSING", raising=False)
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps(
                [{"name": "alpha", "url": "https://a.example/mcp", "bearer_token_env": "MCP_TOKEN_MISSING"}]
            ),
        )


def test_mcp_token_env_must_be_valid_name(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps(
                [{"name": "alpha", "url": "https://a.example/mcp", "bearer_token_env": "bad env name"}]
            ),
        )


def test_mcp_rejects_nonpositive_connect_timeout(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_CONNECT_TIMEOUT_SECONDS="0")


def test_mcp_rejects_nonpositive_result_chars(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MAX_MCP_TOOL_RESULT_CHARS="0")


def test_mcp_accepts_valid_numeric_knobs(monkeypatch):
    cfg = _load(monkeypatch, MCP_CONNECT_TIMEOUT_SECONDS="2.5", MAX_MCP_TOOL_RESULT_CHARS="123")
    assert cfg.mcp_connect_timeout_seconds == 2.5
    assert cfg.max_mcp_tool_result_chars == 123


# ===========================================================================
# stdio transport — config validation
# ===========================================================================
def test_mcp_parses_stdio_server(monkeypatch):
    cfg = _load(
        monkeypatch,
        MCP_SERVERS=json.dumps(
            [
                {
                    "name": "fs",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"FOO": "bar", "MY_SECRET": "topsecret"},
                    "cwd": "/tmp",
                }
            ]
        ),
    )
    assert len(cfg.mcp_servers) == 1
    s = cfg.mcp_servers[0]
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ("-y", "@modelcontextprotocol/server-filesystem", "/tmp")
    assert s.env == (("FOO", "bar"), ("MY_SECRET", "topsecret"))
    assert s.cwd == "/tmp"
    assert s.url == ""
    assert s.bearer_token_env is None
    assert s.auth_type == "none"
    # The env *value* is stored (it is operator config, not a loggable secret in
    # this phase), but it is never part of any error message (covered below).
    assert s.command is not None


def test_mcp_parses_stdio_server_minimal(monkeypatch):
    cfg = _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "python3"}]))
    s = cfg.mcp_servers[0]
    assert s.transport == "stdio"
    assert s.command == "python3"
    assert s.args == ()
    assert s.env == ()
    assert s.cwd is None


def test_mcp_stdio_defaults_to_http_without_transport(monkeypatch):
    # A server with no "transport" key is http and still requires a url — the
    # pre-stdio behaviour is byte-for-byte unchanged.
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha"}]))
    cfg = _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": "https://a.example/mcp"}]))
    assert cfg.mcp_servers[0].transport == "http"


def test_mcp_stdio_rejects_missing_command(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio"}]))


@pytest.mark.parametrize(
    "command",
    ["npx --weird", "a b", "rm -rf /", "npx; ls", "npx|ls", "npx&x", "c:\\path\\exe", "npx>out", "npx*"],
)
def test_mcp_stdio_rejects_bad_command(monkeypatch, command):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": command}]))


@pytest.mark.parametrize("command", ["python3", "/usr/local/bin/mcp-server", "./local/server", "my-tool-2"])
def test_mcp_stdio_allows_legal_command(monkeypatch, command):
    cfg = _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": command}]))
    assert cfg.mcp_servers[0].command == command


def test_mcp_stdio_rejects_url(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "url": "https://a.example"}]),
        )


def test_mcp_stdio_rejects_bearer_token_env(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps(
                [{"name": "fs", "transport": "stdio", "command": "npx", "bearer_token_env": "MCP_TOKEN_A"}]
            ),
        )


def test_mcp_stdio_rejects_oauth_authentication(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps(
                [
                    {
                        "name": "fs",
                        "transport": "stdio",
                        "command": "npx",
                        "authentication": {"type": "oauth", "provider": "google"},
                    }
                ]
            ),
        )


def test_mcp_http_rejects_stdio_fields(monkeypatch):
    for field in ("command", "args", "env", "cwd"):
        entry = {"name": "alpha", "url": "https://a.example/mcp", field: "x"}
        with pytest.raises(ConfigError):
            _load(monkeypatch, MCP_SERVERS=json.dumps([entry]))


def test_mcp_stdio_rejects_bad_transport(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "alpha", "transport": "websocket", "url": "https://a.example"}]),
        )


def test_mcp_stdio_rejects_non_array_args(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "args": "-y"}]))


def test_mcp_stdio_rejects_non_string_arg(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "args": ["ok", 1]}]),
        )


def test_mcp_stdio_rejects_empty_arg(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "args": ["ok", ""]}]),
        )


def test_mcp_stdio_rejects_non_object_env(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "env": [1, 2]}]),
        )


def test_mcp_stdio_rejects_bad_env_key(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "env": {"BAD KEY": "v"}}]),
        )


def test_mcp_stdio_rejects_empty_env_value(monkeypatch):
    with pytest.raises(ConfigError) as exc:
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "env": {"K": ""}}]),
        )
    # The error names the key, never the (empty or secret) value.
    assert "K" in str(exc.value)


def test_mcp_stdio_rejects_empty_env_object(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "env": {}}]))


def test_mcp_stdio_rejects_empty_cwd(monkeypatch):
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps([{"name": "fs", "transport": "stdio", "command": "npx", "cwd": ""}]),
        )


def test_mcp_stdio_env_value_never_in_error(monkeypatch):
    # A bad *key* in an env that also holds a secret value must not echo the value.
    with pytest.raises(ConfigError) as exc:
        _load(
            monkeypatch,
            MCP_SERVERS=json.dumps(
                [{"name": "fs", "transport": "stdio", "command": "npx", "env": {"OK": "supersecret", "BAD KEY": "v"}}]
            ),
        )
    assert "supersecret" not in str(exc.value)


def test_mcp_mixed_transports_parse_together(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_A", "secret-value-a")
    cfg = _load(
        monkeypatch,
        MCP_SERVERS=json.dumps(
            [
                {"name": "alpha", "url": "https://a.example/mcp", "bearer_token_env": "MCP_TOKEN_A"},
                {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "fs"]},
            ]
        ),
    )
    assert [s.transport for s in cfg.mcp_servers] == ["http", "stdio"]
    assert cfg.mcp_servers[0].url == "https://a.example/mcp"
    assert cfg.mcp_servers[1].command == "npx"


# ===========================================================================
# required #7 (naming) — local_tool_name + is_valid_remote_tool_name
# ===========================================================================
def test_local_tool_name_is_namespaced():
    assert local_tool_name("alpha", "get_weather") == "mcp_alpha__get_weather"


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("get_weather", True),
        ("a_b-c9", True),
        ("A-Z_0", True),
        ("", False),
        ("has space", False),
        ("with.dot", False),
        ("with/slash", False),
        ("mcp_injection", True),  # legal chars; collision handled at manager
        ("x" * 91, False),  # over the 90-char remote cap
        ("x" * 90, True),
    ],
)
def test_is_valid_remote_tool_name(remote, expected):
    assert is_valid_remote_tool_name(remote, server_name="alpha") is expected


# ===========================================================================
# Fakes for the MCP transport / session / http client (no network).
# ===========================================================================
def _make_session(*, init_result=None, list_result=None, call_behavior=None, fail_on=None):
    """A fake ``ClientSession``-shaped async context manager.

    ``init_result``/``list_result`` are return values (or an Exception to raise)
    for ``initialize()`` / ``list_tools()``. ``call_behavior`` maps a remote tool
    name to a return value or an Exception. ``fail_on`` is an optional dict of
    method-name -> Exception to raise.
    """
    calls = {"initialize": 0, "list_tools": 0, "call_tool": []}

    class _FakeSession:
        def __init__(self, read, write, read_timeout_seconds=None):
            self._read = read
            self._write = write

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            calls["initialize"] += 1
            v = init_result if init_result is not None else object()
            if isinstance(v, Exception):
                raise v
            return v

        async def list_tools(self):
            calls["list_tools"] += 1
            if list_result is None or isinstance(list_result, Exception):
                raise list_result if list_result is not None else Exception("no tools")
            return list_result

        async def call_tool(self, name, arguments=None):
            calls["call_tool"].append((name, arguments))
            if call_behavior is None:
                raise Exception("no call behavior")
            v = call_behavior(name)
            if isinstance(v, Exception):
                raise v
            return v

    return _FakeSession, calls


def _list_result(*tools):
    """Wrap a list of fake tools (dicts with name/input_schema/description)."""
    class _LR:
        def __init__(self, tools):
            self.tools = tools

    return _LR(list(tools))


def _tool_dict(name, schema=None, description="d"):
    return {"name": name, "description": description, "input_schema": schema or {"type": "object", "properties": {}}}


def _patch(monkeypatch, by_server):
    import fibrecase_agent_backend.mcp.manager as mgr

    opened_http = []

    class _Http:
        def __init__(self, headers=None):
            self.headers = headers
            opened_http.append(headers)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    # Tracks the last transport entry seen, so ``fake_client_session`` can look
    # the matching session up by url (http) or command (stdio).
    current = {"url": None, "command": None}

    def _by_server_entry():
        if current["url"] is not None:
            return by_server[current["url"]]
        return by_server[current["command"]]

    # ``streamable_http_client`` is a *synchronous* factory returning an async
    # context manager that yields ``(read, write)``. The real SDK's
    # ``ClientSession.__aenter__`` does ``async with self._write_stream``, so
    # each stream must *itself* be an async context manager.
    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_streamable(url, http_client=None, terminate_on_close=False):
        current["url"] = url
        current["command"] = None

        class _Streams:
            async def __aenter__(self):
                return (_Stream(), _Stream())

            async def __aexit__(self, *exc):
                return False

        return _Streams()

    # ``stdio_client`` is a *synchronous* factory returning an async context
    # manager that yields ``(read, write)``. It records the (real)
    # ``StdioServerParameters`` so tests can assert the command/args/env/cwd the
    # manager passed through. If the matched ``by_server`` entry carries a
    # ``"spawn_error"``, entering the CM raises it (a spawn failure).
    def fake_stdio_client(server, errlog=None):
        current["command"] = server.command
        current["url"] = None
        by_server.setdefault(server.command, {})["opened"] = server
        spawn_error = by_server[server.command].get("spawn_error")

        class _Streams:
            async def __aenter__(self):
                if spawn_error is not None:
                    raise spawn_error
                return (_Stream(), _Stream())

            async def __aexit__(self, *exc):
                return False

        return _Streams()

    # ``create_mcp_http_client`` is a *synchronous* factory returning an async
    # context manager — it is called without ``await`` in the manager.
    def fake_create_http_client(headers=None):
        return _Http(headers=headers)

    def fake_client_session(read, write, read_timeout_seconds=None):
        sess_cls, _ = _by_server_entry()["session"]
        return sess_cls(read, write, read_timeout_seconds=read_timeout_seconds)

    monkeypatch.setattr(mgr, "create_mcp_http_client", fake_create_http_client)
    monkeypatch.setattr(mgr, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(mgr, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mgr, "ClientSession", fake_client_session)
    return opened_http


def _spec(name, url, token_env=None):
    return McpServer(name=name, url=url, bearer_token_env=token_env)


def _stdio_spec(name, command, args=(), env=(), cwd=None):
    return McpServer(
        name=name,
        transport="stdio",
        command=command,
        args=tuple(args),
        env=tuple(env),
        cwd=cwd,
    )


# ===========================================================================
# required #1 — empty MCP_SERVERS / ENABLE_TOOLS=false → no connection
# ===========================================================================
def test_no_manager_when_no_servers():
    # The composition root guards: no servers → manager is None.
    import fibrecase_agent_backend.main as main

    src = open(main.__file__).read()
    assert "config.enable_tools and config.mcp_servers" in src


def test_manager_with_zero_servers_starts_and_closes_cleanly():
    mgr = McpManager([], connect_timeout_seconds=1.0, max_result_chars=100)
    import asyncio as _a
    _a.run(mgr.start())
    assert mgr.tools() == []
    assert mgr.status() == []
    assert mgr.total_tools == 0
    assert len(mgr) == 0


# ===========================================================================
# required #3 — two healthy servers: init, discovery, close order; built-ins kept
# ===========================================================================
async def test_two_healthy_servers_discover_and_order(monkeypatch):
    sess_a, calls_a = _make_session(
        list_result=_list_result(_tool_dict("get_weather"), _tool_dict("get_forecast"))
    )
    sess_b, calls_b = _make_session(list_result=_list_result(_tool_dict("search")))
    by_server = {
        "https://a.example/mcp": {"session": (sess_a, calls_a)},
        "https://b.example/mcp": {"session": (sess_b, calls_b)},
    }
    _patch(monkeypatch, by_server)

    mgr = McpManager(
        [_spec("alpha", "https://a.example/mcp"), _spec("beta", "https://b.example/mcp")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
    )
    # Simulate the built-ins already registered (get_current_time / echo / system_info).
    await mgr.start(existing_names={"get_current_time", "echo", "system_info"})

    assert calls_a["initialize"] == 1
    assert calls_a["list_tools"] == 1
    assert calls_b["initialize"] == 1
    assert calls_b["list_tools"] == 1

    names = [t.name for t in mgr.tools()]
    # Server order preserved; each server's tools in discovery order.
    assert names == ["mcp_alpha__get_weather", "mcp_alpha__get_forecast", "mcp_beta__search"]
    assert mgr.total_tools == 3

    # Close tears down both.
    await mgr.close()
    await mgr.close()  # idempotent
    assert mgr.status()[0]["available"] is False


# ===========================================================================
# required #4 — one server's connect/init/list failure doesn't block others
# ===========================================================================
async def test_connect_failure_isolated(monkeypatch):
    good, calls_good = _make_session(list_result=_list_result(_tool_dict("search")))
    bad_init, calls_bad = _make_session(init_result=Exception("boom"), list_result=_list_result(_tool_dict("x")))
    by_server = {
        "https://bad.example/mcp": {"session": (bad_init, calls_bad)},
        "https://good.example/mcp": {"session": (good, calls_good)},
    }
    _patch(monkeypatch, by_server)

    mgr = McpManager(
        [
            _spec("bad", "https://bad.example/mcp"),
            _spec("good", "https://good.example/mcp"),
        ],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
    )
    await mgr.start()

    status = {s["name"]: s for s in mgr.status()}
    assert status["bad"]["available"] is False
    assert status["good"]["available"] is True
    assert status["good"]["tool_count"] == 1
    # The healthy server's tools are the only ones exposed.
    assert [t.name for t in mgr.tools()] == ["mcp_good__search"]
    await mgr.close()


async def test_initialize_failure_maps_to_initialize_code(monkeypatch, caplog):
    import logging

    bad, _ = _make_session(init_result=Exception("proto"))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    with caplog.at_level(logging.WARNING, logger="mcp"):
        await mgr.start()
    state = mgr.status()[0]
    assert state["available"] is False
    # The stable code is logged (never the exception text).
    logged = {r.__dict__.get("code") for r in caplog.records}
    assert CODE_INITIALIZE_FAILED in logged
    await mgr.close()


async def test_list_failure_maps_to_discovery_code(monkeypatch, caplog):
    import logging

    bad, _ = _make_session(list_result=Exception("list boom"))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    with caplog.at_level(logging.WARNING, logger="mcp"):
        await mgr.start()
    assert mgr.status()[0]["available"] is False
    assert CODE_DISCOVERY_FAILED in {r.__dict__.get("code") for r in caplog.records}
    await mgr.close()


async def test_connect_timeout_maps_to_connect_code(monkeypatch, caplog):
    import logging

    # initialize() sleeps past the tiny timeout.
    class _SlowSession:
        def __init__(self, r, w, read_timeout_seconds=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            await asyncio.sleep(5)

    _patch(monkeypatch, {"https://slow.example/mcp": {"session": (_SlowSession, {})}})
    mgr = McpManager([_spec("slow", "https://slow.example/mcp")], connect_timeout_seconds=0.01, max_result_chars=100)
    with caplog.at_level(logging.WARNING, logger="mcp"):
        await mgr.start()
    assert mgr.status()[0]["available"] is False
    assert CODE_CONNECT_FAILED in {r.__dict__.get("code") for r in caplog.records}
    await mgr.close()


# A server whose handshake *leaks* an ``asyncio.CancelledError`` — e.g. the MCP
# SDK's own cancel scope unwinding mid-``initialize`` (``Cancelled via cancel
# scope …``) — must be treated as a plain connect failure, not allowed to abort
# the whole bot startup. Regression for the startup crash where a
# ``BaseException`` escaped ``_start_one`` because it is not an ``Exception``.
async def test_leaked_cancelled_error_degrades_to_unavailable(monkeypatch, caplog):
    import logging

    class _LeakingSession:
        def __init__(self, read, write, read_timeout_seconds=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            # Simulates the SDK leaking a bare CancelledError from within the
            # await (no outstanding cancel on our task — this is the internal case).
            raise asyncio.CancelledError()

    _patch(monkeypatch, {"https://leak.example/mcp": {"session": (_LeakingSession, {})}})
    mgr = McpManager([_spec("leak", "https://leak.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    with caplog.at_level(logging.WARNING, logger="mcp"):
        await mgr.start()  # must NOT raise
    assert mgr.status()[0]["available"] is False
    assert mgr.tools() == []
    assert mgr.total_tools == 0
    assert CODE_CONNECT_FAILED in {r.__dict__.get("code") for r in caplog.records}
    await mgr.close()


# The inverse guarantee: when the *process* is shutting down (our task is
# genuinely cancelled), the leaked-cancellation guard must **propagate** rather
# than swallow it — otherwise a real Ctrl-C during MCP startup would hang.
async def test_external_cancellation_during_start_propagates(monkeypatch):
    class _HangSession:
        def __init__(self, read, write, read_timeout_seconds=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            await asyncio.Event().wait()  # never returns; will be cancelled

    _patch(monkeypatch, {"https://hang.example/mcp": {"session": (_HangSession, {})}})
    mgr = McpManager([_spec("hang", "https://hang.example/mcp")], connect_timeout_seconds=30.0, max_result_chars=100)

    task = asyncio.create_task(mgr.start())
    await asyncio.sleep(0.05)  # let it enter the handshake
    task.cancel()  # genuine external cancellation (simulated shutdown)
    with pytest.raises(asyncio.CancelledError):
        await task
    # ``start()`` was aborted by the cancellation before it could register the
    # state, so the server must never appear as available.
    assert not any(s["available"] for s in mgr.status())
    await mgr.close()


# ===========================================================================
# required #5 — invalid schema/name or collision → whole server dropped
# ===========================================================================
async def test_invalid_remote_name_drops_whole_server(monkeypatch):
    # One tool has an illegal name → the *whole* server is rejected atomically.
    bad, _ = _make_session(list_result=_list_result(_tool_dict("ok_tool"), _tool_dict("bad name!")))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    assert mgr.status()[0]["available"] is False
    assert mgr.tools() == []
    await mgr.close()


async def test_invalid_schema_drops_whole_server(monkeypatch, caplog):
    import logging

    bad, _ = _make_session(list_result=_list_result(_tool_dict("x", schema={"type": "not-a-type"})))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    with caplog.at_level(logging.DEBUG, logger="mcp"):
        await mgr.start()
    assert mgr.status()[0]["available"] is False
    assert CODE_INVALID_TOOL in {r.__dict__.get("code") for r in caplog.records}
    await mgr.close()


async def test_missing_schema_drops_whole_server(monkeypatch):
    # A tool with no input_schema is invalid.
    bad, _ = _make_session(list_result=_list_result({"name": "x", "description": "d"}))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    assert mgr.status()[0]["available"] is False
    assert mgr.tools() == []
    await mgr.close()


async def test_duplicate_remote_name_drops_whole_server(monkeypatch):
    # A server lists the same tool twice → collision within the server.
    bad, _ = _make_session(list_result=_list_result(_tool_dict("dup"), _tool_dict("dup")))
    _patch(monkeypatch, {"https://bad.example/mcp": {"session": (bad, {})}})
    mgr = McpManager([_spec("bad", "https://bad.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    assert mgr.status()[0]["available"] is False
    await mgr.close()


async def test_one_bad_server_does_not_block_good(monkeypatch):
    good, _ = _make_session(list_result=_list_result(_tool_dict("ok")))
    bad, _ = _make_session(list_result=_list_result(_tool_dict("bad name!")))
    _patch(monkeypatch, {
        "https://good.example/mcp": {"session": (good, {})},
        "https://bad.example/mcp": {"session": (bad, {})},
    })
    mgr = McpManager(
        [_spec("good", "https://good.example/mcp"), _spec("bad", "https://bad.example/mcp")],
        connect_timeout_seconds=5.0,
        max_result_chars=100,
    )
    await mgr.start()
    assert [t.name for t in mgr.tools()] == ["mcp_good__ok"]
    status = {s["name"]: s for s in mgr.status()}
    assert status["good"]["available"] is True
    assert status["bad"]["available"] is False
    await mgr.close()


# ===========================================================================
# stdio transport — manager (faked stdio_client, no real subprocess)
# ===========================================================================
async def test_stdio_server_discovers_tools(monkeypatch):
    from mcp.client.stdio import StdioServerParameters

    sess, calls = _make_session(
        list_result=_list_result(_tool_dict("read_file"), _tool_dict("write_file"))
    )
    by_server = {"npx": {"session": (sess, calls)}}
    _patch(monkeypatch, by_server)

    mgr = McpManager(
        [_stdio_spec("fs", "npx", args=("-y", "@fs/server"), env=(("FOO", "bar"),), cwd="/tmp")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
    )
    await mgr.start()
    assert calls["initialize"] == 1
    assert calls["list_tools"] == 1
    assert [t.name for t in mgr.tools()] == ["mcp_fs__read_file", "mcp_fs__write_file"]
    assert mgr.total_tools == 2
    # The manager passed the operator-configured command/args/env/cwd through to
    # stdio_client (recorded by the fake); no http client was built for it.
    assert by_server["npx"]["opened"] == StdioServerParameters(
        command="npx", args=["-y", "@fs/server"], env={"FOO": "bar"}, cwd="/tmp"
    )
    await mgr.close()


async def test_stdio_and_http_mix_discover_in_order(monkeypatch):
    http_sess, _ = _make_session(list_result=_list_result(_tool_dict("search")))
    stdio_sess, _ = _make_session(list_result=_list_result(_tool_dict("local")))
    _patch(monkeypatch, {
        "https://a.example/mcp": {"session": (http_sess, {})},
        "python3": {"session": (stdio_sess, {})},
    })
    mgr = McpManager(
        [_spec("web", "https://a.example/mcp"), _stdio_spec("local", "python3")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
    )
    await mgr.start()
    assert [t.name for t in mgr.tools()] == ["mcp_web__search", "mcp_local__local"]
    await mgr.close()


async def test_stdio_spawn_failure_isolated(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="mcp")
    good, _ = _make_session(list_result=_list_result(_tool_dict("search")))
    _patch(monkeypatch, {
        "python3": {"session": (good, {}), "spawn_error": OSError("spawn failed")},
        "https://good.example/mcp": {"session": (good, {}), "spawn_error": None},
    })
    mgr = McpManager(
        [_stdio_spec("badproc", "python3"), _spec("good", "https://good.example/mcp")],
        connect_timeout_seconds=5.0,
        max_result_chars=1000,
    )
    await mgr.start()
    status = {s["name"]: s for s in mgr.status()}
    assert status["badproc"]["available"] is False
    assert status["good"]["available"] is True
    assert [t.name for t in mgr.tools()] == ["mcp_good__search"]
    # The failure is logged with a stable code + the exception class, never the command.
    assert CODE_CONNECT_FAILED in {r.__dict__.get("code") for r in caplog.records}
    assert "OSError" in {r.__dict__.get("exception") for r in caplog.records}
    logged = " ".join(str(r.getMessage()) for r in caplog.records)
    assert "python3" not in logged  # the command is never echoed
    await mgr.close()


async def test_stdio_status_has_only_safe_fields(monkeypatch):
    sess, _ = _make_session(list_result=_list_result(_tool_dict("x")))
    _patch(monkeypatch, {"npx": {"session": (sess, {})}})
    mgr = McpManager(
        [_stdio_spec("fs", "npx", args=("-y", "top-secret-arg"), env=(("K", "v"),), cwd="/tmp")],
        connect_timeout_seconds=5.0,
        max_result_chars=100,
    )
    await mgr.start()
    for entry in mgr.status():
        assert set(entry) == {"name", "available", "tool_count"}
    text = json.dumps(mgr.status())
    assert "npx" not in text  # no command
    assert "top-secret-arg" not in text  # no args
    assert "/tmp" not in text  # no cwd
    await mgr.close()


async def test_stdio_close_is_idempotent_and_tears_down(monkeypatch):
    calls = {"initialize": 0, "list_tools": 0, "call_tool": [], "exits": 0}

    class _Sess:
        def __init__(self, read, write, read_timeout_seconds=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            calls["exits"] += 1
            return False

        async def initialize(self):
            calls["initialize"] += 1
            return object()

        async def list_tools(self):
            calls["list_tools"] += 1
            return _list_result(_tool_dict("x"))

    _patch(monkeypatch, {"npx": {"session": (_Sess, calls)}})
    mgr = McpManager([_stdio_spec("fs", "npx")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    await mgr.close()
    assert calls["exits"] == 1
    await mgr.close()  # idempotent — no second teardown
    assert calls["exits"] == 1


# ===========================================================================
# bearer header from env (never stored/logged)
# ===========================================================================
async def test_bearer_header_read_from_env_at_build(monkeypatch):
    sess, _ = _make_session(list_result=_list_result(_tool_dict("ok")))
    opened = _patch(monkeypatch, {"https://a.example/mcp": {"session": (sess, {})}})
    monkeypatch.setenv("MCP_TOKEN_A", "super-secret-123")
    mgr = McpManager([_spec("alpha", "https://a.example/mcp", token_env="MCP_TOKEN_A")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    # The http client carried the bearer header from the env value.
    assert opened == [{"Authorization": "Bearer super-secret-123"}]
    await mgr.close()


async def test_no_bearer_header_when_no_token_env(monkeypatch):
    sess, _ = _make_session(list_result=_list_result(_tool_dict("ok")))
    opened = _patch(monkeypatch, {"https://a.example/mcp": {"session": (sess, {})}})
    mgr = McpManager([_spec("alpha", "https://a.example/mcp")], connect_timeout_seconds=5.0, max_result_chars=100)
    await mgr.start()
    assert opened == [None]
    await mgr.close()


# ===========================================================================
# required #6 — a pre-2.5 DB needs no migration; no unexpected tables
# ===========================================================================
def test_phase4_adds_no_new_db_table():
    # The MCP tool *provider* itself persists no table. Phase 4.x (user-level
    # OAuth) is the only thing that added tables since: ``oauth_credentials``
    # and ``oauth_authorization_states``. ``create_all`` adds them to a fresh
    # DB *and* to a pre-4.x DB, so nothing else here changes.
    from fibrecase_agent_backend.database.models import Base

    tables = set(Base.metadata.tables)
    assert tables == {
        "conversations",
        "messages",
        "attachments",
        "memories",
        "tool_audit_events",
        "oauth_credentials",
        "oauth_authorization_states",
    }


# ===========================================================================
# status() is scope-free and never carries URL/token/error detail
# ===========================================================================
async def test_status_has_only_safe_fields(monkeypatch):
    good, _ = _make_session(list_result=_list_result(_tool_dict("a"), _tool_dict("b")))
    bad, _ = _make_session(init_result=Exception("boom"))
    _patch(monkeypatch, {
        "https://good.example/mcp": {"session": (good, {})},
        "https://bad.example/mcp": {"session": (bad, {})},
    })
    mgr = McpManager(
        [_spec("good", "https://good.example/mcp"), _spec("bad", "https://bad.example/mcp")],
        connect_timeout_seconds=5.0,
        max_result_chars=100,
    )
    await mgr.start()
    for entry in mgr.status():
        assert set(entry) == {"name", "available", "tool_count"}
    text = json.dumps(mgr.status())
    assert "example" not in text  # no URL/host
    assert "boom" not in text  # no exception detail
    await mgr.close()
