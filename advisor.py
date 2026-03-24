from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def build_daily_analysis_text(
    transactions: list[dict],
    timezone_name: str,
    target_date: date | None = None,
) -> str:
    zone = _safe_zone(timezone_name)
    now_local = datetime.now(zone)
    day = target_date or now_local.date()
    today_transactions = _transactions_for_local_day(transactions, day, zone)

    if not today_transactions:
        return (
            f"Щоденний фінансовий аналіз за {day.isoformat()}\n"
            "Операцій за день не знайдено.\n"
            "Стан: спокійний день без руху коштів."
        )

    prev_days = [day - timedelta(days=index) for index in range(1, 8)]
    prev_transactions = [
        item
        for item in transactions
        if _local_date(item, zone) in prev_days
    ]

    by_currency: dict[str, dict[str, int]] = defaultdict(
        lambda: {"income_minor": 0, "expense_minor": 0, "count": 0}
    )
    expense_by_category: dict[str, int] = defaultdict(int)
    small_expenses_minor = 0
    small_expenses_count = 0
    holds = 0

    for item in today_transactions:
        amount_minor = int(item["amount_minor"])
        currency = str(item["currency"])
        entry = by_currency[currency]
        if amount_minor >= 0:
            entry["income_minor"] += amount_minor
        else:
            expense_minor = abs(amount_minor)
            entry["expense_minor"] += expense_minor
            expense_by_category[str(item["category"])] += expense_minor
            if expense_minor <= 20000:
                small_expenses_minor += expense_minor
                small_expenses_count += 1
        if item.get("hold"):
            holds += 1
        entry["count"] += 1

    status = "під контролем"
    advice: list[str] = []

    prev_expense_by_currency: dict[str, list[int]] = defaultdict(list)
    for prev_day in prev_days:
        day_totals: dict[str, int] = defaultdict(int)
        for item in prev_transactions:
            if _local_date(item, zone) != prev_day:
                continue
            amount_minor = int(item["amount_minor"])
            if amount_minor < 0:
                day_totals[str(item["currency"])] += abs(amount_minor)
        for currency, total in day_totals.items():
            prev_expense_by_currency[currency].append(total)

    for currency, totals in sorted(by_currency.items()):
        income = totals["income_minor"]
        expense = totals["expense_minor"]
        net = income - expense

        avg_prev_expense = 0
        previous_values = prev_expense_by_currency.get(currency, [])
        if previous_values:
            avg_prev_expense = sum(previous_values) / len(previous_values)

        if net < 0:
            status = "потрібна увага"
            advice.append(
                f"По {currency} день закрився в мінусі: {_money(net)}."
            )
        if avg_prev_expense and expense > avg_prev_expense * 1.5:
            status = "потрібна увага"
            advice.append(
                f"Витрати по {currency} вищі за ваш середній день за тиждень."
            )

    if expense_by_category:
        top_category, top_expense = max(expense_by_category.items(), key=lambda item: item[1])
        total_expense = sum(expense_by_category.values())
        if total_expense and top_expense / total_expense >= 0.4:
            advice.append(
                f"Найбільше витрат сьогодні було в категорії «{top_category}»."
            )

    total_expense_all = sum(values["expense_minor"] for values in by_currency.values())
    if total_expense_all and small_expenses_count >= 5 and small_expenses_minor / total_expense_all >= 0.15:
        advice.append(
            "Було багато дрібних витрат. Перевірте непомітні повторювані покупки."
        )

    if holds:
        advice.append(
            f"Є hold-операції ({holds}). Частина списань ще може змінитися."
        )

    if not advice:
        advice.append("День виглядає стабільно, різких відхилень не видно.")

    lines = [
        f"Щоденний фінансовий аналіз за {day.isoformat()}",
        f"Стан: {status}",
        "",
        "Підсумки за день:",
    ]

    for currency, totals in sorted(by_currency.items()):
        income = totals["income_minor"]
        expense = totals["expense_minor"]
        net = income - expense
        lines.append(
            f"- {currency}: надходження {_money(income)}, "
            f"витрати {_money(expense)}, баланс {_money(net)}, операцій {totals['count']}"
        )

    lines.extend(["", "Поради:"])
    for item in advice[:5]:
        lines.append(f"- {item}")

    return "\n".join(lines)


