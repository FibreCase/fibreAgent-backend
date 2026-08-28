"""Phase 5.1 — read-only infrastructure observation: config validation (required #2).

Everything is local: no SSH connection, no network, no subprocess. The
``INFRA_SSH_TARGETS`` JSON array — read from the default
``config/infra_ssh_targets.json`` when that file is present, from the explicit
``INFRA_SSH_TARGETS_FILE`` when set (which wins over both), or from the inline
``INFRA_SSH_TARGETS`` when no file is present — and the two numeric knobs are
parsed and strictly validated at startup. The key / known_hosts *files* are
checked to exist (so a botched secret mount fails fast), but no bytes are ever
read from them. Error messages name the target (or its index) and the field —
**never** a host, a key path, a known_hosts path, or a mount path (operator
secret-adjacent values); a service/unit *name* is the one thing that may be
echoed.
"""

from __future__ import annotations

import json

import pytest

from fibrecase_agent_backend import config as _config_module
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
        "INFRA_SSH_TARGETS",
        "INFRA_SSH_TARGETS_FILE",
        "INFRA_SSH_CONNECT_TIMEOUT_SECONDS",
        "MAX_INFRA_TOOL_RESULT_CHARS",
        "TOOL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def _mkfiles(tmp_path):
    """Write a fake private key + a non-empty known_hosts; return their paths."""
    key = tmp_path / "id_ed25519"
    key.write_text("FAKE-PRIVATE-KEY")
    kh = tmp_path / "known_hosts"
    kh.write_text("nas.local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA")
    return str(key), str(kh)


def _target(key, kh, **over):
    d = {
        "name": "nas",
        "host": "nas.local",
        "port": 22,
        "username": "probe",
        "private_key_path": key,
        "known_hosts_path": kh,
        "mounts": ["/volume1"],
        "services": ["ssh.service"],
    }
    d.update(over)
    return d


def _load_target(monkeypatch, key, kh, **over):
    """Set a single-target INFRA_SSH_TARGETS and load; returns Config or raises."""
    return _load(monkeypatch, INFRA_SSH_TARGETS=json.dumps([_target(key, kh, **over)]))


# ===========================================================================
# empty / defaults
# ===========================================================================
def test_infra_empty_is_empty_tuple_and_defaults(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.infra_ssh_targets == ()
    assert cfg.infra_ssh_connect_timeout_seconds == 10.0
    assert cfg.max_infra_tool_result_chars == 8000


def test_infra_blank_is_empty(monkeypatch):
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS="   ")
    assert cfg.infra_ssh_targets == ()


def test_infra_explicit_empty_array_is_no_targets(monkeypatch):
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS="[]")
    assert cfg.infra_ssh_targets == ()


