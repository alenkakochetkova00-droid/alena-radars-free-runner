"""Точка входа: поднимает сканер и бота, живёт в фоне."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
from datetime import datetime

from .bot import Bot
from .config import LOG_DIR, get_config
from .notifier import Telegram
from .scanner import Scanner
from .storage import Storage

log = logging.getLogger("radar")


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "dealradar.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def scan_loop(scanner: Scanner, storage: Storage, config) -> None:
    # Первый скан — почти сразу после старта, дальше по интервалу
    await asyncio.sleep(10)
    while True:
        interval = int(config.get("general.scan_interval_minutes", 45))
        if storage.setting("paused", False):
            log.info("Радар на паузе, скан пропущен")
        else:
            try:
                flights, trains = await scanner.scan_all()
                log.info("Скан завершён: авиа %s, поезда %s", flights, trains)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Скан упал целиком")
        await asyncio.sleep(max(interval, 5) * 60)


async def amain(args) -> int:
    config = get_config()
    storage = Storage()
    config.apply_overrides(storage.setting("overrides", {}) or {})

    problems = []
    if not config.bot_token:
        problems.append("нет TELEGRAM_BOT_TOKEN")
    if not config.chat_id:
        problems.append("нет TELEGRAM_CHAT_ID")
    if not config.tp_token:
        problems.append("нет TRAVELPAYOUTS_TOKEN — авиабилеты искать нечем")
    if problems:
        log.error("Не хватает настроек: %s. Запусти: python3 setup.py", "; ".join(problems))
        if not config.bot_token or not config.chat_id:
            return 2

    async with Telegram(config.bot_token, config.chat_id,
                        config.get("telegram.parse_mode", "HTML")) as tg:
        scanner = Scanner(config, storage, tg)

        if args.commands_only:
            bot = Bot(config, storage, tg, scanner)
            handled = await bot.poll_once(timeout=0)
            log.info("Быстрая проверка Telegram: команд %s", handled)
            return 0

        if args.scheduled:
            bot = Bot(config, storage, tg, scanner)
            handled = await bot.poll_once(timeout=0)
            if storage.setting("paused", False):
                log.info("Плановый облачный скан пропущен: радар на паузе")
                return 0
            flights, trains = await scanner.scan_all()
            log.info(
                "Облачный цикл: команд %s, авиа %s, поезда %s",
                handled,
                flights,
                trains,
            )
            return 0

        if args.once:
            flights, trains = await scanner.scan_all()
            log.info("Разовый скан: авиа %s, поезда %s", flights, trains)
            return 0

        if args.test:
            ok = await tg.send(
                f"🛰 <b>Deal Radar на связи</b>\nВремя: {datetime.now():%d.%m.%Y %H:%M}")
            log.info("Тестовое сообщение отправлено: %s", ok)
            return 0 if ok else 1

        bot = Bot(config, storage, tg, scanner)
        await tg.send(
            "🛰 <b>Deal Radar запущен</b>\n"
            f"Работаю сам в фоне: проверяю цены каждые "
            f"{config.get('general.scan_interval_minutes', 45)} минут, запускать ничего "
            "не нужно. Напишу, только когда цена будет ниже обычной минимум на "
            f"{int(float(config.get('flights.min_discount', 0.7)) * 100)} %.\n\n"
            "/help — что я умею · /find — посмотреть прямо сейчас",
            silent=True, disable_preview=True)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover
                pass

        tasks = [
            asyncio.create_task(scan_loop(scanner, storage, config), name="scan"),
            asyncio.create_task(bot.run(), name="bot"),
            asyncio.create_task(stop.wait(), name="stop"),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.get_name() != "stop" and task.exception():
                log.error("Задача %s завершилась ошибкой: %s", task.get_name(), task.exception())
        log.info("Остановлен")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deal Radar — охота за дешёвыми билетами")
    parser.add_argument("--once", action="store_true", help="один скан и выход")
    parser.add_argument("--scheduled", action="store_true", help="команды и один плановый скан")
    parser.add_argument("--commands-only", action="store_true", help="только быстрые команды Telegram")
    parser.add_argument("--test", action="store_true", help="проверить связь с Telegram")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
