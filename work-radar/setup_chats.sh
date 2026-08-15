#!/usr/bin/env bash
# Настройка чтения фриланс-чатов одной командой.
# Создаёт изолированное окружение, ставит Telethon и запускает вход.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Создаю изолированное окружение…"
  python3 -m venv .venv
fi

echo "Проверяю зависимости…"
.venv/bin/python -m pip install --quiet --disable-pip-version-check telethon

exec .venv/bin/python login_chats.py
