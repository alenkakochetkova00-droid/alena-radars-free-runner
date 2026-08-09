"""Диагностика: что из источников данных реально доступно с этого ноутбука.

Запуск:  ./dealradar doctor
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from .config import get_config
from .flights import TravelpayoutsClient
from .notifier import Telegram
from .rails import RzdClient
from .storage import Storage


def line(ok: bool | None, name: str, detail: str = "") -> None:
    mark = {True: "\033[32m✓\033[0m", False: "\033[31m✗\033[0m", None: "\033[33m—\033[0m"}[ok]
    print(f"  {mark} {name}" + (f": {detail}" if detail else ""))


async def run() -> int:
    cfg = get_config()
    db = Storage()
    problems = 0

    print("\n=== Проверка Deal Radar ===\n")
    print("Настройки")
    line(bool(cfg.bot_token), "токен Telegram-бота",
         "задан" if cfg.bot_token else "нет — ./dealradar setup")
    line(bool(cfg.chat_id), "chat_id", cfg.chat_id or "нет — ./dealradar setup")
    line(bool(cfg.tp_token), "токен Travelpayouts",
         "задан" if cfg.tp_token else "нет — авиабилеты искать нечем")
    problems += 0 if (cfg.bot_token and cfg.chat_id) else 1

    print("\nИсточники данных")
    if cfg.bot_token:
        async with Telegram(cfg.bot_token, cfg.chat_id) as tg:
            me = await tg.me()
            line(bool(me), "Telegram API",
                 f"бот @{me.get('username')}" if me else "нет связи")
            if not me:
                problems += 1
    else:
        line(None, "Telegram API", "пропущено, нет токена")

    if cfg.tp_token:
        async with TravelpayoutsClient(cfg.tp_token, cfg.tp_marker) as api:
            ok, detail = await api.ping()
            line(ok, "Travelpayouts (авиабилеты)", detail)
            problems += 0 if ok else 1
    else:
        line(None, "Travelpayouts (авиабилеты)", "пропущено, нет токена")

    async with RzdClient(db) as rzd:
        ok, detail = await rzd.ping()
        line(ok, "pass.rzd.ru (поезда)", detail)
        if ok:
            offers = await rzd.search("Москва", "Санкт-Петербург",
                                      date.today() + timedelta(days=14))
            line(bool(offers), "пробный поиск поездов",
                 f"нашлось вариантов: {len(offers)}" if offers
                 else "поезда не вернулись — возможно, нет мест на эту дату")
        else:
            print("      Если ты в России и сайт rzd.ru в браузере открывается — "
                  "проверь VPN/прокси: РЖД часто блокирует зарубежные адреса.")
            print("      Авиабилеты при этом продолжат работать.")

    print("\nБаза данных")
    line(True, "наблюдений цен", f"{db.observation_count():,}".replace(",", " "))
    line(True, "отправлено находок", str(len(db.recent_deals(1000))))
    scans = db.last_scans(1)
    if scans:
        s = scans[0]
        line(not s["error"], "последняя проверка",
             f"{s['finished_at']} {s['kind']}: проверено {s['checked']}, найдено {s['found']}"
             + (f" — {s['error']}" if s["error"] else ""))
    else:
        line(None, "последняя проверка", "ещё не было")

    print()
    if problems:
        print(f"Есть {problems} проблем(ы) — см. отметки выше.\n")
        return 1
    print("Всё, что нужно для работы, на месте.\n")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
