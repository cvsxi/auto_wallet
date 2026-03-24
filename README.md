# Monobank Multi-User Telegram Bot

Бот працює так:

- кожен користувач підключає свій власний `Monobank token`;
- дані кожного користувача зберігаються окремо в `data/users/<chat_id>/`;
- нові транзакції підтягуються автоматично;
- AI-аналіз і поради запускаються тільки вручну командою `/analysis`;
- якщо задано `GEMINI_API_KEY`, аналіз генерується через Gemini на основі агрегованих даних;
- токени Monobank зберігаються в реєстрі користувачів у зашифрованому вигляді;
- runtime JSON-файли зберігаються у стисненому зашифрованому форматі;
- дані одного користувача не використовуються для інших.

## Підготовка

1. Заповніть `.env` на основі `.env.example`.
2. У `.env` обов'язковий `TELEGRAM_BOT_TOKEN`.
3. Опційно додайте `GEMINI_API_KEY`, якщо хочете AI-аналіз через Gemini.
4. За потреби задайте `GEMINI_MODELS` і `GEMINI_SWITCH_AFTER_REQUESTS`.
5. Запустіть:

```powershell
python bot.py
```

## Як користуватися

1. Користувач відкриває бота в приватному чаті.
2. Надсилає свій `Monobank token` одним повідомленням або через `/connect <token>`.
3. Після цього бот починає окремо вести його JSON і моніторинг.
4. Для AI-порад використовується `/analysis`.

## Команди

- `/start`
- `/connect <token>`
- `/status`
- `/report [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]`
- `/analysis [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]`
- `/operations [today|week|month|all|YYYY-MM-DD YYYY-MM-DD]`
- `/exclude <transaction_id> [примітка]`
- `/include <transaction_id>`
- `/disconnect`

`/exclude` робить "м'яке видалення" транзакції: вона залишається в історії, але більше не впливає на баланс, звіти та AI-аналіз. `transaction_id` можна взяти з команди `/operations`.

## Дані користувачів

- профілі користувачів: `data/user_profiles.json`
- ключ шифрування локального сховища: `data/.secret.key`
- транзакції користувача: `data/users/<chat_id>/transactions.json`
- стан моніторингу користувача: `data/users/<chat_id>/state.json`

## Важливо

- для конфіденційності використовуйте бота тільки в приватному чаті;
- Monobank має rate limit, тому повідомлення про операції можуть приходити з невеликою затримкою;
- AI-аналіз більше не надсилається автоматично о 23:00, щоб не витрачати ліміт Gemini;
- для абсолютно миттєвих подій потрібен webhook Monobank на публічному HTTPS сервері.
