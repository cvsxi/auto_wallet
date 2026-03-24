from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta


DATE_FORMAT = "%Y-%m-%d"


class ReportArgumentError(ValueError):
    pass


@dataclass(slots=True)
class DateRange:
    start: datetime | None
    end: datetime | None
    label: str


def parse_range_args(
    args: list[str],
    fallback_days: int,
    now: datetime | None = None,
) -> DateRange:
    current = now or datetime.now(UTC)

    if not args:
        start = (current - timedelta(days=fallback_days)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return DateRange(start=start, end=current, label=f"останні {fallback_days} днів")

    keyword = args[0].lower()
    if len(args) == 1 and keyword == "all":
        return DateRange(start=None, end=None, label="весь період")
    if len(args) == 1 and keyword == "today":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return DateRange(start=start, end=current, label="сьогодні")
    if len(args) == 1 and keyword == "week":
        start = (current - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return DateRange(start=start, end=current, label="останні 7 днів")
    if len(args) == 1 and keyword == "month":
        start = (current - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        return DateRange(start=start, end=current, label="останні 30 днів")

    if len(args) != 2:
        raise ReportArgumentError(
            "Використовуйте `today`, `week`, `month`, `all` або дві дати у форматі YYYY-MM-DD."
        )

    start_date = datetime.strptime(args[0], DATE_FORMAT).date()
    end_date = datetime.strptime(args[1], DATE_FORMAT).date()
    if end_date < start_date:
        raise ReportArgumentError("Кінцева дата не може бути раніше за початкову.")

    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    end = datetime.combine(end_date, time.max, tzinfo=UTC)
    return DateRange(start=start, end=end, label=f"{start_date.isoformat()}..{end_date.isoformat()}")


def filter_transactions(
    transactions: list[dict],
    date_range: DateRange,
) -> list[dict]:
    if date_range.start is None or date_range.end is None:
        return transactions

    result = []
    for item in transactions:
        tx_time = datetime.fromisoformat(str(item["datetime"]))
        if date_range.start <= tx_time <= date_range.end:
            result.append(item)
    return result


def build_summary_text(transactions: list[dict], label: str) -> str:
    if not transactions:
        return f"Немає операцій за період: {label}."

    by_currency: dict[str, dict[str, int]] = {}
    by_category: dict[str, int] = {}
    holds = 0

    for item in transactions:
        currency = str(item["currency"])
        amount_minor = int(item["amount_minor"])
        entry = by_currency.setdefault(
            currency,
            {"income_minor": 0, "expense_minor": 0, "count": 0},
        )
        if amount_minor >= 0:
            entry["income_minor"] += amount_minor
        else:
            expense_minor = abs(amount_minor)
            entry["expense_minor"] += expense_minor
            by_category[str(item["category"])] = by_category.get(str(item["category"]), 0) + expense_minor
        if item.get("hold"):
            holds += 1
        entry["count"] += 1

    lines = [
        f"Звіт за період: {label}",
        f"Операцій: {len(transactions)}",
        f"Hold-операцій: {holds}",
        "",
        "Підсумки по валютах:",
    ]
    for currency, values in sorted(by_currency.items()):
        income = values["income_minor"]
        expense = values["expense_minor"]
        lines.append(
            f"- {currency}: надходження {_money(income)}, "
            f"витрати {_money(expense)}, баланс {_money(income - expense)}, "
            f"операцій {values['count']}"
        )

    if by_category:
        lines.extend(["", "Найбільші категорії витрат:"])
        top_categories = sorted(by_category.items(), key=lambda item: item[1], reverse=True)[:5]
        for category, amount_minor in top_categories:
            lines.append(f"- {category}: {_money(amount_minor)}")

    return "\n".join(lines)


def build_operations_text(transactions: list[dict], label: str) -> str:
    if not transactions:
        return f"Немає операцій за період: {label}."

    excluded_count = sum(1 for item in transactions if item.get("excluded_from_balance"))
    lines = [
        f"Операції за період: {label}",
        f"Всього: {len(transactions)}, виключено з балансу: {excluded_count}",
    ]
    for item in reversed(transactions):
        amount_minor = int(item["amount_minor"])
        sign = "+" if amount_minor >= 0 else "-"
        status = " [виключено]" if item.get("excluded_from_balance") else ""
        note = f" | примітка: {item['exclusion_note']}" if item.get("exclusion_note") else ""
        lines.append(
            f"- id={item['id']} | {item['datetime'][:19]} | {item['category']}{status} | "
            f"{sign}{_money(abs(amount_minor))} {item['currency']} | "
            f"{item['description'] or 'Без опису'}{note}"
        )
    return "\n".join(lines)


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + 1
        if current and current_length + line_length > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = line_length
        else:
            current.append(line)
            current_length += line_length

    if current:
        chunks.append("\n".join(current))
    return chunks


def _money(amount_minor: int) -> str:
    return f"{amount_minor / 100:.2f}"
