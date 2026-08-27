"""``MCP_PERMISSIONS_FILE`` — the dedicated, backend-maintained tool-permission file.

Covers the whole lifecycle, all in ``tmp_path`` / in-memory (no network, no MCP):

* the **seed/sync merge** (``merge_permissions``) — new tool → unfilled, filled
  entry for a vanished tool kept, unfilled entry for a vanished tool dropped,
  deterministic order, idempotent;
* strict **parsing** (``parse_permissions_json`` / ``load_permissions_file``) —
  valid, ``[]``, missing → ``[]``, blank → ``[]``, and each violation →
  ``PermissionsFileError``;
* **serialisation** (``serialize``) — byte-identical round-trip, and the atomic
  write skipping a byte-identical no-op;
* **config** (``load_config``) — path captured as a ``Path``; unset → ``None``;
  a present-but-malformed file + tools enabled → ``ConfigError`` (fail-to-start);
  a malformed file with tools disabled → no error; a missing/blank file → no error;
* the **hot-reload** wrapper (``FileBackedToolPolicy``) — missing file → defaults;
  a written ``deny`` → withheld next call; a rewrite to ``""`` → back to the tool
  default; a corrupt write after a good state → last-good held, no crash; an
  unchanged file → no rebuild.

The repo's autouse ``_no_dotenv`` fixture neutralizes ``load_dotenv`` so a real
``.env`` can't leak into the config assertions.
"""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend.config import ConfigError, load_config
from fibrecase_agent_backend.tools import (
    FileBackedToolPolicy,
    PermissionsFileError,
    ToolPermission,
    build_default_tools,
    load_permissions_file,
    merge_permissions,
    parse_permissions_json,
    reconcile_permissions_file,
    serialize,
    atomic_write,
)
from fibrecase_agent_backend.tools.base import Tool
from fibrecase_agent_backend.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# helpers
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
    for knob in ("MCP_PERMISSIONS_FILE", "TOOL_APPROVAL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _registry_with_mcp_tool() -> ToolRegistry:
    """The built-ins (declared defaults: get_current_time/echo = allow,
    system_info = ask) plus one MCP-style tool that defaults to ``ask``."""
    reg = build_default_tools()

    class _Mcp(Tool):
        name = "mcp_alpha__x"
        description = "d"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments):  # pragma: no cover
            return ""

    reg.register(_Mcp())
    return reg


# ===========================================================================
# merge (seed/sync) — the backend → file direction
# ===========================================================================
def test_merge_new_tool_appears_unfilled():
    merged = merge_permissions([], ["mcp_a__x", "mcp_b__y"])
    assert merged == [
        {"tool": "mcp_a__x", "permission": ""},
        {"tool": "mcp_b__y", "permission": ""},
    ]


def test_merge_preserves_existing_permission():
    existing = [{"tool": "mcp_a__x", "permission": "deny"}]
    merged = merge_permissions(existing, ["mcp_a__x", "mcp_b__y"])
    assert merged == [
        {"tool": "mcp_a__x", "permission": "deny"},
        {"tool": "mcp_b__y", "permission": ""},
    ]


def test_merge_filled_orphan_for_vanished_tool_is_kept():
    # A tool the operator filled (``deny``) that no longer exists survives the
    # sync — it is *filled*, so it must never be pruned.
    existing = [{"tool": "mcp_gone__z", "permission": "deny"}]
    merged = merge_permissions(existing, ["mcp_a__x"])
    assert merged == [
        {"tool": "mcp_a__x", "permission": ""},
        {"tool": "mcp_gone__z", "permission": "deny"},
    ]


def test_merge_unfilled_orphan_for_vanished_tool_is_dropped():
    # A ``""`` (unfilled) entry for a tool that no longer exists is pruned.
    existing = [{"tool": "mcp_gone__z", "permission": ""}]
    merged = merge_permissions(existing, ["mcp_a__x"])
    assert merged == [{"tool": "mcp_a__x", "permission": ""}]