# ===========================================================================
# valid parsing
# ===========================================================================
def test_infra_parses_valid_target(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    cfg = _load_target(monkeypatch, key, kh)
    assert len(cfg.infra_ssh_targets) == 1
    t = cfg.infra_ssh_targets[0]
    assert t.name == "nas"
    assert t.host == "nas.local"
    assert t.port == 22
    assert t.username == "probe"
    assert t.private_key_path == key
    assert t.known_hosts_path == kh
    assert t.mounts == ("/volume1",)
    assert t.services == ("ssh.service",)


def test_infra_parses_multiple_targets(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    cfg = _load(
        monkeypatch,
        INFRA_SSH_TARGETS=json.dumps(
            [_target(key, kh, name="nas"), _target(key, kh, name="pi", host="pi.local", mounts=["/data"])]
        ),
    )
    assert [t.name for t in cfg.infra_ssh_targets] == ["nas", "pi"]


@pytest.mark.parametrize(
    "host",
    ["nas.local", "10.0.0.5", "2001:db8::1", "[2001:db8::1]", "host.internal.example"],
)
def test_infra_accepts_safe_hosts(monkeypatch, tmp_path, host):
    key, kh = _mkfiles(tmp_path)
    cfg = _load_target(monkeypatch, key, kh, host=host)
    assert cfg.infra_ssh_targets[0].host == host


def test_infra_mounts_and_services_dedup_preserving_order(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    cfg = _load_target(monkeypatch, key, kh, mounts=["/a", "/b", "/a"], services=["x.service", "x.service"])
    assert cfg.infra_ssh_targets[0].mounts == ("/a", "/b")
    assert cfg.infra_ssh_targets[0].services == ("x.service",)


# ===========================================================================
# structural rejections
# ===========================================================================
def test_infra_rejects_invalid_json(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS="not json [")


def test_infra_rejects_non_array(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS=json.dumps(_target(key, kh)))


def test_infra_rejects_non_object_entry(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS='["nas"]')


@pytest.mark.parametrize(
    "name",
    ["Nas", "1nas", "a" * 33, "has space", "a.b", "", "a/b"],
)
def test_infra_rejects_bad_name(monkeypatch, tmp_path, name):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, name=name)


def test_infra_rejects_duplicate_name(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            INFRA_SSH_TARGETS=json.dumps([_target(key, kh, name="nas"), _target(key, kh, name="nas")]),
        )


@pytest.mark.parametrize(
    "field",
    ["password", "password_env", "agent", "forwarding", "sftp", "command", "bogus"],
)
def test_infra_rejects_unknown_field(monkeypatch, tmp_path, field):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, **{field: "x"})


def test_infra_rejects_too_many_targets(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load(
            monkeypatch,
            INFRA_SSH_TARGETS=json.dumps([_target(key, kh, name=f"t{i}") for i in range(17)]),
        )


# ===========================================================================
# host validation (never echoed)
# ===========================================================================
@pytest.mark.parametrize(
    "host",
    ["", " ", "nas .local", "user@nas.local", "nas.local:22", "/etc/hosts", ".hidden", "nas-", "-nas", "a.b.c.d.e.f", "[10.0.0.5]"],
)
def test_infra_rejects_unsafe_host(monkeypatch, tmp_path, host):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, host=host)


def test_infra_host_error_never_echoes_host(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    secret_host = "totally-secret-host-XYZ"
    with pytest.raises(ConfigError) as exc:
        _load_target(monkeypatch, key, kh, host=secret_host + ":22")
    assert secret_host not in str(exc.value)
    assert "host" in str(exc.value)


# ===========================================================================
# port / username
# ===========================================================================
@pytest.mark.parametrize("port", [0, 65536, "22", 22.0, True, None, -1])
def test_infra_rejects_bad_port(monkeypatch, tmp_path, port):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, port=port)


@pytest.mark.parametrize("username", ["", "9user", "has space", "a" * 33, "user name"])
def test_infra_rejects_bad_username(monkeypatch, tmp_path, username):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, username=username)


# ===========================================================================
# credential file validation (existence + safety; never echoed)
# ===========================================================================
def test_infra_key_must_exist(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, str(tmp_path / "missing_key"), kh)


def test_infra_known_hosts_must_exist_and_be_nonempty(monkeypatch, tmp_path):
    key, _ = _mkfiles(tmp_path)
    empty_kh = tmp_path / "empty_kh"
    empty_kh.write_text("")
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, str(empty_kh))


def test_infra_key_relative_accepted(monkeypatch, tmp_path):
    # A key / known_hosts path may be relative to the working directory (the same
    # convention as SYSTEM_PROMPT_PATH / ATTACHMENT_STORAGE_PATH). This is the
    # local single-file setup: "config/id_..." + "config/ssh_known_hosts".
    (tmp_path / "config").mkdir()
    key = tmp_path / "config" / "id_nas"
    key.write_text("FAKE-PRIVATE-KEY")
    kh = tmp_path / "config" / "ssh_known_hosts"
    kh.write_text("nas.local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA")
    monkeypatch.chdir(tmp_path)
    cfg = _load_target(monkeypatch, "config/id_nas", "config/ssh_known_hosts")
    t = cfg.infra_ssh_targets[0]
    assert t.private_key_path == "config/id_nas"
    assert t.known_hosts_path == "config/ssh_known_hosts"
    # Relative to the working directory, both must actually exist and be files.
    assert (tmp_path / t.private_key_path).is_file()
    assert (tmp_path / t.known_hosts_path).is_file()


def test_infra_key_relative_missing_rejected(monkeypatch, tmp_path):
    # A relative path is fine, but it still must point at an existing file.
    _, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, "does/not/exist/key", kh)


def test_infra_key_relative_dotdot_rejected(monkeypatch, tmp_path):
    # A '..' segment is rejected whether the path is absolute or relative.
    _, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, "a/../b/key", kh)


def test_infra_key_tilde_rejected(monkeypatch, tmp_path):
    _, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, "~/key", kh)


def test_infra_key_dotdot_rejected(monkeypatch, tmp_path):
    _, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, "/a/../b/key", kh)


def test_infra_key_symlink_rejected(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    link = tmp_path / "link_key"
    link.symlink_to(key)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, str(link), kh)


def test_infra_key_directory_rejected(monkeypatch, tmp_path):
    _, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, str(tmp_path), kh)


