from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from secret_box import SecretBox


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _money_from_minor_units(amount: int) -> str:
    return f"{amount / 100:.2f}"


def _currency_name(code: int | None) -> str:
    mapping = {
        980: "UAH",
        840: "USD",
        978: "EUR",
        985: "PLN",
        826: "GBP",
    }
    if code is None:
        return "UNK"
    return mapping.get(code, str(code))


MANUAL_TRANSACTION_FIELDS = (
    "excluded_from_balance",
    "excluded_from_balance_at",
    "exclusion_note",
)


EXACT_MCC_CATEGORIES: dict[int, str] = {
    4111: "Транспорт",
    4121: "Таксі",
    4131: "Транспорт",
    4784: "Платні дороги",
    4814: "Телеком",
    4816: "Телеком",
    4829: "Перекази",
    4899: "Цифрові сервіси",
    4900: "Комунальні послуги",
    5200: "Дім та ремонт",
    5211: "Будівельні матеріали",
    5251: "Господарські товари",
    5261: "Сади та рослини",
    5300: "Універсальні магазини",
    5310: "Універсальні магазини",
    5311: "Універсальні магазини",
    5331: "Дискаунтери",
    5411: "Продукти",
    5422: "М'ясо та риба",
    5441: "Кондитерські",
    5451: "Молочні продукти",
    5462: "Пекарні",
    5499: "Продукти",
    5532: "Автозапчастини",
    5533: "Автотовари",
    5541: "Пальне",
    5542: "Пальне",
    5611: "Одяг",
    5621: "Жіночий одяг",
    5631: "Аксесуари",
    5641: "Дитячий одяг",
    5651: "Одяг",
    5655: "Спортивний одяг",
    5661: "Взуття",
    5691: "Чоловічий одяг",
    5699: "Одяг",
    5712: "Меблі",
    5714: "Побутова техніка",
    5722: "Техніка",
    5732: "Електроніка",
    5734: "Комп'ютери",
    5735: "Ігри та софт",
    5811: "Кейтеринг",
    5812: "Ресторани",
    5813: "Бари",
    5814: "Фастфуд",
    5912: "Аптеки",
    5921: "Алкоголь",
    5941: "Спорттовари",
    5942: "Книги",
    5944: "Ювелірні вироби",
    5945: "Іграшки",
    5947: "Подарунки",
    5948: "Шкіряні вироби",
    5964: "Маркетплейси",
    5968: "Підписки",
    5977: "Косметика",
    5992: "Квіти",
    5993: "Тютюн",
    5994: "Преса",
    5995: "Тварини",
    5999: "Різне",
    6010: "Фінансові операції",
    6011: "Готівка",
    6012: "Фінансові операції",
    6051: "Квазікеш",
    6211: "Інвестиції",
    6300: "Страхування",
    6513: "Оренда",
    7011: "Готелі",
    7210: "Пральні та хімчистка",
    7211: "Пральні та хімчистка",
    7221: "Фотопослуги",
    7230: "Краса",
    7298: "Здоров'я та краса",
    7299: "Послуги",
    7311: "Реклама",
    7372: "IT-послуги",
    7399: "Бізнес-послуги",
    7512: "Оренда авто",
    7523: "Паркінг",
    7531: "Автосервіс",
    7538: "Автосервіс",
    7542: "Мийка авто",
    7549: "Автопослуги",
    7832: "Кіно",
    7841: "Кіно",
    7911: "Танці та студії",
    7922: "Театри",
    7929: "Розваги",
    7932: "Більярд та боулінг",
    7933: "Зали та спорт",
    7941: "Спортклуби",
    7991: "Туризм",
    7995: "Ігри та розваги",
    7997: "Клуби",
    8011: "Лікарі",
    8021: "Стоматологія",
    8043: "Оптика",
    8062: "Лікарні",
    8099: "Медицина",
    8111: "Юридичні послуги",
    8211: "Освіта",
    8220: "Освіта",
    8241: "Освіта",
    8299: "Освіта",
    8351: "Догляд за дітьми",
    8398: "Благодійність",
    8641: "Громадські організації",
    8661: "Релігійні організації",
    8699: "Громадські організації",
    8999: "Професійні послуги",
    9311: "Податки",
    9399: "Держпослуги",
    9402: "Пошта",
}


