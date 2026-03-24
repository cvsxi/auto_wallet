from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gemini_client import GeminiAPIError, GeminiClient
from secret_box import SecretBox


@dataclass(slots=True)
class GeminiRouter:
    api_key: str
    models: tuple[str, ...]
    usage_path: Path
    secret_box: SecretBox | None = None
    switch_after_requests: int = 19

    def generate_daily_analysis(self, summary_payload: dict[str, Any]) -> tuple[str, str]:
        return self._generate(summary_payload, mode="daily")

    def generate_period_analysis(self, summary_payload: dict[str, Any]) -> tuple[str, str]:
        return self._generate(summary_payload, mode="period")

    def _generate(self, summary_payload: dict[str, Any], mode: str) -> tuple[str, str]:
        usage = self._load_usage()
        today = datetime.now(UTC).date().isoformat()
        day_usage = usage.setdefault(today, {})

        candidates = self._ordered_models(day_usage)
        last_error: Exception | None = None

        for model in candidates:
            model_usage = day_usage.setdefault(
                model,
                {"attempts": 0, "successes": 0, "exhausted": False, "last_error": None},
            )
            try:
                client = GeminiClient(self.api_key, model)
                if mode == "daily":
                    text = client.generate_daily_analysis(summary_payload)
                else:
                    text = client.generate_period_analysis(summary_payload)
                model_usage["attempts"] += 1
                model_usage["successes"] += 1
                model_usage["last_error"] = None
                self._save_usage(usage)
                return text, model
            except GeminiAPIError as exc:
                model_usage["attempts"] += 1
                model_usage["last_error"] = str(exc)
                if self._is_quota_error(exc):
                    model_usage["exhausted"] = True
                self._save_usage(usage)
                last_error = exc
                continue

        if last_error is not None:
            raise last_error
        raise GeminiAPIError("Немає доступних Gemini моделей для запиту.")

    def _ordered_models(self, day_usage: dict[str, dict[str, Any]]) -> list[str]:
        preferred: list[str] = []
        fallback: list[str] = []

        for model in self.models:
            entry = day_usage.get(model, {})
            exhausted = bool(entry.get("exhausted", False))
            attempts = int(entry.get("attempts", 0))
            if exhausted:
                continue
            if attempts >= self.switch_after_requests:
                fallback.append(model)
            else:
                preferred.append(model)

        return preferred + fallback

    def _load_usage(self) -> dict[str, Any]:
        if not self.usage_path.exists():
            return {}
        with self.usage_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if isinstance(payload, str):
            if self.secret_box is None:
                raise GeminiAPIError("Encrypted Gemini usage requires SecretBox.")
            return dict(self.secret_box.decrypt_json(payload))

        if isinstance(payload, dict):
            if self.secret_box is not None:
                self._save_usage(payload)
            return payload

        raise GeminiAPIError("Unsupported Gemini usage format.")

    def _save_usage(self, payload: dict[str, Any]) -> None:
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        keep_dates = sorted(payload.keys())[-14:]
        trimmed = {key: payload[key] for key in keep_dates}
        with self.usage_path.open("w", encoding="utf-8") as file:
            if self.secret_box is None:
                json.dump(trimmed, file, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(
                    self.secret_box.encrypt_json(trimmed),
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "429" in text or "resource_exhausted" in text or "quota" in text
