"""Phase 9 — schedule (``SCHEDULES``) config parsing + validation (required #3).

Everything is local: no file I/O beyond the (tmp) schedules file, no network, no
LLM. ``SCHEDULES`` (and ``SCHEDULES_FILE`` / the default ``config/schedules.json``)
are parsed and strictly validated at startup. The key rule under test is that an
error names the *schedule* (or its index) and the *field* — **never** the
``prompt`` body (or any other field value) — mirroring the infra-targets rule.
The conftest ``_no_default_schedules_file`` guard points the *default* file at a
non-existent path, so every test here sees "no default file" by default (→ inline
fallback); tests that want the default-file path point the constant at a concrete
``tmp_path`` file (an inner override wins).
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
    for knob in ("SCHEDULES", "SCHEDULES_FILE", "SCHEDULE_TIMEZONE"):
        monkeypatch.delenv(knob, raising=False)
    for k, v in _env(**extra).items():
        monkeypatch.setenv(k, v)
    return load_config()


def _sched(name="daily", cron="0 7 * * *", prompt="do the check",
           identity="telegram", telegram=(1, 1), qq=None):
    """A default schedule with an overridable ``receiver``.

    ``telegram`` is ``None`` or a ``(chat_id, user_id)`` pair → ``receiver.telegram``;
    ``qq`` is ``None`` or a ``user_openid`` string → ``receiver.qq``. The default is
    a ``"telegram"``-identity, telegram-receiver-only schedule. Tests that used the
    old flat ``chat_id`` / ``user_id`` knobs retarget onto ``telegram=(chat_id,
    user_id)``; a ``"qq"`` identity sets ``telegram=None, qq=<openid>``.
    """
    receiver: dict = {}
    if telegram is not None:
        receiver["telegram"] = {"chat_id": telegram[0], "user_id": telegram[1]}
    if qq is not None:
        receiver["qq"] = {"user_openid": qq}
    return {"name": name, "cron": cron, "prompt": prompt, "identity": identity, "receiver": receiver}


def _mkfiles_write(path, text):
    path.write_text(text, encoding="utf-8")


# ===========================================================================
# empty / defaults
# ===========================================================================
def test_schedules_unset_is_empty(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.schedules == ()
    assert cfg.schedule_timezone == ""


def test_schedules_blank_is_empty(monkeypatch):
    assert _load(monkeypatch, SCHEDULES="   ").schedules == ()


def test_schedules_explicit_empty_array_is_empty(monkeypatch):
    assert _load(monkeypatch, SCHEDULES="[]").schedules == ()


# ===========================================================================
# valid parsing
# ===========================================================================
def test_schedules_parses_one(monkeypatch):
    cfg = _load(monkeypatch, SCHEDULES=json.dumps([_sched()]))
    assert len(cfg.schedules) == 1
    s = cfg.schedules[0]
    assert s.name == "daily"
    assert s.cron == "0 7 * * *"
    assert s.identity == "telegram"
    assert s.telegram.chat_id == 1
    assert s.telegram.user_id == 1
    assert s.qq is None
    assert s.memory_scope() == "telegram:1"
    assert s.approval_delivery_chat_id() == 1
    assert s.prompt == "do the check"


def test_schedules_parses_qq_identity(monkeypatch):
    cfg = _load(monkeypatch, SCHEDULES=json.dumps([_sched(identity="qq", telegram=None, qq="AxB")]))
    s = cfg.schedules[0]
    assert s.identity == "qq"
    assert s.qq.user_openid == "AxB"
    assert s.telegram is None
    assert s.memory_scope() == "qq:AxB"
    assert s.approval_delivery_chat_id() is None


def test_schedules_parses_both_receivers(monkeypatch):
    # identity=qq but BOTH receivers present → delivered to both channels, the
    # run executes under the qq identity.
    cfg = _load(
        monkeypatch,
        SCHEDULES=json.dumps([_sched(identity="qq", telegram=(42, 7), qq="AxB")]),
    )
    s = cfg.schedules[0]
    assert s.identity == "qq"
    assert s.telegram.chat_id == 42
    assert s.qq.user_openid == "AxB"
    assert s.memory_scope() == "qq:AxB"
    assert s.approval_delivery_chat_id() is None


def test_schedules_parses_multiple(monkeypatch):
    cfg = _load(
        monkeypatch,
        SCHEDULES=json.dumps([_sched(name="a"), _sched(name="b", cron="0 0 1 * *")]),
    )
    assert [s.name for s in cfg.schedules] == ["a", "b"]
    assert cfg.schedules[1].cron == "0 0 1 * *"


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_schedule_timezone_valid(monkeypatch, tz):
    cfg = _load(monkeypatch, SCHEDULE_TIMEZONE=tz)
    assert cfg.schedule_timezone == tz


# ===========================================================================
# structural rejections
# ===========================================================================
def test_schedules_rejects_invalid_json(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES="not json [")


def test_schedules_rejects_non_array(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps(_sched()))


def test_schedules_rejects_non_object_entry(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES='["daily"]')


@pytest.mark.parametrize("field", ["cron_extra", "tz", "prompt2", "bot", "bogus"])
def test_schedules_rejects_unknown_field(monkeypatch, field):
    d = _sched()
    d[field] = "x"
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_missing_name(monkeypatch):
    d = _sched()
    del d["name"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


@pytest.mark.parametrize("name", ["Daily", "1daily", "a" * 33, "has space", "a.b", "", "a/b", "UPPER"])
def test_schedules_rejects_bad_name(monkeypatch, name):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(name=name)]))


def test_schedules_rejects_duplicate_name(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(name="same"), _sched(name="same")]))


def test_schedules_rejects_too_many(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(name=f"t{i}") for i in range(17)]))


def test_schedules_sixteen_is_allowed(monkeypatch):
    cfg = _load(monkeypatch, SCHEDULES=json.dumps([_sched(name=f"t{i}") for i in range(16)]))
    assert len(cfg.schedules) == 16


# ===========================================================================
# cron validation
# ===========================================================================
def test_schedules_rejects_missing_cron(monkeypatch):
    d = _sched()
    del d["cron"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_blank_cron(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(cron="   ")]))


@pytest.mark.parametrize("cron", ["* * * *", "@daily", "0 10-4 * * *", "60 * * * *", "a * * * *"])
def test_schedules_rejects_bad_cron(monkeypatch, cron):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(cron=cron)]))


# ===========================================================================
# identity + receiver validation
# ===========================================================================
def _bad_identity(identity):
    return {"name": "x", "cron": "0 7 * * *", "prompt": "p", "identity": identity,
            "receiver": {"telegram": {"chat_id": 1, "user_id": 1}}}


@pytest.mark.parametrize("identity", [None, "", "TG", "tg", 1, ["telegram"], "slack"])
def test_schedules_rejects_bad_identity(monkeypatch, identity):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_bad_identity(identity)]))


def test_schedules_rejects_missing_identity(monkeypatch):
    d = _sched()
    del d["identity"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


@pytest.mark.parametrize("receiver", [None, "x", "telegram", ["telegram"], {}])
def test_schedules_rejects_bad_receiver(monkeypatch, receiver):
    d = _sched()
    d["receiver"] = receiver
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_unknown_receiver_channel(monkeypatch):
    d = _sched()
    d["receiver"]["slack"] = {"foo": 1}
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_identity_without_its_receiver(monkeypatch):
    # identity=qq but only a telegram receiver present.
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(identity="qq", telegram=(1, 1), qq=None)]))
    # identity=telegram but only a qq receiver present.
    d = _sched(identity="telegram", telegram=None, qq="AxB")
    d["receiver"] = {"qq": {"user_openid": "AxB"}}
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


# --- receiver.telegram.chat_id / user_id (positive int, bools rejected) ------
@pytest.mark.parametrize("chat_id", [0, -1, "1", 1.0, True, None, "abc"])
def test_schedules_rejects_bad_telegram_chat_id(monkeypatch, chat_id):
    d = _sched()
    d["receiver"]["telegram"]["chat_id"] = chat_id
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


@pytest.mark.parametrize("user_id", [0, -1, "1", 1.0, True, None])
def test_schedules_rejects_bad_telegram_user_id(monkeypatch, user_id):
    d = _sched()
    d["receiver"]["telegram"]["user_id"] = user_id
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_telegram_receiver_missing_chat_id(monkeypatch):
    d = _sched()
    del d["receiver"]["telegram"]["chat_id"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_telegram_receiver_missing_user_id(monkeypatch):
    d = _sched()
    del d["receiver"]["telegram"]["user_id"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_unknown_telegram_receiver_field(monkeypatch):
    d = _sched()
    d["receiver"]["telegram"]["bogus"] = 1
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_non_object_telegram_receiver(monkeypatch):
    d = _sched()
    d["receiver"]["telegram"] = "not-an-object"
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


# --- receiver.qq.user_openid (non-empty string) -----------------------------
def _qq_sched(user_openid):
    return {"name": "x", "cron": "0 7 * * *", "prompt": "p", "identity": "qq",
            "receiver": {"qq": {"user_openid": user_openid}}}


@pytest.mark.parametrize("user_openid", [None, "", "   ", 123, ["a"], {"a": 1}])
def test_schedules_rejects_bad_qq_user_openid(monkeypatch, user_openid):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_qq_sched(user_openid)]))


def test_schedules_rejects_qq_receiver_missing_user_openid(monkeypatch):
    d = _qq_sched("AxB")
    del d["receiver"]["qq"]["user_openid"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_unknown_qq_receiver_field(monkeypatch):
    d = _qq_sched("AxB")
    d["receiver"]["qq"]["chat_id"] = 5
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


# ===========================================================================
# prompt validation
# ===========================================================================
def test_schedules_rejects_missing_prompt(monkeypatch):
    d = _sched()
    del d["prompt"]
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([d]))


def test_schedules_rejects_empty_prompt(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(prompt="   ")]))


def test_schedules_rejects_overlong_prompt(monkeypatch):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(prompt="x" * 2001)]))


def test_schedules_accepts_prompt_at_cap(monkeypatch):
    cfg = _load(monkeypatch, SCHEDULES=json.dumps([_sched(prompt="x" * 2000)]))
    assert len(cfg.schedules[0].prompt) == 2000


# ===========================================================================
# SCHEDULE_TIMEZONE validation
# ===========================================================================
@pytest.mark.parametrize("tz", ["Not/AZone", "UTC+8", "", "   "])
def test_schedule_timezone_rejects_bad(monkeypatch, tz):
    if tz in ("", "   "):
        # Blank / whitespace is treated as unset (a no-op), not an error.
        cfg = _load(monkeypatch, SCHEDULE_TIMEZONE=tz)
        assert cfg.schedule_timezone == ""
    else:
        with pytest.raises(ConfigError):
            _load(monkeypatch, SCHEDULE_TIMEZONE=tz)


# ===========================================================================
# error messages name the schedule/field, never the prompt (privacy)
# ===========================================================================
def test_schedules_bad_cron_error_names_schedule_not_prompt(monkeypatch):
    secret = "TOP-SECRET-PROMPT-XYZ"
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(name="nightly", cron="* * * *", prompt=secret)]))
    msg = str(exc.value)
    assert "nightly" in msg  # the name is echoed (it is safe)
    assert "cron" in msg
    assert secret not in msg  # the prompt is NEVER echoed


def test_schedules_bad_chat_id_error_names_field_not_value(monkeypatch):
    secret = "SECRET-PROMPT-ABC"
    d = _sched(name="nightly", telegram=(-5, 1), prompt=secret)
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, SCHEDULES=json.dumps([d]))
    msg = str(exc.value)
    assert "chat_id" in msg
    assert "receiver.telegram" in msg
    assert "-5" not in msg  # the (bad) value is not echoed
    assert secret not in msg


def test_schedules_bad_user_openid_error_names_field_not_value(monkeypatch):
    # A non-string openid is rejected; the error names the field but never echoes
    # the (bad) value — mirroring the "don't echo the value" rule for other providers.
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, SCHEDULES=json.dumps([_qq_sched(12345)]))
    msg = str(exc.value)
    assert "user_openid" in msg
    assert "12345" not in msg  # the (bad) value is not echoed


def test_schedules_overlong_prompt_error_names_length_not_body(monkeypatch):
    secret = "S" * 2001
    with pytest.raises(ConfigError) as exc:
        _load(monkeypatch, SCHEDULES=json.dumps([_sched(prompt=secret)]))
    msg = str(exc.value)
    assert "prompt" in msg
    assert secret not in msg


# ===========================================================================
# source selection — default file config/schedules.json + SCHEDULES_FILE
# ===========================================================================
def _set_default_file(monkeypatch, tmp_path, text):
    p = tmp_path / "default_schedules.json"
    _mkfiles_write(p, text)
    monkeypatch.setattr(_config_module, "_SCHEDULES_DEFAULT_FILE", str(p))
    return p


# --- default file present (SCHEDULES_FILE unset) ---------------------------
def test_schedules_default_file_is_read_when_present(monkeypatch, tmp_path):
    _set_default_file(monkeypatch, tmp_path, json.dumps([_sched(name="fromfile")]))
    cfg = _load(monkeypatch)  # no inline, no explicit file
    assert cfg.schedules[0].name == "fromfile"


def test_schedules_default_file_wins_over_inline(monkeypatch, tmp_path):
    # A malformed inline value proves inline is not read while the default file exists.
    _set_default_file(monkeypatch, tmp_path, json.dumps([_sched(name="fromfile")]))
    cfg = _load(monkeypatch, SCHEDULES="{definitely not json")
    assert cfg.schedules[0].name == "fromfile"


def test_schedules_default_file_blank_is_config_error(monkeypatch, tmp_path):
    for text in ("", "   \n"):
        _set_default_file(monkeypatch, tmp_path, text)
        with pytest.raises(ConfigError):
            _load(monkeypatch)


def test_schedules_default_file_explicit_empty_array_is_empty(monkeypatch, tmp_path):
    _set_default_file(monkeypatch, tmp_path, "[]")
    assert _load(monkeypatch).schedules == ()


def test_schedules_default_file_error_does_not_echo_prompt(monkeypatch, tmp_path):
    _set_default_file(monkeypatch, tmp_path, json.dumps([_sched(name="x", cron="* * * *", prompt="SECRET")]))
    with pytest.raises(ConfigError) as excinfo:
        _load(monkeypatch)
    assert "SECRET" not in str(excinfo.value)


# --- default file missing (conftest guard) → inline fallback ---------------
def test_schedules_inline_used_when_default_file_missing(monkeypatch):
    cfg = _load(monkeypatch, SCHEDULES=json.dumps([_sched(name="inline")]))
    assert cfg.schedules[0].name == "inline"


def test_schedules_all_unset_is_empty(monkeypatch):
    assert _load(monkeypatch).schedules == ()


# --- explicit SCHEDULES_FILE (wins over default AND inline) ----------------
def test_schedules_explicit_file_is_parsed(monkeypatch, tmp_path):
    f = tmp_path / "schedules.json"
    _mkfiles_write(f, json.dumps([_sched(name="fromexplicit")]))
    cfg = _load(monkeypatch, SCHEDULES_FILE=str(f))
    assert cfg.schedules[0].name == "fromexplicit"


def test_schedules_explicit_file_wins_over_default(monkeypatch, tmp_path):
    _set_default_file(monkeypatch, tmp_path, json.dumps([_sched(name="fromdefault")]))
    f = tmp_path / "explicit.json"
    _mkfiles_write(f, json.dumps([_sched(name="fromexplicit")]))
    cfg = _load(monkeypatch, SCHEDULES_FILE=str(f))
    assert cfg.schedules[0].name == "fromexplicit"


def test_schedules_explicit_file_wins_over_inline(monkeypatch, tmp_path):
    f = tmp_path / "schedules.json"
    _mkfiles_write(f, json.dumps([_sched(name="fromfile")]))
    cfg = _load(monkeypatch, SCHEDULES="{definitely not json", SCHEDULES_FILE=str(f))
    assert cfg.schedules[0].name == "fromfile"


def test_schedules_explicit_file_empty_array_is_empty(monkeypatch, tmp_path):
    f = tmp_path / "s.json"
    _mkfiles_write(f, "[]")
    assert _load(monkeypatch, SCHEDULES_FILE=str(f)).schedules == ()


def test_schedules_explicit_file_missing_is_config_error(monkeypatch, tmp_path):
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES_FILE=str(tmp_path / "does-not-exist.json"))


def test_schedules_explicit_file_unreadable_is_config_error(monkeypatch, tmp_path):
    # Point at a *directory*: read_text raises IsADirectoryError (OSError) → ConfigError.
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES_FILE=str(tmp_path))


def test_schedules_explicit_file_blank_is_config_error(monkeypatch, tmp_path):
    for text in ("", "   \n  "):
        f = tmp_path / "blank.json"
        _mkfiles_write(f, text)
        with pytest.raises(ConfigError):
            _load(monkeypatch, SCHEDULES_FILE=str(f))


def test_schedules_explicit_file_invalid_json_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    _mkfiles_write(f, "not json [")
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES_FILE=str(f))


def test_schedules_explicit_file_not_array_is_config_error(monkeypatch, tmp_path):
    f = tmp_path / "obj.json"
    _mkfiles_write(f, json.dumps(_sched()))
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES_FILE=str(f))


def test_schedules_explicit_file_bad_entry_is_config_error(monkeypatch, tmp_path):
    # The *same* strict per-entry validation applies to a file-configured schedule.
    f = tmp_path / "bad.json"
    _mkfiles_write(f, json.dumps([_sched(name="x", cron="* * * *")]))
    with pytest.raises(ConfigError):
        _load(monkeypatch, SCHEDULES_FILE=str(f))


def test_schedules_explicit_file_error_does_not_echo_prompt(monkeypatch, tmp_path):
    f = tmp_path / "bad.json"
    _mkfiles_write(f, json.dumps([_sched(name="x", cron="* * * *", prompt="SECRET")]))
    with pytest.raises(ConfigError) as excinfo:
        _load(monkeypatch, SCHEDULES_FILE=str(f))
    assert "SECRET" not in str(excinfo.value)
