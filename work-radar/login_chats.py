#!/usr/bin/env python3
"""Разовый вход в Telegram для чтения чатов.

Запускается вручную и только владельцем аккаунта: скрипт спросит телефон и код
из Telegram. Ни код, ни пароль никуда не отправляются и нигде не сохраняются —
на выходе печатается строка сессии, которую нужно положить в секрет репозитория.

Как пользоваться:

    pip install telethon
    python3 login_chats.py

Перед запуском получите api_id и api_hash на https://my.telegram.org
(раздел «API development tools») и задайте их в окружении:

    export TELEGRAM_API_ID=1234567
    export TELEGRAM_API_HASH=abcdef...
"""

import os
import sys


def main() -> int:
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        print("Сначала установите Telethon:  pip install telethon", file=sys.stderr)
        return 1

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print(
            "Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.\n"
            "Получите их на https://my.telegram.org → API development tools,\n"
            "затем выполните:\n"
            "    export TELEGRAM_API_ID=...\n"
            "    export TELEGRAM_API_HASH=...",
            file=sys.stderr,
        )
        return 2

    print("Сейчас Telegram спросит номер телефона и пришлёт код в приложение.\n")
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session = client.session.save()
        me = client.get_me()
        print(f"\nВход выполнен: {me.first_name} (@{me.username})")
        print("\nСтрока сессии — положите её в секрет TELEGRAM_SESSION:\n")
        print(session)
        print(
            "\nЭто ключ от аккаунта: не публикуйте его и не пересылайте.\n"
            "Добавить в репозиторий:\n"
            "    gh secret set TELEGRAM_SESSION --repo <владелец>/<репозиторий>"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
