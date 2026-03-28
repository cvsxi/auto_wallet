from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class GeminiAnalysisError(RuntimeError):
    pass


@dataclass(slots=True)
class GeminiAdvisor:
    api_key: str
    model_name: str = "gemini-2.5-flash"

    def analyze_period(
        self,
        transactions: list[dict[str, Any]],
        timezone_name: str,
        label: str,
    ) -> str:
        if not self.api_key.strip():
            raise GeminiAnalysisError("Не задано GEMINI_API_KEY.")
        if not transactions:
            raise GeminiAnalysisError("Немає операцій для аналізу.")

        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise GeminiAnalysisError("Бібліотека google-generativeai не встановлена.") from exc

        prompt = _build_prompt(transactions, timezone_name, label)

        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=(
                    "Ти фінансовий асистент. Аналізуй лише надані дані. "
                    "Відповідай українською, коротко, конкретно і без вигадок."
                ),
            )
            response = model.generate_content(prompt)
        except Exception as exc:
            raise GeminiAnalysisError(f"Помилка Gemini: {exc}") from exc

        text = getattr(response, "text", "").strip()
        if not text:
            raise GeminiAnalysisError("Gemini повернув порожню відповідь.")
        return text


def _build_prompt(
    transactions: list[dict[str, Any]],
    timezone_name: str,
    label: str,
) -> str:
    zone = _safe_zone(timezone_name)
    ordered = sorted(transactions, key=lambda item: (str(item.get("datetime")), str(item.get("id"))))
    first_dt = _local_datetime(ordered[0], zone).strftime("%Y-%m-%d %H:%M")
    last_dt = _local_datetime(ordered[-1], zone).strftime("%Y-%m-%d %H:%M")

    by_currency: dict[str, dict[str, int]] = defaultdict(
        lambda: {"income_minor": 0, "expense_minor": 0, "count": 0}
    )
    expense_by_category: dict[str, int] = defaultdict(int)
    holds = 0

    for item in ordered:
        amount_minor = int(item.get("amount_minor", 0))
        currency = str(item.get("currency") or "UNK")
        bucket = by_currency[currency]
        if amount_minor >= 0:
            bucket["income_minor"] += amount_minor
        else:
            expense_minor = abs(amount_minor)
            bucket["expense_minor"] += expense_minor
            expense_by_category[str(item.get("category") or "Інше")] += expense_minor
        if item.get("hold"):
            holds += 1
        bucket["count"] += 1

    currency_lines = []
    for currency, values in sorted(by_currency.items()):
        income_minor = values["income_minor"]
        expense_minor = values["expense_minor"]
        currency_lines.append(
            f"- {currency}: доходи {_money(income_minor)}, витрати {_money(expense_minor)}, "
            f"баланс {_money(income_minor - expense_minor)}, операцій {values['count']}"
        )

    top_categories = sorted(
        expense_by_category.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:5]
    category_lines = [
        f"- {category}: {_money(amount_minor)}"
        for category, amount_minor in top_categories
    ] or ["- Витратних категорій немає."]

    recent_lines = []
    for item in ordered[-12:]:
        amount_minor = int(item.get("amount_minor", 0))
        sign = "+" if amount_minor >= 0 else "-"
        description = str(
            item.get("description")
            or item.get("comment")
            or item.get("counterName")
            or "Без опису"
        ).strip()
        recent_lines.append(
            f"- {_local_datetime(item, zone).strftime('%Y-%m-%d %H:%M')} | "
            f"{item.get('category') or 'Інше'} | {sign}{_money(abs(amount_minor))} "
            f"{item.get('currency') or 'UNK'} | {description[:80]}"
        )

    return "\n".join(
        [
            "Проаналізуй фінансові операції користувача.",
            f"Період: {label}",
            f"Часовий пояс: {timezone_name}",
            f"Операцій: {len(ordered)}",
            f"Початок періоду в локальному часі: {first_dt}",
            f"Кінець періоду в локальному часі: {last_dt}",
            f"Hold-операцій: {holds}",
            "",
            "Підсумки по валютах:",
            *currency_lines,
            "",
            "Найбільші категорії витрат:",
            *category_lines,
            "",
            "Останні операції:",
            *recent_lines,
            "",
            "Побудуй відповідь українською у форматі:",
            "Оцінка:",
            "- 2-3 короткі пункти про загальний стан та динаміку.",
            "Ризики:",
            "- 1-3 конкретні ризики або слабкі місця.",
            "Поради:",
            "- 3-5 практичних порад, що робити далі.",
            "",
            "Правила:",
            "- Не повторюй дослівно сирий звіт.",
            "- Не вигадуй дані, яких немає.",
            "- Не згадуй, що ти модель або ШІ.",
            "- Максимум 12 коротких булітів сумарно.",
        ]
    )


def _local_datetime(transaction: dict[str, Any], zone: tzinfo) -> datetime:
    raw = str(transaction.get("datetime") or "")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone)


def _safe_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or UTC


def _money(amount_minor: int) -> str:
    return f"{amount_minor / 100:.2f}"
