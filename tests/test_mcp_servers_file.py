"""``MCP_SERVERS_FILE`` — externalizing the MCP servers JSON into a separate file.

The server list can live in a standalone JSON *array* file (``MCP_SERVERS_FILE``,
the preferred source for multiple / stdio servers) instead of the inline
``MCP_SERVERS`` string. This file tests the **source-selection** behaviour and
that a file-configured server reuses the *exact same* strict validation as the
inline path. All servers here are validated-only (no ``McpManager`` is built, so
no connection / subprocess is opened); files are written to ``tmp_path``. The
repo's autouse ``_no_dotenv`` fixture neutralizes ``load_dotenv`` so a real
``.env`` can't leak into these assertions.
"""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend.config import ConfigError, McpServer, load_config


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
    for knob in ("MCP_SERVERS", "MCP_SERVERS_FILE", "MCP_ALLOW_INSECURE_HTTP",
                 "MCP_CONNECT_TIMEOUT_SECONDS", "MAX_MCP_TOOL_RESULT_CHARS"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


# ===========================================================================
# source selection / precedence
# ===========================================================================
def test_file_is_parsed(monkeypatch, tmp_path):
    f = tmp_path / "servers.json"
    _write(f, json.dumps([{"name": "alpha", "url": "https://a.example/mcp"}]))
    cfg = _load(monkeypatch, MCP_SERVERS_FILE=str(f))
    assert len(cfg.mcp_servers) == 1
    assert cfg.mcp_servers[0] == McpServer(name="alpha", url="https://a.example/mcp")


def test_file_wins_over_inline(monkeypatch, tmp_path):
    # Both set → the file is the structured source of truth; inline is ignored
    # (not an error), so a stale inline value can't surprise the operator.
    f = tmp_path / "servers.json"
    _write(f, json.dumps([{"name": "fromfile", "url": "https://f.example/mcp"}]))
    cfg = _load(
        monkeypatch,
        MCP_SERVERS=json.dumps([{"name": "inline", "url": "https://i.example/mcp"}]),
        MCP_SERVERS_FILE=str(f),
    )
    assert [s.name for s in cfg.mcp_servers] == ["fromfile"]


def test_file_parses_mixed_http_and_stdio(monkeypatch, tmp_path):
    f = tmp_path / "servers.json"
    _write(
        f,
        json.dumps(
            [
                {"name": "web", "url": "https://a.example/mcp"},
                {
                    "name": "fs",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"FOO": "bar"},
                    "cwd": "/tmp",
                },
            ]
        ),
    )
    cfg = _load(monkeypatch, MCP_SERVERS_FILE=str(f))
    assert [s.name for s in cfg.mcp_servers] == ["web", "fs"]
    assert cfg.mcp_servers[0].transport == "http"
    assert cfg.mcp_servers[1].transport == "stdio"
    assert cfg.mcp_servers[1].command == "npx"
    assert cfg.mcp_servers[1].env == (("FOO", "bar"),)
    assert cfg.mcp_servers[1].cwd == "/tmp"


def test_inline_still_works_when_file_unset(monkeypatch):
    # Regression: with no MCP_SERVERS_FILE the inline MCP_SERVERS path is
    # byte-for-byte the original behaviour.
    cfg = _load(monkeypatch, MCP_SERVERS=json.dumps([{"name": "alpha", "url": "https://a.example/mcp"}]))
    assert [s.name for s in cfg.mcp_servers] == ["alpha"]


def test_both_unset_is_empty(monkeypatch):
    assert _load(monkeypatch).mcp_servers == ()


def test_empty_file_array_means_no_servers(monkeypatch, tmp_path):
    # An explicit [] is valid and means "no servers" (distinct from a 0-byte file).
    f = tmp_path / "servers.json"
    _write(f, "[]")
    assert _load(monkeypatch, MCP_SERVERS_FILE=str(f)).mcp_servers == ()


# ===========================================================================
# file errors are a ConfigError (never a silent drop)
# ===========================================================================
def test_missing_file_is_config_error(monkeypatch, tmp_path):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(tmp_path / "does-not-exist.json"))


def test_unreadable_file_is_config_error(monkeypatch, tmp_path):
    # Point MCP_SERVERS_FILE at a *directory*: read_text raises IsADirectoryError
    # (an OSError) → ConfigError, never a crash.
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(tmp_path))


def test_blank_file_is_config_error(monkeypatch, tmp_path):
    # A set-but-empty (0-byte / whitespace-only) file must NOT silently disable
    # servers — it's a ConfigError naming the path.
    for text in ("", "   \n  "):
        f = tmp_path / "blank.json"
        _write(f, text)
        with pytest.raises(ConfigError):
            _load(monkeypatch, MCP_SERVERS_FILE=str(f))


def test_file_invalid_json_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    _write(f, "not json [")
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(f))


def test_file_not_array_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "obj.json"
    _write(f, json.dumps({"name": "alpha", "url": "https://a.example/mcp"}))
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(f))


def test_file_bad_entry_is_config_error(monkeypatch, tmp_path):
    # The *same* strict per-entry validation applies to a file-configured server:
    # a stdio entry with no command is rejected exactly as it would be inline.
    f = tmp_path / "badentry.json"
    _write(f, json.dumps([{"name": "fs", "transport": "stdio"}]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(f))


def test_file_duplicate_name_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "dup.json"
    _write(f, json.dumps([{"name": "a", "url": "https://1.example"}, {"name": "a", "url": "https://2.example"}]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(f))


# ===========================================================================
# secret handling is unchanged: the bearer token value is resolved from the
# process environment, never stored on the spec, even via the file path.
# ===========================================================================
def test_file_bearer_token_env_resolved_from_process_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_TOKEN_FILE", "supersecret-file-value")
    f = tmp_path / "servers.json"
    _write(f, json.dumps([{"name": "beta", "url": "https://b.example/mcp", "bearer_token_env": "MCP_TOKEN_FILE"}]))
    cfg = _load(monkeypatch, MCP_SERVERS_FILE=str(f))
    assert cfg.mcp_servers[0].bearer_token_env == "MCP_TOKEN_FILE"
    # Only the env-var *name* is stored — the value is never on the spec.
    assert "supersecret-file-value" not in str(cfg.mcp_servers)


def test_file_bearer_token_env_missing_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "servers.json"
    _write(f, json.dumps([{"name": "beta", "url": "https://b.example/mcp", "bearer_token_env": "MCP_UNSET_VAR"}]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_SERVERS_FILE=str(f))