def test_infra_file_error_never_echoes_path(monkeypatch, tmp_path):
    _, kh = _mkfiles(tmp_path)
    secret_key = str(tmp_path / "TOPSECRET_KEY_PATH")
    with pytest.raises(ConfigError) as exc:
        _load_target(monkeypatch, secret_key, kh)
    assert "TOPSECRET_KEY_PATH" not in str(exc.value)
    assert "private_key_path" in str(exc.value)


# ===========================================================================
# mounts / services
# ===========================================================================
@pytest.mark.parametrize(
    "mounts",
    ["not-a-list", [], [""], ["/ok", 5], ["/a", "b-relative"], ["/a/../b"], ["~x"], ["/" + "x" * 300]],
)
def test_infra_rejects_bad_mounts(monkeypatch, tmp_path, mounts):
    key, kh = _mkfiles(tmp_path)
    with pytest.raises(ConfigError):
        _load_target(monkeypatch, key, kh, mounts=mounts)


def test_infra_mount_path_not_echoed(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    secret_mount = "/a/../TOPSECRET"
    with pytest.raises(ConfigError) as exc:
        _load_target(monkeypatch, key, kh, mounts=[secret_mount])
    assert "TOPSECRET" not in str(exc.value)


def test_infra_rejects_bad_services(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    for bad in (["bad service"], [], ["a" * 129]):
        with pytest.raises(ConfigError):
            _load_target(monkeypatch, key, kh, services=bad)


# ===========================================================================
# numeric knobs + cross-knob
# ===========================================================================
@pytest.mark.parametrize("val", ["0", "-1", "abc"])
def test_infra_bad_connect_timeout(monkeypatch, val):
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_CONNECT_TIMEOUT_SECONDS=val)


@pytest.mark.parametrize("val", ["0", "-1", "abc"])
def test_infra_bad_max_result_chars(monkeypatch, val):
    with pytest.raises(ConfigError):
        _load(monkeypatch, MAX_INFRA_TOOL_RESULT_CHARS=val)


def test_infra_connect_timeout_cannot_exceed_tool_timeout(monkeypatch):
    # Default TOOL_TIMEOUT_SECONDS is 30; a connect timeout of 31 must be refused
    # (the whole approved SSH call runs *inside* the per-call tool timeout).
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_CONNECT_TIMEOUT_SECONDS="31")


def test_infra_connect_timeout_equal_to_tool_timeout_is_ok(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    cfg = _load(
        monkeypatch,
        TOOL_TIMEOUT_SECONDS="30",
        INFRA_SSH_CONNECT_TIMEOUT_SECONDS="30",
        INFRA_SSH_TARGETS=json.dumps([_target(key, kh)]),
    )
    assert cfg.infra_ssh_connect_timeout_seconds == 30.0


def test_infra_max_result_chars_honoured(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    cfg = _load(monkeypatch, MAX_INFRA_TOOL_RESULT_CHARS="1234", INFRA_SSH_TARGETS=json.dumps([_target(key, kh)]))
    assert cfg.max_infra_tool_result_chars == 1234


# ===========================================================================
# source selection — default file config/infra_ssh_targets.json + INFRA_SSH_TARGETS_FILE
# ===========================================================================
# The conftest ``_no_default_infra_targets_file`` fixture points the *default*
# infra-targets file at a non-existent path, so by default every test here sees
# "no default file" (→ inline fallback). To exercise the default-file path a test
# points the default constant at a concrete file under ``tmp_path`` (an inner
# monkeypatch override wins over the conftest one).


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _set_default_file(monkeypatch, tmp_path, text):
    """Point the *default* infra-targets file at a fresh path under ``tmp_path`` and
    write ``text`` into it; return the path (so tests can also inspect it)."""
    p = tmp_path / "default_targets.json"
    _write(p, text)
    monkeypatch.setattr(_config_module, "_INFRA_TARGETS_DEFAULT_FILE", str(p))
    return p


# --- default file present (INFRA_SSH_TARGETS_FILE unset) --------------------
def test_infra_default_file_is_read_when_present(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    _set_default_file(monkeypatch, tmp_path, json.dumps([_target(key, kh)]))
    cfg = _load(monkeypatch)  # no inline, no explicit file
    assert cfg.infra_ssh_targets[0].name == "nas"


def test_infra_default_file_wins_over_inline(monkeypatch, tmp_path):
    # A *malformed* inline value proves inline is not read while the default file
    # exists: if inline were parsed, this would raise instead of the file's target.
    key, kh = _mkfiles(tmp_path)
    _set_default_file(monkeypatch, tmp_path, json.dumps([_target(key, kh, name="fromfile")]))
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS="{definitely not json")
    assert cfg.infra_ssh_targets[0].name == "fromfile"


def test_infra_default_file_blank_is_config_error(monkeypatch, tmp_path):
    # A present-but-empty default file must not silently mean "no targets".
    for text in ("", "   \n"):
        _set_default_file(monkeypatch, tmp_path, text)
        with pytest.raises(ConfigError):
            _load(monkeypatch)


def test_infra_default_file_explicit_empty_array_is_no_targets(monkeypatch, tmp_path):
    # An explicit [] in the default file is valid and means "no targets".
    _set_default_file(monkeypatch, tmp_path, "[]")
    assert _load(monkeypatch).infra_ssh_targets == ()


def test_infra_default_file_error_does_not_echo_host(monkeypatch, tmp_path):
    _set_default_file(monkeypatch, tmp_path, "")
    with pytest.raises(ConfigError) as excinfo:
        _load(monkeypatch)
    assert "nas.local" not in str(excinfo.value)


# --- default file missing (conftest guard) → inline fallback -----------------
def test_infra_inline_used_when_default_file_missing(monkeypatch, tmp_path):
    # No default file (conftest guard) and no explicit file → inline is used.
    key, kh = _mkfiles(tmp_path)
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS=json.dumps([_target(key, kh)]))
    assert cfg.infra_ssh_targets[0].name == "nas"


def test_infra_all_unset_is_empty(monkeypatch):
    # No default file (conftest guard), no explicit file, no inline → no targets.
    assert _load(monkeypatch).infra_ssh_targets == ()


# --- explicit INFRA_SSH_TARGETS_FILE (wins over default AND inline) ----------
def test_infra_explicit_file_is_parsed(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    f = tmp_path / "targets.json"
    _write(f, json.dumps([_target(key, kh)]))
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))
    assert cfg.infra_ssh_targets[0].name == "nas"


