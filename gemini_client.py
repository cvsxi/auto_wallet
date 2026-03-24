from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GeminiAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class GeminiClient:
    api_key: str
    model: str = "gemini-2.5-flash"

    def generate_daily_analysis(self, summary_payload: dict[str, Any]) -> str:
        prompt = self._build_prompt(summary_payload, mode="daily")
        payload = {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "Ти фінансовий асистент. Відповідай українською. "
                            "Працюй тільки з наданими даними, не вигадуй фактів."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.6,
                "topP": 0.9,
                "maxOutputTokens": 700,
                "thinkingConfig": {
                    "thinkingBudget": 0
                },
            },
        }

        raw = self._post_json(payload)
        text = self._extract_text(raw)
        if not text:
            raise GeminiAPIError("Gemini повернув порожню відповідь.")
        return text.strip()

    def generate_period_analysis(self, summary_payload: dict[str, Any]) -> str:
        prompt = self._build_prompt(summary_payload, mode="period")
        payload = {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "Ти фінансовий асистент. Відповідай українською. "
                            "Працюй тільки з наданими даними, не вигадуй фактів."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.6,
                "topP": 0.9,
                "maxOutputTokens": 900,
                "thinkingConfig": {
                    "thinkingBudget": 0
                },
            },
        }

        raw = self._post_json(payload)
        text = self._extract_text(raw)
        if not text:
            raise GeminiAPIError("Gemini повернув порожню відповідь.")
        return text.strip()

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(self.model)}:generateContent?key={quote(self.api_key)}"
        )
        request = Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GeminiAPIError(f"Gemini API error {exc.code}: {body}") from exc
        except URLError as exc:
            raise GeminiAPIError(f"Gemini API недоступний: {exc}") from exc

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates", [])
        parts: list[str] = []
        for candidate in candidates:
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()

    @staticmethod
    def _build_prompt(summary_payload: dict[str, Any], mode: str) -> str:
        if mode == "daily":
            intro = "Зроби щоденний аналіз особистих фінансів за наданими агрегованими даними."
            label = "Дані дня"
        else:
            intro = "Зроби аналіз особистих фінансів за вказаний період на основі агрегованих даних."
            label = "Дані періоду"

        return (
            f"{intro}\n"
            "Потрібний формат відповіді:\n"
            "1. Заголовок з датою або періодом.\n"
            "2. Короткий висновок 1-2 речення.\n"
            "3. Блок 'Підсумки'.\n"
            "4. Блок 'Поради' з 3-5 практичними пунктами.\n"
            "5. Блок 'Ризики' або 'Окремі спостереження'.\n\n"
            "Не згадуй, що ти модель. Якщо даних мало, так і скажи, але все одно дай корисні висновки.\n\n"
            f"{label}:\n{json.dumps(summary_payload, ensure_ascii=False, indent=2)}"
        )
