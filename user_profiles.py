from __future__ import annotations

import copy
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secret_box import SecretBox


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class UserProfile:
    chat_id: int
    monobank_token: str
    timezone: str = "Europe/Kyiv"
    connected_at: str = ""
    last_daily_report_date: str | None = None
    priority_account_id: str | None = None
    telegram_username: str | None = None
    first_name: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "UserProfile":
        return cls(
            chat_id=int(payload["chat_id"]),
            monobank_token=str(payload["monobank_token"]),
            timezone=str(payload.get("timezone") or "Europe/Kyiv"),
            connected_at=str(payload.get("connected_at") or utc_now_iso()),
            last_daily_report_date=payload.get("last_daily_report_date"),
            priority_account_id=payload.get("priority_account_id"),
            telegram_username=payload.get("telegram_username"),
            first_name=payload.get("first_name"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["chat_id"] = int(self.chat_id)
        return payload


@dataclass(slots=True)
class UserRegistry:
    path: Path
    users_dir: Path
    secret_box: SecretBox
    persist_to_disk: bool = True
    _memory_raw: dict[str, Any] | None = None

    def list_profiles(self) -> list[UserProfile]:
        raw = self._load_raw()
        users = raw.get("users", {})
        return [
            self._deserialize_profile(profile)
            for _, profile in sorted(users.items(), key=lambda item: int(item[0]))
        ]

    def get(self, chat_id: int) -> UserProfile | None:
        raw = self._load_raw()
        payload = raw.get("users", {}).get(str(chat_id))
        if payload is None:
            return None
        return self._deserialize_profile(payload)

    def upsert(self, profile: UserProfile) -> None:
        raw = self._load_raw()
        raw.setdefault("users", {})[str(profile.chat_id)] = self._serialize_profile(profile)
        self._save_raw(raw)

    def remove(self, chat_id: int, delete_files: bool = True) -> None:
        raw = self._load_raw()
        raw.setdefault("users", {}).pop(str(chat_id), None)
        self._save_raw(raw)
        if delete_files:
            self.clear_user_data(chat_id)

    def user_dir(self, chat_id: int) -> Path:
        directory = self.users_dir / str(chat_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def data_file(self, chat_id: int) -> Path:
        return self.user_dir(chat_id) / "transactions.json"

    def state_file(self, chat_id: int) -> Path:
        return self.user_dir(chat_id) / "state.json"

    def clear_user_data(self, chat_id: int) -> None:
        directory = self.users_dir / str(chat_id)
        if directory.exists():
            shutil.rmtree(directory)

    def _load_raw(self) -> dict[str, Any]:
        if not self.persist_to_disk:
            if self._memory_raw is None:
                self._memory_raw = {"users": {}}
            return copy.deepcopy(self._memory_raw)

        if not self.path.exists():
            return {"users": {}}

        with self.path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if isinstance(payload, str):
            return dict(self.secret_box.decrypt_json(payload))

        if isinstance(payload, dict):
            self._save_raw(payload)
            return payload

        raise ValueError("Unsupported registry format.")

    def _save_raw(self, payload: dict[str, Any]) -> None:
        if not self.persist_to_disk:
            self._memory_raw = copy.deepcopy(payload)
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(self.secret_box.encrypt_json(payload), file, ensure_ascii=False, separators=(",", ":"))

    def _serialize_profile(self, profile: UserProfile) -> dict[str, Any]:
        payload = profile.to_dict()
        encrypted = self.secret_box.encrypt(profile.monobank_token)
        payload.pop("monobank_token", None)
        payload["monobank_token_encrypted"] = encrypted
        return payload

    def _deserialize_profile(self, payload: dict[str, Any]) -> UserProfile:
        normalized = dict(payload)
        encrypted = normalized.pop("monobank_token_encrypted", None)
        if encrypted:
            normalized["monobank_token"] = self.secret_box.decrypt(str(encrypted))
        return UserProfile.from_dict(normalized)