def classify_category(amount_minor: int, mcc: int | None, description: str) -> str:
    normalized = description.lower()

    if amount_minor > 0:
        if "зарплат" in normalized:
            return "Зарплата"
        if "кешбек" in normalized:
            return "Кешбек"
        if "переказ" in normalized or "transfer" in normalized:
            return "Перекази"
        return "Надходження"

    if mcc in EXACT_MCC_CATEGORIES:
        return EXACT_MCC_CATEGORIES[mcc]

    if mcc is None:
        return "Інші витрати"

    if 3000 <= mcc <= 3299:
        return "Авіаперельоти"
    if 3351 <= mcc <= 3500:
        return "Оренда авто та проживання"
    if 3501 <= mcc <= 3999:
        return "Подорожі"
    if 4000 <= mcc <= 4799:
        return "Транспорт"
    if 5000 <= mcc <= 5599:
        return "Покупки"
    if 5600 <= mcc <= 5699:
        return "Одяг"
    if 5700 <= mcc <= 5799:
        return "Дім та техніка"
    if 5800 <= mcc <= 5899:
        return "Їжа та напої"
    if 5900 <= mcc <= 5999:
        return "Роздрібні покупки"
    if 6000 <= mcc <= 6499:
        return "Фінанси"
    if 6500 <= mcc <= 6999:
        return "Послуги"
    if 7000 <= mcc <= 7299:
        return "Подорожі та сервіси"
    if 7300 <= mcc <= 7999:
        return "Розваги та сервіси"
    if 8000 <= mcc <= 8999:
        return "Здоров'я, освіта та послуги"
    if 9000 <= mcc <= 9999:
        return "Держпослуги та інше"
    return "Інші витрати"