def test_merge_orphans_are_sorted_by_tool():
    existing = [
        {"tool": "mcp_z__q", "permission": "ask"},
        {"tool": "mcp_a__p", "permission": "deny"},
    ]
    merged = merge_permissions(existing, ["mcp_m__mid"])
    assert merged == [
        {"tool": "mcp_m__mid", "permission": ""},
        {"tool": "mcp_a__p", "permission": "deny"},
        {"tool": "mcp_z__q", "permission": "ask"},
    ]


def test_merge_is_idempotent():
    once = merge_permissions(
        [{"tool": "mcp_a__x", "permission": "deny"}, {"tool": "mcp_gone__z", "permission": "allow"}],
        ["mcp_a__x", "mcp_b__y"],
    )
    twice = merge_permissions(once, ["mcp_a__x", "mcp_b__y"])
    assert once == twice


def test_reconcile_seeds_a_missing_file(tmp_path):
    path = tmp_path / "perm.json"
    reconcile_permissions_file(path, ["mcp_a__x", "mcp_b__y"])
    assert path.exists()
    entries = parse_permissions_json(path.read_text(encoding="utf-8"))
    assert entries == [
        {"tool": "mcp_a__x", "permission": ""},
        {"tool": "mcp_b__y", "permission": ""},
    ]


# ===========================================================================
# parsing — the read side
# ===========================================================================
def test_parse_valid_array():
    text = json.dumps(
        [
            {"tool": "mcp_a__x", "permission": "deny"},
            {"tool": "mcp_b__y", "permission": ""},
            {"tool": "mcp_c__z"},  # permission absent → normalized to ""
        ]
    )
    assert parse_permissions_json(text) == [
        {"tool": "mcp_a__x", "permission": "deny"},
        {"tool": "mcp_b__y", "permission": ""},
        {"tool": "mcp_c__z", "permission": ""},
    ]


def test_parse_empty_array():
    assert parse_permissions_json("[]") == []


def test_parse_all_legal_permission_values():
    for perm in ("allow", "ask", "deny", ""):
        assert parse_permissions_json(json.dumps([{"tool": "mcp_a__x", "permission": perm}])) == [
            {"tool": "mcp_a__x", "permission": perm}
        ]


@pytest.mark.parametrize("text", ["not json [", "{nope", "42", "null", '"a string"', "true"])
def test_parse_invalid_structure_raises(text):
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(text)


def test_parse_non_object_entry_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([["mcp_a__x"], "deny"]))


def test_parse_missing_tool_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"permission": "deny"}]))


def test_parse_non_string_tool_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"tool": 5, "permission": "deny"}]))


def test_parse_bad_tool_name_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"tool": "bad name", "permission": "deny"}]))


@pytest.mark.parametrize("perm", ["maybe", "ALLOW", "Allow", 5, None, "deny;drop table"])
def test_parse_bad_permission_raises(perm):
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"tool": "mcp_a__x", "permission": perm}]))


def test_parse_unknown_field_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"tool": "mcp_a__x", "permission": "deny", "extra": 1}]))


def test_parse_duplicate_tool_raises():
    with pytest.raises(PermissionsFileError):
        parse_permissions_json(json.dumps([{"tool": "mcp_a__x", "permission": "deny"}, {"tool": "mcp_a__x", "permission": "allow"}]))


def test_load_missing_file_is_empty(tmp_path):
    assert load_permissions_file(tmp_path / "absent.json") == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n"])
def test_load_blank_file_is_empty(tmp_path, text):
    f = tmp_path / "blank.json"
    _write(f, text)
    assert load_permissions_file(f) == []


def test_load_valid_file(tmp_path):
    f = tmp_path / "perm.json"
    _write(f, json.dumps([{"tool": "mcp_a__x", "permission": "deny"}]))
    assert load_permissions_file(f) == [{"tool": "mcp_a__x", "permission": "deny"}]


