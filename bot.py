from __future__ import annotations

import json
import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from advisor import (
    build_daily_digest_text,
    build_month_comparison_chart,
    build_month_comparison_text,
)
from config import Settings
from gemini_advisor import GeminiAnalysisError, GeminiAdvisor
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
    _storages: dict[int, JsonStorage] = field(default_factory=dict)
    _state_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    _pending_connect_chats: set[int] = field(default_factory=set)
    _menu_contexts: dict[int, str] = field(default_factory=dict)
    _menu_actions: dict[str, str] = field(
        default_factory=lambda: {
            "Підключити": "/connect",
            "Почати": "/start",
            "Статус": "/status",
            "Аналіз": "/analysis_menu",
            "Звіти": "/report_menu",
            "Операції": "/operations_menu",
            "Сповіщення": "/notifications",
            "Миттєво": "/notifications instant",
            "Щоденний звіт": "/notifications daily",
            "Вимкнути повідомлення": "/notifications off",
            "Звіт за сьогодні": "/report today",
            "Звіт за тиждень": "/report week",
            "Звіт за місяць": "/report month",
            "Звіт за весь час": "/report all",
            "Аналіз за сьогодні": "/analysis today",
            "Аналіз за тиждень": "/analysis week",
            "Аналіз за місяць": "/analysis month",
            "Аналіз за весь час": "/analysis all",
            "Операції за сьогодні": "/operations today",
            "Операції за тиждень": "/operations week",
            "Операції за місяць": "/operations month",
            "Усі операції": "/operations all",
            "Назад": "/back",
            "Відключити": "/disconnect",
        }
    )

    def run(self) -> None:
        self.telegram.delete_webhook(drop_pending_updates=False)
        self.telegram.set_commands(
            [
                {"command": "start", "description": "Початок роботи"},
                {"command": "connect", "description": "Підключити Monobank token"},
                {"command": "status", "description": "Статус підключення"},
                {"command": "report", "description": "Фінансовий звіт"},
                {"command": "analysis", "description": "Аналіз за період"},
                {"command": "operations", "description": "Список операцій"},
                {"command": "notifications", "description": "Режим сповіщень"},
                {"command": "exclude", "description": "Виключити з балансу"},
                {"command": "include", "description": "Повернути в баланс"},
                {"command": "disconnect", "description": "Видалити підключення"},
            ]
        )
        self._migrate_legacy_user()
        self._start_monitor()

        print("Telegram bot started. Waiting for updates...")
        while True:
            try:
                updates = self.telegram.get_updates(
                    offset=self._offset,
                    timeout=self.settings.poll_timeout_seconds,
                )
            except TelegramAPIError as exc:
                if self._is_polling_conflict(exc):
                    print("Telegram polling conflict detected. Retrying in 5 seconds...")
                    self._sleep_seconds(5)
                    continue
                raise
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
        text = self._menu_actions.get(text, text)

        try:
            if text.startswith("/"):
                command, args = self._parse_command(text)
                self._handle_command(chat_id, command, args, message)
                return

            if chat_id in self._pending_connect_chats:
                self._connect_user(chat_id, text, message)
                self._pending_connect_chats.discard(chat_id)
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

                amount_minor, category, description = manual_transaction
                self._handle_manual_transaction(
                    chat_id,
                    profile,
                    amount_minor,
                    category,
                    description,
                )
                return

            if profile is not None:
                self._safe_send(
                    chat_id,
                    "Для ручної транзакції надішліть `+ сума [категорія] [назва]` або `- сума [категорія] [назва]`. Наприклад: `- 250 їжа кава`.",
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
            self._set_menu_context(chat_id, "main")
            self._send_help(chat_id)
            return
        if command == "back":
            self._set_menu_context(chat_id, "main")
            self._safe_send(chat_id, "Головне меню.")
            return
        if command == "connect":
            if not args:
                self._pending_connect_chats.add(chat_id)
                self._safe_send(chat_id, "Надішліть одним наступним повідомленням ваш Monobank token.")
                return
            self._pending_connect_chats.discard(chat_id)
            self._connect_user(chat_id, args[0], message)
            return
        if command == "disconnect":
            self._pending_connect_chats.discard(chat_id)
            self._set_menu_context(chat_id, "main")
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
            self._set_menu_context(chat_id, "report")
            self._handle_report(chat_id, profile, args, mode="summary")
        elif command == "report_menu":
            self._set_menu_context(chat_id, "report")
            self._safe_send(chat_id, "Оберіть період звіту.")
        elif command == "analysis":
            self._set_menu_context(chat_id, "analysis")
            self._handle_analysis(chat_id, profile, args)
        elif command == "analysis_menu":
            self._set_menu_context(chat_id, "analysis")
            self._safe_send(chat_id, "Оберіть період аналізу.")
        elif command == "operations":
            self._set_menu_context(chat_id, "operations")
            self._handle_report(chat_id, profile, args, mode="operations")
        elif command == "operations_menu":
            self._set_menu_context(chat_id, "operations")
            self._safe_send(chat_id, "Оберіть період для списку операцій.")
        elif command in {"notifications", "notify"}:
            self._set_menu_context(chat_id, "notifications")
            self._handle_notifications(chat_id, profile, args)
        elif command in {"exclude", "delete"}:
            self._handle_transaction_exclusion(chat_id, profile, args, excluded=True)
        elif command in {"include", "restore"}:
            self._handle_transaction_exclusion(chat_id, profile, args, excluded=False)
        else:
            self._safe_send(chat_id, "Невідома команда. Спробуйте /start.")

    def _connect_user(self, chat_id: int, token: str, message: dict[str, Any]) -> None:
        token = token.strip()
        if not token:
            raise ValueError("Надішліть непорожній Monobank token.")

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
        self._pending_connect_chats.discard(chat_id)

        self._safe_send(
            chat_id,
            "Monobank підключено. Дані ізольовані для цього чату, нові операції підтягуватимуться автоматично, а аналіз доступний по команді /analysis.",
        )

    def _disconnect_user(self, chat_id: int) -> None:
        profile = self.registry.get(chat_id)
        if profile is None:
            self._safe_send(chat_id, "Підключення для цього чату не знайдено.")
            return

        self.registry.remove(chat_id, delete_files=True)
        self._clients.pop(chat_id, None)
        self._storages.pop(chat_id, None)
        self._state_cache.pop(chat_id, None)
        self._menu_contexts.pop(chat_id, None)
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
            f"- сповіщення: {self._notification_mode(state)}",
            f"- аналіз: /analysis + Gemini ({profile.timezone})"
            if self.settings.gemini_api_key
            else f"- аналіз: локальний через /analysis ({profile.timezone})",
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
            state = self._load_state(profile)
            self._remember_operations_view(profile, state, filtered, date_range.label)
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

        state = self._load_state(profile)
        if not args or args == ["today"]:
            today = self._local_today(profile)
            local_text = self._cached_daily_digest(profile, state)
            active_transactions = self._transactions_for_local_day(
                self._balance_transactions(transactions),
                profile,
                today,
            )
            self._send_long_message(
                chat_id,
                self._compose_analysis_text(
                    profile,
                    active_transactions,
                    f"{today.isoformat()} (локальний день)",
                    local_text,
                ),
            )
            self._send_month_comparison_chart(chat_id, profile)
            return
        if len(args) == 1 and args[0].lower() == "month":
            today = self._local_today(profile)
            local_text = self._cached_monthly_report(profile, state)
            active_transactions = self._transactions_for_local_month(
                self._balance_transactions(transactions),
                profile,
                today.replace(day=1),
            )
            self._send_long_message(
                chat_id,
                self._compose_analysis_text(
                    profile,
                    active_transactions,
                    f"{today.strftime('%Y-%m')} (локальний місяць)",
                    local_text,
                ),
            )
            self._send_month_comparison_chart(chat_id, profile)
            return

        effective_args = args
        date_range = parse_range_args(effective_args, fallback_days=1)
        filtered = filter_transactions(transactions, date_range)
        active_transactions = self._balance_transactions(filtered)
        if not active_transactions:
            excluded_count = len(filtered)
            if excluded_count:
                self._safe_send(
                    chat_id,
                    (
                        f"Немає операцій для аналізу за період: {date_range.label}.\n"
                        f"Усі знайдені операції виключені з балансу: {excluded_count}"
                    ),
                )
            else:
                self._safe_send(chat_id, f"Немає операцій для аналізу за період: {date_range.label}.")
            return

        base_text = build_summary_text(active_transactions, date_range.label)
        text = self._compose_analysis_text(
            profile,
            active_transactions,
            date_range.label,
            base_text,
        )
        self._send_long_message(chat_id, text)

    def _compose_analysis_text(
        self,
        profile: UserProfile,
        transactions: list[dict[str, Any]],
        label: str,
        base_text: str,
    ) -> str:
        gemini_text = self._generate_gemini_analysis(profile, transactions, label)
        if not gemini_text:
            return base_text
        return f"{base_text}\n\nGemini:\n{gemini_text}"

    def _generate_gemini_analysis(
        self,
        profile: UserProfile,
        transactions: list[dict[str, Any]],
        label: str,
    ) -> str | None:
        if not transactions or not self.settings.gemini_api_key:
            return None

        advisor = GeminiAdvisor(
            api_key=self.settings.gemini_api_key,
            model_name=self.settings.gemini_model,
        )
        try:
            return advisor.analyze_period(
                transactions=transactions,
                timezone_name=profile.timezone,
                label=label,
            )
        except GeminiAnalysisError as exc:
            print("Gemini analysis error:", exc)
            return f"Gemini тимчасово недоступний: {exc}"

    def _remember_operations_view(
        self,
        profile: UserProfile,
        state: dict[str, Any],
        transactions: list[dict[str, Any]],
        label: str,
    ) -> None:
        visible_transactions = list(reversed(transactions))
        state["last_operations_label"] = label
        state["last_operations_lookup"] = [str(item["id"]) for item in visible_transactions]
        state["last_operations_updated_at"] = utc_now_iso()
        self._save_state(profile, state)

    def _resolve_transaction_reference(
        self,
        state: dict[str, Any],
        reference: str,
    ) -> tuple[str, int | None]:
        normalized = reference.strip().lstrip("#№")
        if normalized.isdigit():
            lookup = state.get("last_operations_lookup", [])
            number = int(normalized)
            if 1 <= number <= len(lookup):
                return str(lookup[number - 1]), number
        return reference.strip(), None

    def _transactions_for_local_day(
        self,
        transactions: list[dict[str, Any]],
        profile: UserProfile,
        day: date,
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in transactions
            if self._transaction_local_datetime(item, profile).date() == day
        ]

    def _transactions_for_local_month(
        self,
        transactions: list[dict[str, Any]],
        profile: UserProfile,
        month_start: date,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in transactions:
            local_dt = self._transaction_local_datetime(item, profile)
            if local_dt.year == month_start.year and local_dt.month == month_start.month:
                result.append(item)
        return result

    def _send_help(self, chat_id: int) -> None:
        profile = self.registry.get(chat_id)
        if profile is None:
            text = (
                "Надішліть Monobank token одним повідомленням або використайте `/connect <token>`.\n"
                "Після підключення бот буде:\n"
                "- автоматично відстежувати ваші транзакції;\n"
                "- зберігати ваш JSON окремо від інших користувачів;\n"
                "- будувати аналіз по запиту через /analysis.\n\n"
                "Команди:\n"
                "/connect <token>\n"
                "/status\n"
                "/report [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/analysis [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/operations [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]\n"
                "/exclude <номер зі списку> [примітка]\n"
                "/include <номер зі списку>\n"
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
                "/exclude <номер зі списку> [примітка]\n"
                "/include <номер зі списку>\n"
                "/disconnect\n\n"
                "Щоб транзакція лишилася в історії, але не впливала на баланс, використовуйте /exclude."
            )
        text += (
            "\n\nРучне додавання транзакцій:\n"
            "`+ 1200 зарплата аванс`\n"
            "`- 250 їжа кава`"
        )
        text += (
            "\n\nСповіщення:\n"
            "/notifications instant\n"
            "/notifications daily\n"
            "/notifications off\n"
            "\n/analysis без аргументів: день + поточний місяць + порівняння місяців"
        )
        if self.settings.privacy_strict_mode:
            text += "\n\nPrivacy mode: токени та історія не зберігаються на диску. Після рестарту бота потрібно підключитися знову."
        self._safe_send(chat_id, text)

    def _handle_notifications(
        self,
        chat_id: int,
        profile: UserProfile,
        args: list[str],
    ) -> None:
        state = self._load_state(profile)
        if not args:
            mode = self._notification_mode(state)
            self.telegram.send_message(
                chat_id,
                (
                    f"Поточний режим сповіщень: {mode}\n"
                    "Оберіть кнопкою, коли бот має надсилати повідомлення."
                ),
                reply_markup=self._notifications_menu_markup(),
            )
            return

        raw_mode = args[0].strip().lower()
        aliases = {
            "instant": "instant",
            "live": "instant",
            "on": "instant",
            "daily": "daily",
            "digest": "daily",
            "off": "off",
            "mute": "off",
        }
        mode = aliases.get(raw_mode)
        if mode is None:
            self._safe_send(
                chat_id,
                "Використання: /notifications instant | daily | off",
            )
            return

        state["notification_mode"] = mode
        self._save_state(profile, state)
        descriptions = {
            "instant": "миттєві повідомлення по кожній транзакції",
            "daily": "лише один автозвіт наприкінці дня",
            "off": "автосповіщення вимкнені",
        }
        self._safe_send(chat_id, f"Режим сповіщень: {mode} ({descriptions[mode]}).")

    def _handle_transaction_exclusion(
        self,
        chat_id: int,
        profile: UserProfile,
        args: list[str],
        excluded: bool,
    ) -> None:
        if not args:
            command = "/exclude <номер зі списку> [примітка]" if excluded else "/include <номер зі списку>"
            self._safe_send(chat_id, f"Використання: {command}")
            return

        state = self._load_state(profile)
        transaction_id, visible_number = self._resolve_transaction_reference(state, args[0])
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
        reference_text = f"Операцію №{visible_number}" if visible_number is not None else "Операцію"
        lines = [
            f"{reference_text} {action_text}.",
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
        description: str,
    ) -> None:
        transaction = self._storage_for(profile.chat_id).append_manual_transaction(
            amount_minor=amount_minor,
            category=category,
            description=description,
            account_id=profile.priority_account_id,
        )
        sign = "+" if amount_minor >= 0 else "-"
        text = (
            f"{sign}{abs(amount_minor) / 100:.2f} {transaction['currency']} | "
            f"{transaction['category']} | {transaction['description']}"
        )
        self._safe_send(chat_id, text)

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
        self._maybe_send_scheduled_reports(profile, state)
        self._save_state(profile, state)

    def _notify_transaction(self, profile: UserProfile, transaction: dict[str, Any]) -> None:
        state = self._load_state(profile)
        if self._notification_mode(state) != "instant":
            return

        amount_minor = int(transaction["amount_minor"])
        sign = "+" if amount_minor >= 0 else "-"
        text = f"{sign}{abs(amount_minor) / 100:.2f} {transaction['currency']} | {transaction['category']} | {transaction.get('description') or transaction.get('comment') or transaction.get('counterName') or 'Без назви'}"
        self._safe_send(profile.chat_id, text)

    @staticmethod
    def _notification_mode(state: dict[str, Any]) -> str:
        return str(state.get("notification_mode") or "instant")

    def _local_today(self, profile: UserProfile) -> date:
        try:
            zone = ZoneInfo(profile.timezone)
        except ZoneInfoNotFoundError:
            zone = datetime.now().astimezone().tzinfo or UTC
        return datetime.now(zone).date()

    def _transaction_local_datetime(
        self,
        transaction: dict[str, Any],
        profile: UserProfile,
    ) -> datetime:
        try:
            zone = ZoneInfo(profile.timezone)
        except ZoneInfoNotFoundError:
            zone = datetime.now().astimezone().tzinfo or UTC

        dt = datetime.fromisoformat(str(transaction["datetime"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(zone)

    def _cached_daily_digest(
        self,
        profile: UserProfile,
        state: dict[str, Any],
    ) -> str:
        today = self._local_today(profile).isoformat()
        cached = state.get("cached_daily_digest_text")
        if state.get("cached_daily_digest_date") == today and cached:
            return str(cached)

        snapshot = self._storage_for(profile.chat_id).load()
        active_transactions = self._balance_transactions(snapshot.get("transactions", []))
        text = build_daily_digest_text(
            active_transactions,
            profile.timezone,
            self._local_today(profile),
        )
        state["cached_daily_digest_date"] = today
        state["cached_daily_digest_text"] = text
        self._save_state(profile, state)
        return text

    def _cached_monthly_report(
        self,
        profile: UserProfile,
        state: dict[str, Any],
    ) -> str:
        today = self._local_today(profile).isoformat()
        cached = state.get("cached_monthly_report_text")
        if state.get("cached_monthly_report_date") == today and cached:
            return str(cached)

        snapshot = self._storage_for(profile.chat_id).load()
        active_transactions = self._balance_transactions(snapshot.get("transactions", []))
        text = build_month_comparison_text(
            active_transactions,
            profile.timezone,
            self._local_today(profile),
        )
        state["cached_monthly_report_date"] = today
        state["cached_monthly_report_text"] = text
        self._save_state(profile, state)
        return text

    def _maybe_send_scheduled_reports(
        self,
        profile: UserProfile,
        state: dict[str, Any],
    ) -> None:
        if self._notification_mode(state) != "daily":
            return

        today = self._local_today(profile)
        if state.get("last_auto_report_date") == today.isoformat():
            return

        try:
            zone = ZoneInfo(profile.timezone)
        except ZoneInfoNotFoundError:
            zone = datetime.now().astimezone().tzinfo or UTC
        now_local = datetime.now(zone)
        if now_local.hour < self.settings.daily_analysis_hour:
            return

        digest = self._cached_daily_digest(profile, state)
        self._send_long_message(profile.chat_id, digest)
        self._send_month_comparison_chart(profile.chat_id, profile)
        state["last_auto_report_date"] = today.isoformat()
        self._save_state(profile, state)

    @staticmethod
    def _balance_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item
            for item in transactions
            if not item.get("excluded_from_balance")
        ]

    def _storage_for(self, chat_id: int) -> JsonStorage:
        storage = self._storages.get(chat_id)
        if storage is not None:
            return storage

        storage = JsonStorage(
            self.registry.data_file(chat_id),
            self.registry.secret_box,
            persist_to_disk=not self.settings.privacy_strict_mode,
        )
        self._storages[chat_id] = storage
        return storage

    def _load_state(self, profile: UserProfile) -> dict[str, Any]:
        if self.settings.privacy_strict_mode:
            return dict(self._state_cache.get(profile.chat_id, self._default_state()))

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
        if self.settings.privacy_strict_mode:
            self._state_cache[profile.chat_id] = dict(payload)
            return

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
            "notification_mode": "instant",
            "last_operations_label": None,
            "last_operations_lookup": [],
            "last_operations_updated_at": None,
            "cached_daily_digest_date": None,
            "cached_daily_digest_text": None,
            "cached_monthly_report_date": None,
            "cached_monthly_report_text": None,
            "last_auto_report_date": None,
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

    def _send_month_comparison_chart(self, chat_id: int, profile: UserProfile) -> None:
        snapshot = self._storage_for(profile.chat_id).load()
        active_transactions = self._balance_transactions(snapshot.get("transactions", []))
        if not active_transactions:
            return

        image_bytes = build_month_comparison_chart(
            active_transactions,
            profile.timezone,
            self._local_today(profile),
        )
        self.telegram.send_photo(
            chat_id,
            image_bytes,
            filename="month-comparison.png",
            caption="Графік доходів і витрат по місяцях",
        )

    def _menu_markup(self, chat_id: int) -> dict[str, Any]:
        if self.registry.get(chat_id) is None:
            keyboard = [
                ["Підключити", "Почати"],
                ["Статус"],
            ]
            placeholder = "Оберіть команду або надішліть token"
        else:
            context = self._menu_contexts.get(chat_id, "main")
            if context == "report":
                return self._report_menu_markup()
            if context == "analysis":
                return self._analysis_menu_markup()
            if context == "operations":
                return self._operations_menu_markup()
            if context == "notifications":
                return self._notifications_menu_markup()
            keyboard = [
                ["Статус", "Аналіз"],
                ["Звіти", "Операції"],
                ["Сповіщення", "Підключити"],
                ["Відключити"],
            ]
            placeholder = "Оберіть команду або введіть транзакцію"
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": placeholder,
        }

    def _set_menu_context(self, chat_id: int, context: str) -> None:
        self._menu_contexts[chat_id] = context

    @staticmethod
    def _notifications_menu_markup() -> dict[str, Any]:
        return {
            "keyboard": [
                ["Миттєво", "Щоденний звіт"],
                ["Вимкнути повідомлення"],
                ["Назад"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Оберіть режим сповіщень",
        }

    @staticmethod
    def _report_menu_markup() -> dict[str, Any]:
        return {
            "keyboard": [
                ["Звіт за сьогодні", "Звіт за тиждень"],
                ["Звіт за місяць", "Звіт за весь час"],
                ["Назад"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Оберіть період звіту",
        }

    @staticmethod
    def _analysis_menu_markup() -> dict[str, Any]:
        return {
            "keyboard": [
                ["Аналіз за сьогодні", "Аналіз за тиждень"],
                ["Аналіз за місяць", "Аналіз за весь час"],
                ["Назад"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Оберіть період аналізу",
        }

    @staticmethod
    def _operations_menu_markup() -> dict[str, Any]:
        return {
            "keyboard": [
                ["Операції за сьогодні", "Операції за тиждень"],
                ["Операції за місяць", "Усі операції"],
                ["Назад"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Оберіть період операцій",
        }

    def _send_long_message(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            self._safe_send(chat_id, chunk)

    def _safe_send(self, chat_id: int, text: str) -> None:
        self.telegram.send_message(
            chat_id,
            text[:4096],
            reply_markup=self._menu_markup(chat_id),
        )

    @staticmethod
    def _sleep_seconds(seconds: int) -> None:
        threading.Event().wait(seconds)

    @staticmethod
    def _is_polling_conflict(exc: TelegramAPIError) -> bool:
        message = str(exc)
        return "Telegram API error 409" in message or '"error_code":409' in message

    @staticmethod
    def _parse_command(text: str) -> tuple[str, list[str]]:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lstrip("/").lower()
        return command, parts[1:]

    @staticmethod
    def _parse_manual_transaction_text(text: str) -> tuple[int, str, str] | None:
        normalized = text.strip()
        if not normalized or normalized[0] not in "+-":
            return None

        payload = normalized[1:].strip()
        if not payload:
            raise ValueError("Після + або - вкажіть суму. Наприклад: - 250 їжа кава.")

        parts = payload.split(maxsplit=1)
        amount_text = parts[0].replace(",", ".")
        try:
            amount = Decimal(amount_text)
        except InvalidOperation as exc:
            raise ValueError(
                "Не вдалося розпізнати суму. Приклад: + 1200 зарплата аванс або - 250 їжа кава."
            ) from exc

        if amount <= 0:
            raise ValueError("Сума має бути більшою за нуль.")

        amount_minor = int(
            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * Decimal("100")
        )
        signed_amount = amount_minor if normalized[0] == "+" else -amount_minor
        default_category = "Ручне надходження" if signed_amount > 0 else "Ручні витрати"
        if len(parts) == 1 or not parts[1].strip():
            return signed_amount, default_category, "Без назви"

        category_parts = parts[1].strip().split(maxsplit=1)
        category = category_parts[0].strip() or default_category
        description = category_parts[1].strip() if len(category_parts) > 1 else category
        return signed_amount, category, description

    @staticmethod
    def _looks_like_monobank_token(text: str) -> bool:
        normalized = text.strip()
        if not normalized or " " in normalized:
            return False
        if normalized.startswith(("+", "-", "/")):
            return False
        return len(normalized) >= 20


def main() -> None:
    settings = Settings.from_env()
    bot = MonobankTelegramBot(
        settings=settings,
        telegram=TelegramBotAPI(settings.telegram_bot_token),
        registry=UserRegistry(
            settings.registry_file,
            settings.users_dir,
            SecretBox(
                settings.secrets_key_file,
                persist_to_disk=not settings.privacy_strict_mode,
            ),
            persist_to_disk=not settings.privacy_strict_mode,
        ),
    )
    bot.run()


if __name__ == "__main__":
    main()