def test_infra_explicit_file_wins_over_default(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    _set_default_file(monkeypatch, tmp_path, json.dumps([_target(key, kh, name="fromdefault")]))
    f = tmp_path / "explicit.json"
    _write(f, json.dumps([_target(key, kh, name="fromexplicit")]))
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))
    assert cfg.infra_ssh_targets[0].name == "fromexplicit"


def test_infra_explicit_file_wins_over_inline(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    f = tmp_path / "targets.json"
    _write(f, json.dumps([_target(key, kh)]))
    cfg = _load(monkeypatch, INFRA_SSH_TARGETS="{definitely not json", INFRA_SSH_TARGETS_FILE=str(f))
    assert cfg.infra_ssh_targets[0].name == "nas"


def test_infra_explicit_file_empty_array_is_no_targets(monkeypatch, tmp_path):
    f = tmp_path / "targets.json"
    _write(f, "[]")
    assert _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f)).infra_ssh_targets == ()


def test_infra_explicit_file_missing_is_config_error(monkeypatch, tmp_path):
    # A set-but-missing explicit file must not silently fall back — ConfigError.
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(tmp_path / "does-not-exist.json"))


def test_infra_explicit_file_unreadable_is_config_error(monkeypatch, tmp_path):
    # Point at a *directory*: read_text raises IsADirectoryError (OSError) →
    # ConfigError, never a crash.
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(tmp_path))


def test_infra_explicit_file_blank_is_config_error(monkeypatch, tmp_path):
    for text in ("", "   \n  "):
        f = tmp_path / "blank.json"
        _write(f, text)
        with pytest.raises(ConfigError):
            _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))


def test_infra_explicit_file_invalid_json_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    _write(f, "not json [")
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))


def test_infra_explicit_file_not_array_is_config_error(monkeypatch, tmp_path):
    key, kh = _mkfiles(tmp_path)
    f = tmp_path / "obj.json"
    _write(f, json.dumps(_target(key, kh)))
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))


def test_infra_explicit_file_bad_entry_is_config_error(monkeypatch, tmp_path):
    # The *same* strict per-entry validation applies to a file-configured target:
    # a bad host is rejected exactly as inline (and never echoed).
    key, kh = _mkfiles(tmp_path)
    f = tmp_path / "badentry.json"
    _write(f, json.dumps([_target(key, kh, host="user@host:22")]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))


def test_infra_explicit_file_error_does_not_echo_path_or_host(monkeypatch, tmp_path):
    f = tmp_path / "blank.json"
    _write(f, "")
    with pytest.raises(ConfigError) as excinfo:
        _load(monkeypatch, INFRA_SSH_TARGETS_FILE=str(f))
    assert "nas.local" not in str(excinfo.value)


