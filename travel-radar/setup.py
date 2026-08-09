#!/usr/bin/env python3
"""Мастер настройки Deal Radar: токены, chat_id, проверка связи.

Запуск:  python3 setup.py
Или без вопросов:  python3 setup.py --bot-token XXX --tp-token YYY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.config import load_env, save_env  # noqa: E402

TG_API = "https://api.telegram.org"
TP_API = "https://api.travelpayouts.com"


def get_json(url: str, timeout: int = 20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def check_bot(token: str):
    data = get_json(f"{TG_API}/bot{token}/getMe")
    if data.get("ok"):
        return True, data["result"].get("username", "?")
    return False, data.get("description") or data.get("error") or "токен не принят"


def detect_chat_id(token: str):
    data = get_json(f"{TG_API}/bot{token}/getUpdates")
    if not data.get("ok"):
        return None
    for update in reversed(data.get("result", [])):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("first_name") or chat.get("username") or ""
            return str(chat["id"]), name
    return None


def send_message(token: str, chat_id: str, text: str) -> bool:
    q = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    data = get_json(f"{TG_API}/bot{token}/sendMessage?{q}")
    return bool(data.get("ok"))


def check_travelpayouts(token: str):
    data = get_json(f"{TP_API}/v1/city-directions?origin=MOW&currency=rub&token={token}")
    if isinstance(data, dict) and data.get("success") and data.get("data"):
        return True, f"работает, направлений из Москвы: {len(data['data'])}"
    if isinstance(data, dict) and data.get("success") is False:
        return False, str(data.get("error") or "токен отклонён")
    return False, "нет валидного ответа (проверь токен)"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--tp-token", default="")
    parser.add_argument("--marker", default="")
    parser.add_argument("--no-input", action="store_true")
    args = parser.parse_args()

    env = load_env()
    print("\n=== Настройка Deal Radar ===\n")

    # --- Telegram -------------------------------------------------------
    bot_token = args.bot_token or env.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token and not args.no_input:
        print("1) Telegram-бот")
        print("   Открой @BotFather → /newbot → придумай имя → скопируй токен.\n")
        bot_token = ask("   Токен бота")
    if not bot_token:
        print("   ✗ Без токена бота дальше нельзя.")
        return 1

    ok, info = check_bot(bot_token)
    print(f"   {'✓' if ok else '✗'} Бот: {info}")
    if not ok:
        return 1

    # --- chat_id --------------------------------------------------------
    chat_id = args.chat_id or env.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        print(f"\n2) Напиши боту @{info} что угодно (например /start) и нажми Enter здесь.")
        if not args.no_input:
            try:
                input("   Нажми Enter, когда написала: ")
            except EOFError:
                pass
        found = detect_chat_id(bot_token)
        if found:
            chat_id, name = found
            print(f"   ✓ Нашла чат: {chat_id} ({name})")
        else:
            print("   ✗ Не вижу сообщений боту.")
            if not args.no_input:
                chat_id = ask("   Введи chat_id вручную (узнать: @userinfobot)")
    if not chat_id:
        return 1

    # --- Travelpayouts --------------------------------------------------
    tp_token = args.tp_token or env.get("TRAVELPAYOUTS_TOKEN", "")
    if not tp_token and not args.no_input:
        print("\n3) Данные по авиабилетам — Travelpayouts (бесплатно)")
        print("   travelpayouts.com → регистрация → Инструменты → API → токен.")
        print("   Можно пропустить: поезда будут работать, авиабилеты — нет.\n")
        tp_token = ask("   Токен Travelpayouts (Enter — пропустить)")
    if tp_token:
        ok_tp, info_tp = check_travelpayouts(tp_token)
        print(f"   {'✓' if ok_tp else '✗'} Travelpayouts: {info_tp}")
    else:
        print("   ⚠️ Без токена Travelpayouts авиабилеты искать нечем.")

    marker = args.marker or env.get("TRAVELPAYOUTS_MARKER", "")

    save_env({
        "TELEGRAM_BOT_TOKEN": bot_token,
        "TELEGRAM_CHAT_ID": str(chat_id),
        "TRAVELPAYOUTS_TOKEN": tp_token,
        "TRAVELPAYOUTS_MARKER": marker,
    })
    print("\n   ✓ Секреты сохранены в .env (доступ только тебе)")

    if send_message(bot_token, chat_id,
                    "🛰 <b>Deal Radar настроен</b>\nЯ на связи. Напиши /help, чтобы "
                    "посмотреть, что я умею."):
        print("   ✓ Тестовое сообщение отправлено в Telegram")
    else:
        print("   ✗ Не удалось отправить сообщение — проверь chat_id")
        return 1

    print("\nГотово. Запуск:  ./dealradar start\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
