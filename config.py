from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"

_load_dotenv(ENV_PATH)


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    registry_file: Path
    users_dir: Path
    secrets_key_file: Path
    privacy_strict_mode: bool = False
    poll_timeout_seconds: int = 30
    default_sync_days: int = 30
    default_timezone: str = "Europe/Kyiv"
    daily_analysis_hour: int = 23
    monitor_initial_lookback_minutes: int = 30
    monitor_secondary_every_cycles: int = 5
    legacy_monobank_token: str | None = None
    legacy_chat_id: int | None = None
    legacy_priority_account_id: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        privacy_strict_mode = _read_bool("PRIVACY_STRICT_MODE", default=False)
        default_sync_days = int(os.getenv("DEFAULT_SYNC_DAYS", "30").strip())
        default_timezone = os.getenv("DEFAULT_TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
        daily_analysis_hour = int(os.getenv("DAILY_ANALYSIS_HOUR", "23").strip())
        monitor_lookback = int(os.getenv("MONITOR_INITIAL_LOOKBACK_MINUTES", "30").strip())
        monitor_secondary_every = int(os.getenv("MONITOR_SECONDARY_EVERY_CYCLES", "5").strip())

        if not telegram_bot_token:
            raise ValueError("Не задано TELEGRAM_BOT_TOKEN у .env або змінних середовища.")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        USERS_DIR.mkdir(parents=True, exist_ok=True)

        return cls(
            telegram_bot_token=telegram_bot_token,
            registry_file=DATA_DIR / "user_profiles.json",
            users_dir=USERS_DIR,
            secrets_key_file=DATA_DIR / ".secret.key",
            privacy_strict_mode=privacy_strict_mode,
            poll_timeout_seconds=30,
            default_sync_days=max(1, default_sync_days),
            default_timezone=default_timezone,
            daily_analysis_hour=min(23, max(0, daily_analysis_hour)),
            monitor_initial_lookback_minutes=max(5, monitor_lookback),
            monitor_secondary_every_cycles=max(2, monitor_secondary_every),
            legacy_monobank_token=os.getenv("LEGACY_MONOBANK_TOKEN", "").strip() or os.getenv("MONOBANK_TOKEN", "").strip() or None,
            legacy_chat_id=_read_optional_int("LEGACY_CHAT_ID") or _read_optional_int("TELEGRAM_ALLOWED_CHAT_ID"),
            legacy_priority_account_id=os.getenv("LEGACY_PRIORITY_ACCOUNT_ID", "").strip() or os.getenv("MONOBANK_PRIORITY_ACCOUNT_ID", "").strip() or None,
        )


def _read_optional_int(name: str) -> int | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}
