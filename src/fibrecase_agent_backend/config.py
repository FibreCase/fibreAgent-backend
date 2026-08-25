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
    max_context_estimated_tokens: int
    context_image_estimated_tokens: int

    enable_tools: bool
    max_tool_iterations: int

    max_image_size_mb: float

    attachment_storage_path: Path

    # Phase 2.5: explicit long-term memory. All positive integers; the memory
    # estimated-token sub-budget must not exceed the total context budget.
    # Defaults keep an existing .env (without these keys) working.
    max_memories_per_scope: int = 200
    max_memory_chars: int = 1000
    max_retrieved_memories: int = 5
    max_memory_estimated_tokens: int = 3000

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
        if self.max_context_estimated_tokens < 1:
            raise ConfigError("MAX_CONTEXT_ESTIMATED_TOKENS must be >= 1")
        if self.context_image_estimated_tokens < 1:
            raise ConfigError("CONTEXT_IMAGE_ESTIMATED_TOKENS must be >= 1")
        if self.max_tool_iterations < 1:
            raise ConfigError("MAX_TOOL_ITERATIONS must be >= 1")
        if self.max_image_size_mb < 1:
            raise ConfigError("MAX_IMAGE_SIZE_MB must be >= 1")
        if self.max_memories_per_scope < 1:
            raise ConfigError("MAX_MEMORIES_PER_SCOPE must be >= 1")
        if self.max_memory_chars < 1:
            raise ConfigError("MAX_MEMORY_CHARS must be >= 1")
        if self.max_retrieved_memories < 1:
            raise ConfigError("MAX_RETRIEVED_MEMORIES must be >= 1")
        if self.max_memory_estimated_tokens < 1:
            raise ConfigError("MAX_MEMORY_ESTIMATED_TOKENS must be >= 1")
        if self.max_memory_estimated_tokens > self.max_context_estimated_tokens:
            raise ConfigError(
                "MAX_MEMORY_ESTIMATED_TOKENS must be <= MAX_CONTEXT_ESTIMATED_TOKENS "
                "(the memory sub-budget cannot exceed the total context budget)"
            )

    @property
    def max_image_size_bytes(self) -> int:
        """The ``MAX_IMAGE_SIZE_MB`` cap expressed in bytes."""
        return int(self.max_image_size_mb * 1_000_000)

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
            "你可以调用可用工具来回答问题（例如查询当前时间、获取系统信息）。"
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


def _parse_bool(raw: str, default: bool) -> bool:
    raw = raw.strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y", "t"):
        return True
    if raw in ("0", "false", "no", "off", "n", "f"):
        return False
    raise ConfigError(f"invalid boolean value: {raw!r}")


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
        max_context_estimated_tokens=_parse_int(os.environ.get("MAX_CONTEXT_ESTIMATED_TOKENS", ""), 24000),
        context_image_estimated_tokens=_parse_int(os.environ.get("CONTEXT_IMAGE_ESTIMATED_TOKENS", ""), 2000),
        enable_tools=_parse_bool(os.environ.get("ENABLE_TOOLS", ""), True),
        max_tool_iterations=_parse_int(os.environ.get("MAX_TOOL_ITERATIONS", ""), 5),
        max_image_size_mb=_parse_float(os.environ.get("MAX_IMAGE_SIZE_MB", ""), 10.0),
        attachment_storage_path=Path(os.environ.get("ATTACHMENT_STORAGE_PATH", "./data/attachments")),
        max_memories_per_scope=_parse_int(os.environ.get("MAX_MEMORIES_PER_SCOPE", ""), 200),
        max_memory_chars=_parse_int(os.environ.get("MAX_MEMORY_CHARS", ""), 1000),
        max_retrieved_memories=_parse_int(os.environ.get("MAX_RETRIEVED_MEMORIES", ""), 5),
        max_memory_estimated_tokens=_parse_int(os.environ.get("MAX_MEMORY_ESTIMATED_TOKENS", ""), 3000),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        system_prompt_override=os.environ.get("SYSTEM_PROMPT", "").strip() or None,
    )