def build_daily_analysis_payload(
    transactions: list[dict],
    timezone_name: str,
    target_date: date | None = None,
) -> dict:
    zone = _safe_zone(timezone_name)
    now_local = datetime.now(zone)
    day = target_date or now_local.date()
    today_transactions = _transactions_for_local_day(transactions, day, zone)
    prev_days = [day - timedelta(days=index) for index in range(1, 8)]

    by_currency: dict[str, dict[str, int]] = defaultdict(
        lambda: {"income_minor": 0, "expense_minor": 0, "count": 0}
    )
    by_category: dict[str, int] = defaultdict(int)
    top_operations: list[dict] = []
    holds = 0

    for item in today_transactions:
        amount_minor = int(item["amount_minor"])
        currency = str(item["currency"])
        currency_entry = by_currency[currency]
        if amount_minor >= 0:
            currency_entry["income_minor"] += amount_minor
        else:
            expense_minor = abs(amount_minor)
            currency_entry["expense_minor"] += expense_minor
            by_category[str(item["category"])] += expense_minor
        if item.get("hold"):
            holds += 1
        currency_entry["count"] += 1

        top_operations.append(
            {
                "datetime": str(item["datetime"])[:19],
                "category": str(item["category"]),
                "currency": currency,
                "amount": round(amount_minor / 100, 2),
                "direction": "income" if amount_minor >= 0 else "expense",
                "hold": bool(item.get("hold")),
            }
        )

    top_operations = sorted(
        top_operations,
        key=lambda item: abs(float(item["amount"])),
        reverse=True,
    )[:5]

    weekly_expense_avg_by_currency: dict[str, float] = {}
    for currency in by_currency:
        totals: list[int] = []
        for prev_day in prev_days:
            total = 0
            for item in transactions:
                if _local_date(item, zone) != prev_day:
                    continue
                if str(item["currency"]) != currency:
                    continue
                amount_minor = int(item["amount_minor"])
                if amount_minor < 0:
                    total += abs(amount_minor)
            if total:
                totals.append(total)
        weekly_expense_avg_by_currency[currency] = round((sum(totals) / len(totals)) / 100, 2) if totals else 0.0

    return {
        "date": day.isoformat(),
        "timezone": timezone_name,
        "transactions_count": len(today_transactions),
        "hold_transactions_count": holds,
        "by_currency": {
            currency: {
                "income": round(values["income_minor"] / 100, 2),
                "expense": round(values["expense_minor"] / 100, 2),
                "net": round((values["income_minor"] - values["expense_minor"]) / 100, 2),
                "count": values["count"],
                "avg_daily_expense_last_7_days": weekly_expense_avg_by_currency.get(currency, 0.0),
            }
            for currency, values in sorted(by_currency.items())
        },
        "top_expense_categories": [
            {"category": category, "expense": round(amount_minor / 100, 2)}
            for category, amount_minor in sorted(
                by_category.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        ],
        "largest_operations": top_operations,
    }


def build_period_analysis_payload(
    transactions: list[dict],
    timezone_name: str,
    label: str,
) -> dict:
    by_currency: dict[str, dict[str, int]] = defaultdict(
        lambda: {"income_minor": 0, "expense_minor": 0, "count": 0}
    )
    by_category: dict[str, int] = defaultdict(int)
    top_operations: list[dict] = []
    holds = 0

    for item in transactions:
        amount_minor = int(item["amount_minor"])
        currency = str(item["currency"])
        currency_entry = by_currency[currency]
        if amount_minor >= 0:
            currency_entry["income_minor"] += amount_minor
        else:
            expense_minor = abs(amount_minor)
            currency_entry["expense_minor"] += expense_minor
            by_category[str(item["category"])] += expense_minor
        if item.get("hold"):
            holds += 1
        currency_entry["count"] += 1

        top_operations.append(
            {
                "datetime": str(item["datetime"])[:19],
                "category": str(item["category"]),
                "currency": currency,
                "amount": round(amount_minor / 100, 2),
                "direction": "income" if amount_minor >= 0 else "expense",
                "hold": bool(item.get("hold")),
            }
        )

    top_operations = sorted(
        top_operations,
        key=lambda item: abs(float(item["amount"])),
        reverse=True,
    )[:7]

    return {
        "period": label,
        "timezone": timezone_name,
        "transactions_count": len(transactions),
        "hold_transactions_count": holds,
        "by_currency": {
            currency: {
                "income": round(values["income_minor"] / 100, 2),
                "expense": round(values["expense_minor"] / 100, 2),
                "net": round((values["income_minor"] - values["expense_minor"]) / 100, 2),
                "count": values["count"],
            }
            for currency, values in sorted(by_currency.items())
        },
        "top_expense_categories": [
            {"category": category, "expense": round(amount_minor / 100, 2)}
            for category, amount_minor in sorted(
                by_category.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:7]
        ],
        "largest_operations": top_operations,
    }


def _transactions_for_local_day(
    transactions: list[dict],
    day: date,
    zone: tzinfo,
) -> list[dict]:
    return [item for item in transactions if _local_date(item, zone) == day]


def _local_date(transaction: dict, zone: tzinfo) -> date:
    dt = datetime.fromisoformat(str(transaction["datetime"]))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone).date()


def _safe_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or UTC


def _money(amount_minor: int | float) -> str:
    return f"{amount_minor / 100:.2f}"
