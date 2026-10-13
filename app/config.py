"""Central configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # LLM
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_model: str = "openrouter/auto"

    # GitHub
    github_token: str = field(default="", repr=False)
    github_webhook_secret: str = field(default="", repr=False)

    # Telegram
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""

    # Agent behavior
    max_iterations: int = 40

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            openrouter_api_key=_get("OPENROUTER_API_KEY"),
            openrouter_model=_get("OPENROUTER_MODEL", "openrouter/auto"),
            github_token=_get("GITHUB_TOKEN"),
            github_webhook_secret=_get("GITHUB_WEBHOOK_SECRET"),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
            max_iterations=max(1, _get_int("MAX_ITERATIONS", 40)),
        )

    @property
    def missing(self) -> list[str]:
        """Names of required env vars that are unset, for clear error messages."""
        checks = [
            ("OPENROUTER_API_KEY", self.openrouter_api_key),
            ("GITHUB_TOKEN", self.github_token),
            ("GITHUB_WEBHOOK_SECRET", self.github_webhook_secret),
            ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
            ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
        ]
        return [name for name, val in checks if not val]

    @property
    def is_valid(self) -> bool:
        return not self.missing


@lru_cache(maxsize=1)
def get_settings() -> "Settings":
    """Lazy singleton — loaded on first call, not at import time."""
    return Settings.load()
