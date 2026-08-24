"""Runtime configuration.

All external configuration is read from environment variables (optionally
loaded from a local ``.env`` file). Secrets (the Telegram bot token and the
OpenAI API key) come *only* from the environment and must never be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _load_env() -> None:
    # Load an uncommitted .env from the current working directory. Existing
    # environment variables always take precedence over values in the file.
    load_dotenv(override=False)


@dataclass(frozen=True)
class Config:
    """Immutable, validated view of everything the backend needs to run."""

    telegram_bot_token: str
    allowed_user_ids: frozenset[int]

    openai_base_url: str
    openai_api_key: str
    openai_model: str
    openai_timeout: float

    database_url: str
    system_prompt_path: Path
    max_context_messages: int

    log_level: str = "INFO"
    system_prompt_override: str | None = field(default=None)

    def __post_init__(self) -> None:
        # OpenAI SDK appends "/chat/completions" to base_url, so base_url must
        # point at the API *prefix* (e.g. …/v1), not the full completions URL.
        object.__setattr__(self, "openai_base_url", self.openai_base_url.rstrip("/"))
        if not self.allowed_user_ids:
            raise ConfigError("TELEGRAM_ALLOWED_USER_IDS must list at least one Telegram user id")
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
        if not self.openai_base_url:
            raise ConfigError("OPENAI_BASE_URL is not set — the API prefix, e.g. https://host/v1 (the SDK appends /chat/completions)")
        if not self.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set")
        if not self.openai_model:
            raise ConfigError("OPENAI_MODEL is not set")
        if self.max_context_messages < 1:
            raise ConfigError("MAX_CONTEXT_MESSAGES must be >= 1")

    @property
    def system_prompt(self) -> str:
        """The effective system prompt (override > file > built-in fallback)."""
        if self.system_prompt_override:
            return self.system_prompt_override
        if self.system_prompt_path.exists():
            return self.system_prompt_path.read_text(encoding="utf-8").strip()
        return (
            "你是一个运行在用户私人服务器上的个人 AI Agent。"
            "你需要准确、简洁地回答用户问题。"
            "当前你只能进行对话，不能执行任何外部操作。"
        )


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"invalid Telegram user id in TELEGRAM_ALLOWED_USER_IDS: {part!r}") from exc
    return frozenset(ids)


def _parse_float(raw: str, default: float) -> float:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid float value: {raw!r}") from exc


def _parse_int(raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid int value: {raw!r}") from exc


def load_config() -> Config:
    """Build a validated :class:`Config` from the environment.

    The LLM endpoint and model have no hardcoded default — they must be
    provided via env (``OPENAI_*``), kept out of the repo for privacy.
    """
    _load_env()
    return Config(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_user_ids=_parse_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "").strip(),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_model=os.environ.get("OPENAI_MODEL", "").strip(),
        openai_timeout=_parse_float(os.environ.get("OPENAI_TIMEOUT", ""), 120.0),
        database_url=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/agent.db").strip(),
        system_prompt_path=Path(os.environ.get("SYSTEM_PROMPT_PATH", "config/system_prompt.txt")),
        max_context_messages=_parse_int(os.environ.get("MAX_CONTEXT_MESSAGES", ""), 50),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        system_prompt_override=os.environ.get("SYSTEM_PROMPT", "").strip() or None,
    )