@dataclass(slots=True)
class JsonStorage:
    path: Path
    secret_box: SecretBox

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "updated_at": None,
                "client": {},
                "accounts": [],
                "transactions": [],
                "stats": {},
            }

        with self.path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if isinstance(payload, str):
            return dict(self.secret_box.decrypt_json(payload))

        if isinstance(payload, dict):
            self._write_payload(payload)
            return payload

        raise ValueError("Unsupported storage format.")

    def save_snapshot(
        self,
        client_info: dict[str, Any],
        transactions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int]:
        existing_snapshot = self.load()
        existing_transactions = existing_snapshot.get("transactions", [])
        accounts, account_names = self._build_accounts(client_info)

        normalized_transactions = self._normalize_transactions(
            transactions=transactions,
            account_names=account_names,
        )
        merged_transactions = self._merge_transactions(
            existing_transactions=existing_transactions,
            new_transactions=normalized_transactions,
        )
        payload = {
            "updated_at": _utc_now_iso(),
            "client": {
                "clientId": client_info.get("clientId"),
                "name": client_info.get("name"),
                "permissions": client_info.get("permissions"),
            },
            "accounts": accounts,
            "transactions": merged_transactions,
            "stats": self._build_stats(merged_transactions),
        }

        self._write_payload(payload)

        return payload, len(normalized_transactions)

    def save_accounts(self, client_info: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.load()
        accounts, _ = self._build_accounts(client_info)
        transactions = snapshot.get("transactions", [])
        payload = {
            "updated_at": _utc_now_iso(),
            "client": {
                "clientId": client_info.get("clientId"),
                "name": client_info.get("name"),
                "permissions": client_info.get("permissions"),
            },
            "accounts": accounts,
            "transactions": transactions,
            "stats": self._build_stats(transactions),
        }

        self._write_payload(payload)

        return payload

    def append_transactions(
        self,
        transactions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        snapshot = self.load()
        accounts = snapshot.get("accounts", [])
        if not accounts:
            raise ValueError("Немає збережених рахунків. Спочатку виконайте save_snapshot або /sync.")

        account_names = {
            str(account["id"]): str(account["title"])
            for account in accounts
        }
        normalized_transactions = self._normalize_transactions(
            transactions=transactions,
            account_names=account_names,
        )
        before_ids = {str(item["id"]) for item in snapshot.get("transactions", [])}
        merged_transactions = self._merge_transactions(
            existing_transactions=snapshot.get("transactions", []),
            new_transactions=normalized_transactions,
        )
        added_transactions = [
            item for item in merged_transactions if str(item["id"]) not in before_ids
        ]

        payload = {
            "updated_at": _utc_now_iso(),
            "client": snapshot.get("client", {}),
            "accounts": accounts,
            "transactions": merged_transactions,
            "stats": self._build_stats(merged_transactions),
        }

        self._write_payload(payload)

        return payload, added_transactions

    def append_manual_transaction(
        self,
        amount_minor: int,
        category: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.load()
        accounts = snapshot.get("accounts", [])
        if not accounts:
            raise ValueError("Немає збережених рахунків. Перепідключіть Monobank через /connect.")

        account = self._resolve_account(accounts, account_id)
        account_id = str(account["id"])
        currency_code = int(account.get("currencyCode") or 980)
        timestamp = int(datetime.now(UTC).timestamp())
        transaction_id = f"manual-{timestamp}-{uuid4().hex[:8]}"

        _, added_transactions = self.append_transactions(
            [
                {
                    "id": transaction_id,
                    "account_id": account_id,
                    "time": timestamp,
                    "description": "Додано вручну",
                    "comment": None,
                    "counterName": None,
                    "mcc": None,
                    "originalMcc": None,
                    "hold": False,
                    "amount": amount_minor,
                    "operationAmount": amount_minor,
                    "currencyCode": currency_code,
                    "operationCurrencyCode": currency_code,
                    "cashbackAmount": 0,
                    "commissionRate": 0,
                    "balance": 0,
                    "category": category,
                }
            ]
        )
        transaction = next(
            (
                item
                for item in added_transactions
                if str(item.get("id")) == transaction_id
            ),
            None,
        )
        if transaction is None:
            raise ValueError("Не вдалося додати ручну транзакцію.")
        transaction["category"] = category
        return transaction

    def set_transaction_excluded(
        self,
        transaction_id: str,
        excluded: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.load()
        transactions = snapshot.get("transactions", [])
        updated = False
        normalized_id = str(transaction_id)

        for item in transactions:
            if str(item.get("id")) != normalized_id:
                continue

            item["excluded_from_balance"] = excluded
            if excluded:
                item["excluded_from_balance_at"] = _utc_now_iso()
                item["exclusion_note"] = (note or "").strip() or None
            else:
                item["excluded_from_balance_at"] = None
                item["exclusion_note"] = None
            updated = True
            break

        if not updated:
            raise ValueError("Транзакцію не знайдено.")

        payload = {
            "updated_at": _utc_now_iso(),
            "client": snapshot.get("client", {}),
            "accounts": snapshot.get("accounts", []),
            "transactions": transactions,
            "stats": self._build_stats(transactions),
        }
        self._write_payload(payload)
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(
                self.secret_box.encrypt_json(payload),
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )

    def _normalize_transactions(
        self,
        transactions: list[dict[str, Any]],
        account_names: dict[str, str],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}

        for item in transactions:
            amount_minor = int(item.get("amount", 0))
            currency_code = item.get("currencyCode")
            operation_currency_code = item.get("operationCurrencyCode", currency_code)
            account_id = str(item.get("account_id", "unknown"))
            description = str(item.get("description", "")).strip()
            category = str(
                item.get("category")
                or classify_category(
                    amount_minor=amount_minor,
                    mcc=item.get("mcc"),
                    description=description,
                )
            )

            normalized = {
                "id": item.get("id"),
                "account_id": account_id,
                "account_name": account_names.get(account_id, account_id),
                "time": item.get("time"),
                "datetime": datetime.fromtimestamp(
                    int(item.get("time", 0)), tz=UTC
                ).isoformat(),
                "description": description,
                "comment": item.get("comment"),
                "counterName": item.get("counterName"),
                "mcc": item.get("mcc"),
                "originalMcc": item.get("originalMcc"),
                "hold": bool(item.get("hold", False)),
                "direction": "income" if amount_minor > 0 else "expense",
                "category": category,
                "amount_minor": amount_minor,
                "amount": _money_from_minor_units(amount_minor),
                "operation_amount_minor": int(item.get("operationAmount", amount_minor)),
                "operation_amount": _money_from_minor_units(
                    int(item.get("operationAmount", amount_minor))
                ),
                "currency_code": currency_code,
                "currency": _currency_name(currency_code),
                "operation_currency_code": operation_currency_code,
                "operation_currency": _currency_name(operation_currency_code),
                "cashback_amount_minor": int(item.get("cashbackAmount", 0)),
                "cashback_amount": _money_from_minor_units(
                    int(item.get("cashbackAmount", 0))
                ),
                "commission_rate_minor": int(item.get("commissionRate", 0)),
                "commission_rate": _money_from_minor_units(
                    int(item.get("commissionRate", 0))
                ),
                "balance_minor": int(item.get("balance", 0)),
                "balance": _money_from_minor_units(int(item.get("balance", 0))),
                "excluded_from_balance": bool(item.get("excluded_from_balance", False)),
                "excluded_from_balance_at": item.get("excluded_from_balance_at"),
                "exclusion_note": item.get("exclusion_note"),
            }
            deduped[str(normalized["id"])] = normalized

        return sorted(
            deduped.values(),
            key=lambda item: (int(item["time"]), str(item["id"])),
        )

    def _build_stats(self, transactions: list[dict[str, Any]]) -> dict[str, Any]:
        total_income_minor = 0
        total_expense_minor = 0
        by_category: dict[str, dict[str, int]] = {}
        by_account: dict[str, dict[str, int]] = {}
        by_currency: dict[str, dict[str, int]] = {}

        for item in transactions:
            if item.get("excluded_from_balance"):
                continue
            amount_minor = int(item["amount_minor"])
            account_name = str(item["account_name"])
            category = str(item["category"])
            currency = str(item["currency"])

            if amount_minor >= 0:
                total_income_minor += amount_minor
            else:
                total_expense_minor += abs(amount_minor)

            category_entry = by_category.setdefault(
                category,
                {"income_minor": 0, "expense_minor": 0, "count": 0},
            )
            account_entry = by_account.setdefault(
                account_name,
                {"income_minor": 0, "expense_minor": 0, "count": 0},
            )
            currency_entry = by_currency.setdefault(
                currency,
                {"income_minor": 0, "expense_minor": 0, "count": 0},
            )

            if amount_minor >= 0:
                category_entry["income_minor"] += amount_minor
                account_entry["income_minor"] += amount_minor
                currency_entry["income_minor"] += amount_minor
            else:
                category_entry["expense_minor"] += abs(amount_minor)
                account_entry["expense_minor"] += abs(amount_minor)
                currency_entry["expense_minor"] += abs(amount_minor)

            category_entry["count"] += 1
            account_entry["count"] += 1
            currency_entry["count"] += 1

        single_currency = len(by_currency) <= 1

        return {
            "transactions_count": len(transactions),
            "excluded_transactions_count": sum(
                1 for item in transactions if item.get("excluded_from_balance")
            ),
            "included_transactions_count": sum(
                1 for item in transactions if not item.get("excluded_from_balance")
            ),
            "currencies": sorted(by_currency.keys()),
            "total_income_minor": total_income_minor if single_currency else None,
            "total_income": _money_from_minor_units(total_income_minor) if single_currency else None,
            "total_expense_minor": total_expense_minor if single_currency else None,
            "total_expense": _money_from_minor_units(total_expense_minor) if single_currency else None,
            "net_minor": (total_income_minor - total_expense_minor) if single_currency else None,
            "net": _money_from_minor_units(total_income_minor - total_expense_minor) if single_currency else None,
            "by_currency": {
                currency: {
                    **values,
                    "income": _money_from_minor_units(values["income_minor"]),
                    "expense": _money_from_minor_units(values["expense_minor"]),
                    "net_minor": values["income_minor"] - values["expense_minor"],
                    "net": _money_from_minor_units(values["income_minor"] - values["expense_minor"]),
                }
                for currency, values in sorted(by_currency.items())
            },
            "by_category": {
                category: {
                    **values,
                    "income": _money_from_minor_units(values["income_minor"]),
                    "expense": _money_from_minor_units(values["expense_minor"]),
                }
                for category, values in sorted(
                    by_category.items(),
                    key=lambda item: (
                        item[1]["expense_minor"] + item[1]["income_minor"],
                        item[1]["count"],
                    ),
                    reverse=True,
                )
            },
            "by_account": {
                account: {
                    **values,
                    "income": _money_from_minor_units(values["income_minor"]),
                    "expense": _money_from_minor_units(values["expense_minor"]),
                }
                for account, values in sorted(
                    by_account.items(),
                    key=lambda item: item[1]["count"],
                    reverse=True,
                )
            },
        }

    @staticmethod
    def _merge_transactions(
        existing_transactions: list[dict[str, Any]],
        new_transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        for item in existing_transactions:
            merged[str(item["id"])] = item
        for item in new_transactions:
            existing = merged.get(str(item["id"]))
            if existing is not None:
                for field in MANUAL_TRANSACTION_FIELDS:
                    if field in existing:
                        item[field] = existing.get(field)
            merged[str(item["id"])] = item

        return sorted(
            merged.values(),
            key=lambda item: (int(item["time"]), str(item["id"])),
        )

    @staticmethod
    def _resolve_account(
        accounts: list[dict[str, Any]],
        account_id: str | None,
    ) -> dict[str, Any]:
        if account_id is not None:
            for account in accounts:
                if str(account.get("id")) == str(account_id):
                    return account
        return accounts[0]

    def _build_accounts(
        self,
        client_info: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        accounts = []
        account_names: dict[str, str] = {}

        for account in client_info.get("accounts", []):
            title = self._account_title(account, jar=False)
            account_names[account["id"]] = title
            accounts.append(
                {
                    "id": account["id"],
                    "sendId": account.get("sendId"),
                    "title": title,
                    "currencyCode": account.get("currencyCode"),
                    "currency": _currency_name(account.get("currencyCode")),
                    "type": account.get("type"),
                    "maskedPan": account.get("maskedPan", []),
                    "iban": account.get("iban"),
                    "kind": "account",
                }
            )

        for jar in client_info.get("jars", []):
            title = self._account_title(jar, jar=True)
            account_names[jar["id"]] = title
            accounts.append(
                {
                    "id": jar["id"],
                    "sendId": jar.get("sendId"),
                    "title": title,
                    "currencyCode": jar.get("currencyCode"),
                    "currency": _currency_name(jar.get("currencyCode")),
                    "type": "jar",
                    "kind": "jar",
                }
            )

        return accounts, account_names

    @staticmethod
    def _account_title(account: dict[str, Any], jar: bool) -> str:
        if jar:
            return account.get("title") or account.get("description") or account["id"]

        masked_pan = account.get("maskedPan", [])
        if masked_pan:
            return f"{account.get('type', 'card')} {masked_pan[0]}"
        return account.get("iban") or account["id"]