def test_load_present_but_malformed_raises(tmp_path):
    f = tmp_path / "bad.json"
    _write(f, "not json [")
    with pytest.raises(PermissionsFileError):
        load_permissions_file(f)


# ===========================================================================
# serialization — canonical form + atomic write skip
# ===========================================================================
def test_serialize_round_trip_is_byte_identical():
    entries = [
        {"tool": "mcp_b__y", "permission": ""},
        {"tool": "mcp_a__x", "permission": "deny"},
    ]
    text = serialize(entries)
    # parse → re-serialize must be byte-for-byte identical (the compare key).
    assert serialize(parse_permissions_json(text)) == text


def test_serialize_sorted_and_stable():
    entries = [{"tool": "mcp_a__x", "permission": "deny"}]
    assert serialize(entries) == serialize(entries)


def test_atomic_write_skips_when_identical(tmp_path):
    p = tmp_path / "perm.json"
    text = serialize([{"tool": "mcp_a__x", "permission": ""}])
    _write(p, text)
    before = (p.stat().st_mtime_ns, p.stat().st_size)
    assert atomic_write(p, text) is False  # byte-identical → no write
    assert (p.stat().st_mtime_ns, p.stat().st_size) == before
    assert p.read_text(encoding="utf-8") == text


def test_atomic_write_writes_when_changed_or_missing(tmp_path):
    p = tmp_path / "perm.json"
    assert atomic_write(p, "[]\n") is True  # first write (file absent)
    other = serialize([{"tool": "mcp_a__x", "permission": "deny"}])
    assert atomic_write(p, other) is True  # content differs → rewrite
    assert p.read_text(encoding="utf-8") == other


# ===========================================================================
# config — MCP_PERMISSIONS_FILE wiring
# ===========================================================================
def test_config_no_file_by_default(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.mcp_permissions_file is None


def test_config_captures_file_path(monkeypatch, tmp_path):
    f = tmp_path / "perm.json"
    _write(f, "[]")
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f))
    assert cfg.mcp_permissions_file == f


def test_config_set_but_missing_file_is_fine(monkeypatch, tmp_path):
    # A set-but-missing file is not an error — it is seeded at startup.
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(tmp_path / "absent.json"))
    assert cfg.mcp_permissions_file == tmp_path / "absent.json"


def test_config_set_but_blank_file_is_fine(monkeypatch, tmp_path):
    # A set-but-blank (0-byte) file is not an error — it means "no overrides".
    f = tmp_path / "blank.json"
    _write(f, "")
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f))
    assert cfg.mcp_permissions_file == f


def test_config_present_malformed_file_with_tools_enabled_is_error(monkeypatch, tmp_path):
    # Fail-to-start: a present, non-blank, malformed file with tools enabled.
    f = tmp_path / "bad.json"
    _write(f, "not json [")
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f))


def test_config_malformed_entry_with_tools_enabled_is_error(monkeypatch, tmp_path):
    # Not just bad JSON — a structurally invalid entry also fails to start.
    f = tmp_path / "bad.json"
    _write(f, json.dumps([{"tool": "mcp_a__x", "permission": "maybe"}]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f))


def test_config_malformed_file_with_tools_disabled_is_not_error(monkeypatch, tmp_path):
    # With ENABLE_TOOLS=false there is no policy to enforce, so a malformed file
    # is not validated — no ConfigError (the file is still captured as a path).
    f = tmp_path / "bad.json"
    _write(f, "not json [")
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f), ENABLE_TOOLS="false")
    assert cfg.mcp_permissions_file == f
    assert cfg.enable_tools is False


def test_config_valid_file_with_tools_enabled_is_fine(monkeypatch, tmp_path):
    f = tmp_path / "perm.json"
    _write(f, json.dumps([{"tool": "mcp_a__x", "permission": "deny"}]))
    cfg = _load(monkeypatch, MCP_PERMISSIONS_FILE=str(f))
    assert cfg.mcp_permissions_file == f


