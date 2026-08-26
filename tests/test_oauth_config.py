"""Phase 4.x — OAuth configuration parsing (``OAUTH_*`` + MCP ``authentication``).

Proves the strict, fail-fast startup validation: the callback base URL is an
absolute bare origin (no path/query/userinfo/trailing slash), the port and state
TTL are positive, and each MCP server's optional ``authentication`` object is
limited to ``{type: none|oauth, provider}`` with ``oauth`` requiring a provider
and forbidding a concurrent operator ``bearer_token_env``. Nothing here touches
the network.
"""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config


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
        "OAUTH_CALLBACK_BASE_URL",
        "OAUTH_CALLBACK_PORT",
        "OAUTH_STATE_TTL_SECONDS",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_SCOPES",
    ):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


# ---------------------------------------------------------------------------
# OAUTH_* knobs
# ---------------------------------------------------------------------------
def test_oauth_defaults_when_unset(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.oauth_callback_base_url is None
    assert cfg.oauth_callback_port == 8090
    assert cfg.oauth_state_ttl_seconds == 600.0


def test_oauth_parses_callback_base(monkeypatch):
    cfg = _load(monkeypatch, OAUTH_CALLBACK_BASE_URL="https://ex.com", OAUTH_CALLBACK_PORT="9001")
    assert cfg.oauth_callback_base_url == "https://ex.com"
    assert cfg.oauth_callback_port == 9001


def test_oauth_accepts_http_callback_base(monkeypatch):
    # A trusted local/private endpoint may be http (the operator's own base).
    cfg = _load(monkeypatch, OAUTH_CALLBACK_BASE_URL="http://127.0.0.1:8090")
    assert cfg.oauth_callback_base_url == "http://127.0.0.1:8090"


@pytest.mark.parametrize(
    "base",
    [
        "https://ex.com/",  # trailing slash
        "https://ex.com/oauth",  # a path
        "https://ex.com/?x=1",  # a query
        "https://ex.com#f",  # a fragment
        "https://user:pw@ex.com",  # userinfo
        "https://",  # no host
        "ex.com",  # no scheme
        "ftp://ex.com",  # bad scheme
    ],
)
def test_oauth_rejects_bad_callback_base(monkeypatch, base):
    with pytest.raises(ConfigError):
        _load(monkeypatch, OAUTH_CALLBACK_BASE_URL=base)


def test_oauth_rejects_nonpositive_state_ttl(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, OAUTH_CALLBACK_BASE_URL="https://ex.com", OAUTH_STATE_TTL_SECONDS="0")


def test_oauth_rejects_bad_port(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, OAUTH_CALLBACK_BASE_URL="https://ex.com", OAUTH_CALLBACK_PORT="70000")


def test_oauth_state_ttl_positive_accepted(monkeypatch):
    cfg = _load(monkeypatch, OAUTH_CALLBACK_BASE_URL="https://ex.com", OAUTH_STATE_TTL_SECONDS="120")
    assert cfg.oauth_state_ttl_seconds == 120.0


# ---------------------------------------------------------------------------
# MCP server ``authentication`` metadata
# ---------------------------------------------------------------------------
def _one(monkeypatch, obj):
    return _load(monkeypatch, MCP_SERVERS=json.dumps([obj])).mcp_servers[0]


def test_mcp_authentication_none_is_default(monkeypatch):
    s = _one(monkeypatch, {"name": "alpha", "url": "https://a.example/mcp"})
    assert s.auth_type == "none"
    assert s.auth_provider is None


def test_mcp_authentication_oauth_with_provider(monkeypatch):
    s = _one(
        monkeypatch,
        {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth", "provider": "google"}},
    )
    assert s.auth_type == "oauth"
    assert s.auth_provider == "google"


def test_mcp_authentication_explicit_none(monkeypatch):
    s = _one(
        monkeypatch,
        {"name": "alpha", "url": "https://a.example/mcp", "authentication": {"type": "none"}},
    )
    assert s.auth_type == "none"
    assert s.auth_provider is None


def test_mcp_authentication_none_with_provider_rejected(monkeypatch):
    with pytest.raises(ConfigError):
        _one(monkeypatch, {"name": "alpha", "url": "https://a.example/mcp", "authentication": {"type": "none", "provider": "google"}})


def test_mcp_authentication_oauth_requires_provider(monkeypatch):
    with pytest.raises(ConfigError):
        _one(monkeypatch, {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth"}})


def test_mcp_authentication_bad_provider_rejected(monkeypatch):
    for bad in ("Google", "1google", "a b", "", "x" * 33):
        with pytest.raises(ConfigError):
            _one(
                monkeypatch,
                {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth", "provider": bad}},
            )


def test_mcp_authentication_unknown_field_rejected(monkeypatch):
    with pytest.raises(ConfigError):
        _one(
            monkeypatch,
            {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth", "provider": "google", "extra": 1}},
        )


def test_mcp_authentication_must_be_object(monkeypatch):
    with pytest.raises(ConfigError):
        _one(monkeypatch, {"name": "gcal", "url": "https://g.example/mcp", "authentication": "oauth"})


def test_mcp_authentication_oauth_and_bearer_are_mutually_exclusive(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_A", "secret-a")
    with pytest.raises(ConfigError):
        _one(
            monkeypatch,
            {
                "name": "gcal",
                "url": "https://g.example/mcp",
                "bearer_token_env": "MCP_TOKEN_A",
                "authentication": {"type": "oauth", "provider": "google"},
            },
        )


def test_mcp_authentication_oauth_server_keeps_no_bearer(monkeypatch):
    s = _one(
        monkeypatch,
        {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth", "provider": "google"}},
    )
    assert s.bearer_token_env is None


def test_multiple_servers_one_oauth_one_none(monkeypatch):
    cfg = _load(
        monkeypatch,
        MCP_SERVERS=json.dumps(
            [
                {"name": "alpha", "url": "https://a.example/mcp"},
                {"name": "gcal", "url": "https://g.example/mcp", "authentication": {"type": "oauth", "provider": "google"}},
            ]
        ),
    )
    assert cfg.mcp_servers[0].auth_type == "none"
    assert cfg.mcp_servers[1].auth_type == "oauth"
