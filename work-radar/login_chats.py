#!/usr/bin/env python3
"""Разовая настройка чтения фриланс-чатов.

Запускает владелец аккаунта и только он: Telegram спросит номер телефона и код
из приложения. Код никуда не передаётся и не сохраняется — он нужен только
самому Telegram.

Скрипт делает всё остальное сам:
  1. спрашивает api_id и api_hash (или берёт их из окружения);
  2. выполняет вход и получает строку сессии;
  3. проверяет, читаются ли публичные чаты без вступления в них;
  4. записывает секреты в репозиторий через gh, чтобы строку сессии
     не пришлось никуда копировать вручную.

Запуск:

    ./setup_chats.sh
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "alenkakochetkova00-droid/alena-radars-free-runner"
PROBE_CHATS = ["frilancru", "jobospherechat", "proffreelancee_chat"]


def ask(prompt: str, env_name: str) -> str:
    import os

    value = os.getenv(env_name)
    if value:
        print(f"{env_name} взят из окружения.")
        return value.strip()
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nВвод прерван. Запустите ./setup_chats.sh в терминале.", file=sys.stderr)
        raise SystemExit(130)


def set_secret(name: str, value: str) -> bool:
    """Кладёт секрет в репозиторий, не печатая его значение."""
    try:
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", REPO],
            input=value.encode(),
            check=True,
            capture_output=True,
        )
        print(f"  ✅ секрет {name} записан")
        return True
    except FileNotFoundError:
        print("  ⚠️  не найден gh — секреты придётся добавить вручную")
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠️  не удалось записать {name}: {exc.stderr.decode()[:120]}")
    return False


def main() -> int:
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError:
        print("Telethon не установлен. Запустите ./setup_chats.sh", file=sys.stderr)
        return 1

    print("Настройка чтения фриланс-чатов\n" + "=" * 46)
    print(
        "Нужны api_id и api_hash с https://my.telegram.org\n"
        "→ войдите → API development tools → создайте приложение\n"
    )
    api_id = ask("api_id: ", "TELEGRAM_API_ID")
    api_hash = ask("api_hash: ", "TELEGRAM_API_HASH")
    if not api_id.isdigit() or len(api_hash) < 16:
        print("api_id должен быть числом, api_hash — длинной строкой.", file=sys.stderr)
        return 2

    print("\nСейчас Telegram спросит номер телефона и пришлёт код в приложение.")
    print("Код нужен только Telegram: он нигде не сохраняется.\n")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = client.get_me()
        session = client.session.save()
        print(f"\n✅ Вход выполнен: {me.first_name} (@{me.username})\n")

        print("Проверяю, читаются ли чаты без вступления в них…")
        readable, blocked = [], []
        for chat in PROBE_CHATS:
            try:
                messages = client.get_messages(chat, limit=1)
                if messages:
                    readable.append(chat)
                    print(f"  ✅ @{chat}: читается, последнее сообщение получено")
                else:
                    blocked.append(chat)
                    print(f"  ⚠️  @{chat}: пусто")
            except Exception as exc:
                blocked.append(chat)
                print(f"  ❌ @{chat}: {type(exc).__name__} — {str(exc)[:70]}")

        print("\nЗаписываю секреты в репозиторий…")
        ok = all([
            set_secret("TELEGRAM_SESSION", session),
            set_secret("TELEGRAM_API_ID", api_id),
            set_secret("TELEGRAM_API_HASH", api_hash),
        ])

    print("\n" + "=" * 46)
    if readable:
        print(f"Чаты читаются без вступления: {len(readable)} из {len(PROBE_CHATS)}.")
        print("Вступать в группы не нужно.")
    else:
        print("Ни один чат не прочитался без вступления.")
        print("Вступите в несколько групп из config.json и запустите снова.")
    if ok:
        print("Секреты на месте — радар подхватит чаты при следующем запуске.")
    else:
        print("Секреты записать не удалось, добавьте их вручную в настройках репозитория.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