# ===========================================================================
# hot-reload wrapper — FileBackedToolPolicy
# ===========================================================================
def test_wrapper_missing_file_uses_declared_defaults(tmp_path):
    reg = _registry_with_mcp_tool()
    policy = FileBackedToolPolicy(tmp_path / "absent.json", reg)
    # The MCP tool resolves to its declared default (ask); built-ins keep theirs.
    assert policy.resolve("mcp_alpha__x") is ToolPermission.ASK
    assert policy.resolve("echo") is ToolPermission.ALLOW
    assert policy.resolve("system_info") is ToolPermission.ASK


def test_wrapper_writes_deny_next_call_withholds_it(tmp_path):
    reg = _registry_with_mcp_tool()
    path = tmp_path / "perm.json"
    policy = FileBackedToolPolicy(path, reg)
    assert policy.resolve("mcp_alpha__x") is ToolPermission.ASK  # initial, no file
    _write(path, json.dumps([{"tool": "mcp_alpha__x", "permission": "deny"}]))
    # The *next* consultation sees the deny without a restart.
    assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY
    known = {"mcp_alpha__x", "echo", "get_current_time", "system_info"}
    assert "mcp_alpha__x" not in policy.advertised_names(known)
    assert "echo" in policy.advertised_names(known)


def test_wrapper_rewrite_to_empty_returns_to_default(tmp_path):
    reg = _registry_with_mcp_tool()
    path = tmp_path / "perm.json"
    _write(path, json.dumps([{"tool": "mcp_alpha__x", "permission": "deny"}]))
    policy = FileBackedToolPolicy(path, reg)
    assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY
    # Rewrite to unfilled (""): the override is dropped, back to the default ask.
    # The body differs in length from the "deny" form, so the (mtime, size) key
    # changes and the reload fires.
    _write(path, json.dumps([{"tool": "mcp_alpha__x", "permission": ""}]))
    assert policy.resolve("mcp_alpha__x") is ToolPermission.ASK


def test_wrapper_corrupt_after_good_keeps_last_good(tmp_path, caplog):
    import logging

    reg = _registry_with_mcp_tool()
    path = tmp_path / "perm.json"
    _write(path, json.dumps([{"tool": "mcp_alpha__x", "permission": "deny"}]))
    policy = FileBackedToolPolicy(path, reg)
    assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY
    # Corrupt the file at runtime: the last-good policy holds, no crash.
    _write(path, "not json [")
    with caplog.at_level(logging.WARNING, logger="fibrecase_agent_backend.tools.policy"):
        assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY
    # The warning fired, and names only the path (never the file contents).
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    assert all("not json" not in r.getMessage() for r in warnings)


def test_wrapper_unchanged_file_does_not_rebuild(tmp_path, monkeypatch):
    import fibrecase_agent_backend.tools.policy as policy_mod

    reg = _registry_with_mcp_tool()
    path = tmp_path / "perm.json"
    _write(path, json.dumps([{"tool": "mcp_alpha__x", "permission": "deny"}]))
    policy = FileBackedToolPolicy(path, reg)

    calls = {"n": 0}
    real = policy_mod.load_permissions_file

    def counting(path_):
        calls["n"] += 1
        return real(path_)

    monkeypatch.setattr(policy_mod, "load_permissions_file", counting)

    assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY  # initial load
    first = calls["n"]
    assert first == 1
    assert policy.resolve("mcp_alpha__x") is ToolPermission.DENY  # unchanged → no reload
    assert calls["n"] == first  # the read was NOT re-run


def test_wrapper_repr_is_path_only(tmp_path):
    reg = _registry_with_mcp_tool()
    path = tmp_path / "perm.json"
    policy = FileBackedToolPolicy(path, reg)
    assert "perm.json" in repr(policy)
