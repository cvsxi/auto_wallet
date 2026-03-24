from __future__ import annotations

import json
import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from advisor import (
    build_daily_analysis_payload,
    build_daily_analysis_text,
    build_period_analysis_payload,
)
from config import Settings
from gemini_client import GeminiAPIError
from gemini_router import GeminiRouter
from monobank_client import MonobankAPIError, MonobankClient
from reporting import (
    ReportArgumentError,
    build_operations_text,
    build_summary_text,
    chunk_text,
    filter_transactions,
    parse_range_args,
)
from secret_box import SecretBox
from storage import JsonStorage
from telegram_api import TelegramAPIError, TelegramBotAPI
from user_profiles import UserProfile, UserRegistry, utc_now_iso


@dataclass(slots=True)
class MonobankTelegramBot:
    settings: Settings
    telegram: TelegramBotAPI
    registry: UserRegistry
    _offset: int | None = None
    _monitor_thread: threading.Thread | None = None
    _clients: dict[int, MonobankClient] = field(default_factory=dict)

    def run(self) -> None:
        self.telegram.delete_webhook(drop_pending_updates=False)
        self.telegram.set_commands(
            [
                {"command": "start", "description": "Початок роботи"},
                {"command": "connect", "description": "Підключити Monobank token"},
                {"command": "status", "description": "Статус підключення"},
                {"command": "report", "description": "Фінансовий звіт"},
                {"command": "analysis", "description": "AI-аналіз за період"},
                {"command": "operations", "description": "Список операцій"},
                {"command": "exclude", "description": "Виключити з балансу"},
                {"command": "include", "description": "Повернути в баланс"},
                {"command": "disconnect", "description": "Видалити підключення"},
            ]
        )
        self._migrate_legacy_user()
        self._start_monitor()

        print("Telegram bot started. Waiting for updates...")
        while True:
            updates = self.telegram.get_updates(
                offset=self._offset,
                timeout=self.settings.poll_timeout_seconds,
            )
            for update in updates:
                self._offset = int(update["update_id"]) + 1
                self._handle_update(update)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = int(chat["id"])
        if chat.get("type") != "private":
            self._safe_send(chat_id, "Для безпеки використовуйте бота тільки в приватному чаті.")
            return

        text = str(message.get("text", "")).strip()
        if not text:
            return

        try:
            if text.startswith("/"):
                command, args = self._parse_command(text)
                self._handle_command(chat_id, command, args, message)
                return

            if self._looks_like_monobank_token(text):
                self._connect_user(chat_id, text, message)
                return

            profile = self.registry.get(chat_id)
            manual_transaction = self._parse_manual_transaction_text(text)
            if manual_transaction is not None:
                if profile is None:
                    self._safe_send(chat_id, "Спочатку підключіть Monobank token через /connect.")
                    return

                amount_minor, category = manual_transaction
                self._handle_manual_transaction(chat_id, profile, amount_minor, category)
                return

            if profile is not None:
                self._safe_send(
                    chat_id,
                    "Для ручної транзакції надішліть `+ сума [категорія]` або `- сума [категорія]`. Наприклад: `- 250 їжа`.",
                )
                return

            self._safe_send(
                chat_id,
                "Надішліть Monobank token повідомленням або використайте /connect <token>.",
            )
        except ReportArgumentError as exc:
            self._safe_send(chat_id, f"Помилка аргументів: {exc}")
        except (MonobankAPIError, TelegramAPIError, ValueError) as exc:
            self._safe_send(chat_id, f"Помилка: {exc}")
        except Exception as exc:
            print("Unhandled error:", exc)
            print(traceback.format_exc())
            self._safe_send(chat_id, "Сталася неочікувана помилка. Подивіться лог консолі.")

    def _handle_command(
        self,
        chat_id: int,
        command: str,
        args: list[str],
        message: dict[str, Any],
    ) -> None:
        if command in {"start", "help"}:
            self._send_help(chat_id)
            return
        if command == "connect":
            if not args:
                self._safe_send(chat_id, "Надішліть `/connect <monobank_token>` або просто сам token одним повідомленням.")
                return
            self._connect_user(chat_id, args[0], message)
            return
        if command == "disconnect":
            self._disconnect_user(chat_id)
            return
        if command == "status":
            self._handle_status(chat_id)
            return

        profile = self.registry.get(chat_id)
        if profile is None:
            self._safe_send(chat_id, "Спочатку підключіть Monobank token через /connect.")
            return

        if command == "report":
            self._handle_report(chat_id, profile, args, mode="summary")
        elif command == "analysis":
            self._handle_analysis(chat_id, profile, args)
        elif command == "operations":
            self._handle_report(chat_id, profile, args, mode="operations")
        elif command in {"exclude", "delete"}:
            self._handle_transaction_exclusion(chat_id, profile, args, excluded=True)
        elif command in {"include", "restore"}:
            self._handle_transaction_exclusion(chat_id, profile, args, excluded=False)
        else:
            self._safe_send(chat_id, "Невідома команда. Спробуйте /start.")

    def _connect_user(self, chat_id: int, token: str, message: dict[str, Any]) -> None:
        token = token.strip()
        if not self._looks_like_monobank_token(token):
            raise ValueError("Схоже, це не Monobank token.")

        existing_profile = self.registry.get(chat_id)
        preserve_existing_data = existing_profile is not None and existing_profile.monobank_token == token
        client = MonobankClient(token)
        client_info = client.get_client_info()
        priority_account_id = self._determine_priority_account_id(client_info)
        sender = message.get("from", {})

        if not preserve_existing_data:
            self.registry.clear_user_data(chat_id)

        profile = UserProfile(
            chat_id=chat_id,
            monobank_token=token,
            timezone=self.settings.default_timezone,
            connected_at=existing_profile.connected_at if preserve_existing_data and existing_profile else utc_now_iso(),
            last_daily_report_date=existing_profile.last_daily_report_date if preserve_existing_data and existing_profile else None,
            priority_account_id=priority_account_id,
            telegram_username=sender.get("username"),
            first_name=sender.get("first_name"),
        )
        self.registry.upsert(profile)
        self._clients[chat_id] = client

        storage = self._storage_for(chat_id)
        storage.save_accounts(client_info)
        if not preserve_existing_data:
            self._save_state(profile, self._default_state())

        self._safe_send(
            chat_id,
            "Monobank підключено. Дані ізольовані для цього чату, нові операції підтягуватимуться автоматично, а AI-аналіз доступний по команді /analysis.",
        )

    def _disconnect_user(self, chat_id: int) -> None:
        profile = self.registry.get(chat_id)
        if profile is None:
            self._safe_send(chat_id, "Підключення для цього чату не знайдено.")
            return

        self.registry.remove(chat_id, delete_files=True)
        self._clients.pop(chat_id, None)
        self._safe_send(chat_id, "Підключення видалено. Token і локальні дані цього чату очищені.")

    def _handle_status(self, chat_id: int) -> None:
        profile = self.registry.get(chat_id)
        if profile is None:
            self._safe_send(chat_id, "Monobank ще не підключено. Надішліть token або використайте /connect.")
            return

        snapshot = self._storage_for(chat_id).load()
        state = self._load_state(profile)
        lines = [
            "Статус підключення:",
            f"- chat_id: {chat_id}",
            f"- підключено: {profile.connected_at}",
            f"- часовий пояс: {profile.timezone}",
            f"- рахунків/банок: {len(snapshot.get('accounts', []))}",
            f"- операцій у JSON: {len(snapshot.get('transactions', []))}",
            f"- виключено з балансу: {snapshot.get('stats', {}).get('excluded_transactions_count', 0)}",
            f"- перевірено рахунків: {len(state.get('last_checked_by_account', {}))}",
            f"- AI-аналіз: вручну через /analysis ({profile.timezone})",
        ]
        self._safe_send(chat_id, "\n".join(lines))

    def _handle_report(
        self,
        chat_id: int,
        profile: UserProfile,
        args: list[str],
        mode: str,
    ) -> None:
        snapshot = self._storage_for(profile.chat_id).load()
        transactions = snapshot.get("transactions", [])
        if not transactions:
            self._safe_send(chat_id, "Дані ще не накопичені. Зачекайте трохи після підключення.")
            return

        date_range = parse_range_args(args, fallback_days=self.settings.default_sync_days)
        filtered = filter_transactions(transactions, date_range)
        if mode == "summary":
            active_transactions = self._balance_transactions(filtered)
            excluded_count = len(filtered) - len(active_transactions)
            if active_transactions:
                text = build_summary_text(active_transactions, date_range.label)
                if excluded_count:
                    text += f"\n\nВиключено з балансу: {excluded_count}"
            elif excluded_count:
                text = (
                    f"Немає операцій, що впливають на баланс, за період: {date_range.label}.\n"
                    f"Виключено з балансу: {excluded_count}"
                )
            else:
                text = build_summary_text(active_transactions, date_range.label)
        else:
            text = build_operations_text(filtered, date_range.label)
        self._send_long_message(chat_id, text)

    def _handle_analysis(
        self,
        chat_id: int,
        profile: UserProfile,
        args: list[str],
    ) -> None:
        snapshot = self._storage_for(profile.chat_id).load()
        transactions = snapshot.get("transactions", [])
        if not transactions:
            self._safe_send(chat_id, "Дані ще не накопичені. Зачекайте трохи після підключення.")
            return

        effective_args = args or ["today"]
        date_range = parse_range_args(effective_args, fallback_days=1)
        filtered = filter_transactions(transactions, date_range)
        active_transactions = self._balance_transactions(filtered)
        if not active_transactions:
            excluded_count = len(filtered)
            if excluded_count:
                self._safe_send(
                    chat_id,
                    (
                        f"Немає операцій для AI-аналізу за період: {date_range.label}.\n"
                        f"Усі знайдені операції виключені з балансу: {excluded_count}"
                    ),
                )
            else:
                self._safe_send(chat_id, f"Немає операцій для AI-аналізу за період: {date_range.label}.")
            return

        text = self._build_period_analysis_message(profile, active_transactions, date_range.label)
        self._send_long_message(chat_id, text)

    def _send_help(self, chat_id: int) -> None:
        profile = self.registry.get(chat_id)
        if profile is None:
            text = (
                "Надішліть Monobank token одним повідомленням або використайте `/connect <token>`.\n"
                "Після підключення бот буде:\n"
                "- автоматично відстежувати ваші транзакції;\n"
                "- зберігати ваш JSON окремо від інших користувачів;\n"
                "- будувати AI-аналіз по запиту через /analysis.\n\n"
                "Команди:\n"
                "/connect <token>\n"
                "/status\n"
                "/report [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/analysis [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/operations [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/exclude <transaction_id> [примітка]\n"
                "/include <transaction_id>\n"
                "/disconnect"
            )
        else:
            text = (
                "Monobank уже підключено для цього чату.\n"
                "Команди:\n"
                "/status\n"
                "/report [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/analysis [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/operations [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/exclude <transaction_id> [примітка]\n"
                "/include <transaction_id>\n"
                "/disconnect\n\n"
                "Щоб транзакція лишилася в історії, але не впливала на баланс, використовуйте /exclude."
            )
        text += (
            "\n\nРучне додавання транзакцій:\n"
            "`+ 1200 зарплата`\n"
            "`- 250 їжа`"
        )
        self._safe_send(chat_id, text)

    def _handle_transaction_exclusion(
        self,
        chat_id: int,
        profile: UserProfile,
        args: list[str],
        excluded: bool,
    ) -> None:
        if not args:
            command = "/exclude <transaction_id> [примітка]" if excluded else "/include <transaction_id>"
            self._safe_send(chat_id, f"Використання: {command}")
            return

        transaction_id = args[0]
        note = " ".join(args[1:]).strip() if excluded else None
        storage = self._storage_for(profile.chat_id)
        snapshot = storage.set_transaction_excluded(transaction_id, excluded=excluded, note=note)
        transaction = next(
            (
                item
                for item in snapshot.get("transactions", [])
                if str(item.get("id")) == str(transaction_id)
            ),
            None,
        )
        if transaction is None:
            raise ValueError("Транзакцію не знайдено.")

        action_text = "виключена з балансу" if excluded else "повернута в баланс"
        amount_minor = int(transaction["amount_minor"])
        sign = "+" if amount_minor >= 0 else "-"
        lines = [
            f"Транзакцію {transaction_id} {action_text}.",
            f"{transaction['datetime'][:19]} | {transaction['category']}",
            f"{sign}{abs(amount_minor) / 100:.2f} {transaction['currency']}",
            transaction["description"] or "Без опису",
        ]
        if excluded and note:
            lines.append(f"Примітка: {note}")
        self._safe_send(chat_id, "\n".join(lines))

    def _handle_manual_transaction(
        self,
        chat_id: int,
        profile: UserProfile,
        amount_minor: int,
        category: str,
    ) -> None:
        transaction = self._storage_for(profile.chat_id).append_manual_transaction(
            amount_minor=amount_minor,
            category=category,
            account_id=profile.priority_account_id,
        )
        sign = "+" if amount_minor >= 0 else "-"
        lines = [
            "Ручну транзакцію додано.",
            f"{transaction['datetime'][:19]} | {transaction['category']}",
            f"{sign}{abs(amount_minor) / 100:.2f} {transaction['currency']}",
            transaction["account_name"],
        ]
        self._safe_send(chat_id, "\n".join(lines))

    def _start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="monobank-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while True:
            profiles = self.registry.list_profiles()
            if not profiles:
                self._sleep_seconds(10)
                continue

            for profile in profiles:
                try:
                    seeded_now = self._ensure_accounts_seeded(profile)
                    if not seeded_now:
                        self._monitor_profile(profile)
                except Exception as exc:
                    print(f"Monitor error for chat {profile.chat_id}:", exc)
                    print(traceback.format_exc())

            self._sleep_seconds(5)

    def _ensure_accounts_seeded(self, profile: UserProfile) -> bool:
        snapshot = self._storage_for(profile.chat_id).load()
        if snapshot.get("accounts"):
            return False

        client = self._get_client(profile)
        client_info = client.get_client_info()
        self._storage_for(profile.chat_id).save_accounts(client_info)
        return True

    def _monitor_profile(self, profile: UserProfile) -> None:
        storage = self._storage_for(profile.chat_id)
        snapshot = storage.load()
        accounts = snapshot.get("accounts", [])
        if not accounts:
            return

        state = self._load_state(profile)
        cursor = int(state.get("cursor", 0))
        poll_count = int(state.get("poll_count", 0))
        account = self._select_monitor_account(profile, accounts, cursor, poll_count)
        account_id = str(account["id"])
        state["cursor_account_id"] = account_id

        now = datetime.now(UTC)
        last_checked_raw = state.get("last_checked_by_account", {}).get(account_id)
        if last_checked_raw:
            start_at = datetime.fromisoformat(last_checked_raw) - timedelta(minutes=5)
        else:
            start_at = now - timedelta(minutes=self.settings.monitor_initial_lookback_minutes)

        items = self._get_client(profile).get_statements(
            account_id=account_id,
            start_at=start_at,
            end_at=now,
        )
        for item in items:
            item["account_id"] = account_id

        _, added_transactions = storage.append_transactions(items)
        known_ids = set(state.get("notified_transaction_ids", []))
        fresh_transactions = [
            item
            for item in added_transactions
            if str(item["id"]) not in known_ids
        ]
        for transaction in fresh_transactions:
            self._notify_transaction(profile, transaction)
            known_ids.add(str(transaction["id"]))

        state.setdefault("last_checked_by_account", {})[account_id] = now.isoformat()
        if account_id != profile.priority_account_id:
            state["cursor"] = self._next_secondary_cursor(profile, accounts, cursor)
        else:
            state["cursor"] = cursor
        state["poll_count"] = poll_count + 1
        state["cursor_account_id"] = self._peek_next_account_id(
            profile,
            accounts,
            int(state["cursor"]),
            int(state["poll_count"]),
        )
        state["notified_transaction_ids"] = list(known_ids)[-5000:]
        self._save_state(profile, state)

    def _notify_transaction(self, profile: UserProfile, transaction: dict[str, Any]) -> None:
        amount_minor = int(transaction["amount_minor"])
        sign = "+" if amount_minor >= 0 else "-"
        text = (
            "Нова транзакція\n"
            f"ID: {transaction['id']}\n"
            f"{transaction['datetime'][:19]}\n"
            f"{transaction['category']}\n"
            f"{sign}{abs(amount_minor) / 100:.2f} {transaction['currency']}\n"
            f"{transaction['account_name']}\n"
            f"{transaction['description'] or 'Без опису'}"
        )
        self._safe_send(profile.chat_id, text)

    @staticmethod
    def _balance_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item
            for item in transactions
            if not item.get("excluded_from_balance")
        ]

    def _storage_for(self, chat_id: int) -> JsonStorage:
        return JsonStorage(self.registry.data_file(chat_id), self.registry.secret_box)

    def _load_state(self, profile: UserProfile) -> dict[str, Any]:
        path = self.registry.state_file(profile.chat_id)
        if not path.exists():
            return self._default_state()

        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)

        if isinstance(payload, str):
            return dict(self.registry.secret_box.decrypt_json(payload))

        if isinstance(payload, dict):
            self._save_state(profile, payload)
            return payload

        raise ValueError("Unsupported state format.")

    def _save_state(self, profile: UserProfile, payload: dict[str, Any]) -> None:
        path = self.registry.state_file(profile.chat_id)
        with path.open("w", encoding="utf-8") as file:
            json.dump(
                self.registry.secret_box.encrypt_json(payload),
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "cursor": 0,
            "poll_count": 0,
            "cursor_account_id": None,
            "last_checked_by_account": {},
            "notified_transaction_ids": [],
        }

    def _get_client(self, profile: UserProfile) -> MonobankClient:
        cached = self._clients.get(profile.chat_id)
        if cached is not None and cached.token == profile.monobank_token:
            return cached

        client = MonobankClient(profile.monobank_token)
        self._clients[profile.chat_id] = client
        return client

    def _select_monitor_account(
        self,
        profile: UserProfile,
        accounts: list[dict[str, Any]],
        cursor: int,
        poll_count: int,
    ) -> dict[str, Any]:
        priority_id = profile.priority_account_id
        use_secondary = (
            not priority_id
            or poll_count % self.settings.monitor_secondary_every_cycles
            == self.settings.monitor_secondary_every_cycles - 1
        )

        if use_secondary:
            secondary_accounts = self._secondary_accounts(profile, accounts)
            if secondary_accounts:
                return secondary_accounts[cursor % len(secondary_accounts)]

        for account in accounts:
            if str(account["id"]) == priority_id:
                return account
        return accounts[cursor % len(accounts)]

    def _peek_next_account_id(
        self,
        profile: UserProfile,
        accounts: list[dict[str, Any]],
        cursor: int,
        poll_count: int,
    ) -> str | None:
        if not accounts:
            return None
        return str(self._select_monitor_account(profile, accounts, cursor, poll_count)["id"])

    def _secondary_accounts(
        self,
        profile: UserProfile,
        accounts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            account
            for account in accounts
            if str(account["id"]) != profile.priority_account_id
        ]

    def _next_secondary_cursor(
        self,
        profile: UserProfile,
        accounts: list[dict[str, Any]],
        cursor: int,
    ) -> int:
        secondary_accounts = self._secondary_accounts(profile, accounts)
        if not secondary_accounts:
            return cursor
        return (cursor + 1) % len(secondary_accounts)

    def _determine_priority_account_id(self, client_info: dict[str, Any]) -> str | None:
        accounts = client_info.get("accounts", [])

        for account in accounts:
            if account.get("type") == "black" and account.get("currencyCode") == 980:
                return str(account["id"])
        for account in accounts:
            if account.get("currencyCode") == 980:
                return str(account["id"])
        if accounts:
            return str(accounts[0]["id"])
        jars = client_info.get("jars", [])
        if jars:
            return str(jars[0]["id"])
        return None

    def _migrate_legacy_user(self) -> None:
        token = self.settings.legacy_monobank_token
        chat_id = self.settings.legacy_chat_id
        if not token or chat_id is None:
            return
        if self.registry.get(chat_id) is not None:
            return

        client = MonobankClient(token)
        client_info = client.get_client_info()
        profile = UserProfile(
            chat_id=chat_id,
            monobank_token=token,
            timezone=self.settings.default_timezone,
            connected_at=utc_now_iso(),
            priority_account_id=self.settings.legacy_priority_account_id or self._determine_priority_account_id(client_info),
        )
        self.registry.upsert(profile)
        self._clients[chat_id] = client

        global_data_file = self.settings.registry_file.parent / "monobank_transactions.json"
        global_state_file = self.settings.registry_file.parent / "bot_state.json"
        user_data_file = self.registry.data_file(chat_id)
        user_state_file = self.registry.state_file(chat_id)

        if global_data_file.exists():
            shutil.copy2(global_data_file, user_data_file)
        else:
            self._storage_for(chat_id).save_accounts(client_info)

        if global_state_file.exists():
            shutil.copy2(global_state_file, user_state_file)
        elif not user_state_file.exists():
            self._save_state(profile, self._default_state())

    def _build_daily_analysis_message(
        self,
        profile: UserProfile,
        transactions: list[dict[str, Any]],
        report_date,
    ) -> str:
        fallback_text = build_daily_analysis_text(transactions, profile.timezone, report_date)
        api_key = self.settings.gemini_api_key
        if not api_key or not self.settings.gemini_usage_file:
            return fallback_text

        payload = build_daily_analysis_payload(transactions, profile.timezone, report_date)
        try:
            router = GeminiRouter(
                api_key=api_key,
                models=self.settings.gemini_models,
                usage_path=self.settings.gemini_usage_file,
                secret_box=self.registry.secret_box,
                switch_after_requests=self.settings.gemini_switch_after_requests,
            )
            text, model = router.generate_daily_analysis(payload)
            print(f"Gemini daily analysis for chat {profile.chat_id} via model {model}")
            return text
        except GeminiAPIError as exc:
            print(f"Gemini analysis fallback for chat {profile.chat_id}: {exc}")
            return fallback_text

    def _build_period_analysis_message(
        self,
        profile: UserProfile,
        transactions: list[dict[str, Any]],
        label: str,
    ) -> str:
        fallback_text = build_summary_text(transactions, label)
        api_key = self.settings.gemini_api_key
        if not api_key or not self.settings.gemini_usage_file:
            return fallback_text

        payload = build_period_analysis_payload(transactions, profile.timezone, label)
        try:
            router = GeminiRouter(
                api_key=api_key,
                models=self.settings.gemini_models,
                usage_path=self.settings.gemini_usage_file,
                secret_box=self.registry.secret_box,
                switch_after_requests=self.settings.gemini_switch_after_requests,
            )
            text, model = router.generate_period_analysis(payload)
            print(f"Gemini period analysis for chat {profile.chat_id} via model {model}")
            return text
        except GeminiAPIError as exc:
            print(f"Gemini period analysis fallback for chat {profile.chat_id}: {exc}")
            return fallback_text

    def _send_long_message(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            self._safe_send(chat_id, chunk)

    def _safe_send(self, chat_id: int, text: str) -> None:
        self.telegram.send_message(chat_id, text[:4096])

    @staticmethod
    def _sleep_seconds(seconds: int) -> None:
        threading.Event().wait(seconds)

    @staticmethod
    def _parse_command(text: str) -> tuple[str, list[str]]:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lstrip("/").lower()
        return command, parts[1:]

    @staticmethod
    def _parse_manual_transaction_text(text: str) -> tuple[int, str] | None:
        normalized = text.strip()
        if not normalized or normalized[0] not in "+-":
            return None

        payload = normalized[1:].strip()
        if not payload:
            raise ValueError("Після + або - вкажіть суму. Наприклад: - 250 їжа.")

        parts = payload.split(maxsplit=1)
        amount_text = parts[0].replace(",", ".")
        try:
            amount = Decimal(amount_text)
        except InvalidOperation as exc:
            raise ValueError(
                "Не вдалося розпізнати суму. Приклад: + 1200 зарплата або - 250 їжа."
            ) from exc

        if amount <= 0:
            raise ValueError("Сума має бути більшою за нуль.")

        amount_minor = int(
            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * Decimal("100")
        )
        signed_amount = amount_minor if normalized[0] == "+" else -amount_minor
        default_category = "Ручне надходження" if signed_amount > 0 else "Ручні витрати"
        category = parts[1].strip() if len(parts) > 1 else default_category
        return signed_amount, category

    @staticmethod
    def _looks_like_monobank_token(text: str) -> bool:
        return text.startswith("ud_") and " " not in text and len(text) >= 20


def main() -> None:
    settings = Settings.from_env()
    bot = MonobankTelegramBot(
        settings=settings,
        telegram=TelegramBotAPI(settings.telegram_bot_token),
        registry=UserRegistry(
            settings.registry_file,
            settings.users_dir,
            SecretBox(settings.secrets_key_file),
        ),
    )
    bot.run()


if __name__ == "__main__":
    main()
