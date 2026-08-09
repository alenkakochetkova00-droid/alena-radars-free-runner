"""Telegram-бот: управление радаром прямо из чата."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from .planner import render_card, render_plan
from .util import esc, fmt_money

log = logging.getLogger("radar.bot")

COMMANDS = [
    ("status", "Что сейчас происходит"),
    ("find", "Показать лучшее прямо сейчас"),
    ("details", "Полный маршрут по последней находке"),
    ("scan", "Запустить проверку немедленно"),
    ("last", "Последние находки"),
    ("threshold", "Порог скидки, напр. /threshold 50"),
    ("cap", "Потолок бюджета поездки, напр. /cap vacation 100000"),
    ("maxprice", "Потолок цены билета, напр. /maxprice 40000"),
    ("budget", "Бюджет на ночь, напр. /budget 6000"),
    ("shapes", "Форматы поездок: /shapes weekend off"),
    ("visa", "Виза по стране: /visa TH"),
    ("addtrain", "Добавить ж/д маршрут: /addtrain Москва - Тюмень"),
    ("reset", "Сбросить дневной лимит уведомлений"),
    ("pause", "Поставить радар на паузу"),
    ("resume", "Снять с паузы"),
    ("help", "Справка"),
]

HELP = """<b>Deal Radar</b> — ищу аномально дешёвые билеты и присылаю готовый маршрут.

<b>Что я делаю</b>
Сама, в фоне, каждые {interval} минут проверяю авиабилеты из Москвы по всему миру и
поезда по России. Запускать вручную ничего не нужно — я работаю, пока включён ноутбук.
Пишу, только когда цена ниже обычной минимум на {threshold} % и даты складываются
в нормальную поездку.

<b>Форматы поездок</b>
• Длинные выходные — вылет в чт вечером или пт утром, назад в вс. Один день отпуска.
• Стандартный отпуск — 5–9 ночей.
• Зимовка — от 25 ночей, только туда, где реально легально жить долго.
• Мультимаршрут — две-три страны за поездку.

Уведомление приходит короткой карточкой — цена, даты, жильё, виза, итог.
Весь маршрут с планом по дням — команда /details.

<b>Команды</b>
/status — статус сканов и статистика
/find — лучшие варианты прямо сейчас
/details — полный маршрут по последней находке
/scan — запустить проверку немедленно
/last — что присылала в последнее время
/cap — потолок бюджета всей поездки по каждому формату
/threshold 50 — слать только при скидке от 50 %
/maxprice 40000 — не присылать билеты дороже этой суммы
/budget 6000 — потолок за ночь жилья
/shapes weekend off — выключить формат поездки
/visa TH — визовая справка по стране
/addtrain Москва - Тюмень — добавить ж/д маршрут
/reset — сбросить дневной лимит уведомлений
/pause и /resume — пауза и возобновление"""


class Bot:
    def __init__(self, config, storage, telegram, scanner) -> None:
        self.cfg = config
        self.db = storage
        self.tg = telegram
        self.scanner = scanner
        saved_offset = self.db.setting("telegram_update_offset", None)
        self._offset: int | None = int(saved_offset) if saved_offset is not None else None
        self._busy = False
        # Результаты последнего /find — чтобы /details показал именно их,
        # если они свежее последнего автоматического уведомления.
        self._last_plans: list = []
        self._last_plans_at: datetime | None = None

    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.tg.set_commands(COMMANDS)
        log.info("Бот слушает команды")
        while True:
            try:
                await self.poll_once(timeout=25)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Сбой в цикле бота")
                await asyncio.sleep(5)

    async def poll_once(self, timeout: int = 0) -> int:
        """Process pending Telegram commands during a scheduled cloud run."""
        await self.tg.set_commands(COMMANDS)
        updates = await self.tg.get_updates(self._offset, timeout=timeout)
        for update in updates:
            self._offset = update["update_id"] + 1
            self.db.set_setting("telegram_update_offset", self._offset)
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat_id = str((message.get("chat") or {}).get("id") or "")
            if text and chat_id:
                await self.handle(text, chat_id)
        return len(updates)

    # ------------------------------------------------------------------
    async def handle(self, text: str, chat_id: str) -> None:
        known = self.cfg.chat_id
        if known and chat_id != str(known):
            log.warning("Сообщение из чужого чата %s — игнорирую", chat_id)
            return

        command, _, args = text.partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        args = args.strip()
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            await self.tg.send("Не знаю такой команды. /help покажет список.", chat_id)
            return
        try:
            await handler(args, chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Команда /%s упала", command)
            await self.tg.send(f"Команда сломалась: {esc(exc)}", chat_id)

    # --- команды --------------------------------------------------------
    async def cmd_start(self, args: str, chat_id: str) -> None:
        await self.cmd_help(args, chat_id)

    async def cmd_help(self, args: str, chat_id: str) -> None:
        await self.tg.send(
            HELP.format(
                interval=self.cfg.get("general.scan_interval_minutes", 45),
                threshold=int(float(self.cfg.get("flights.min_discount", 0.7)) * 100),
            ),
            chat_id, disable_preview=True)

    async def cmd_status(self, args: str, chat_id: str) -> None:
        paused = self.db.setting("paused", False)
        scans = self.db.last_scans(3)
        lines = [
            f"<b>Радар {'на паузе ⏸' if paused else 'работает ✅'}</b>",
            f"• Наблюдений в базе: {self.db.observation_count():,}".replace(",", " "),
            f"• Отправлено сегодня: {self.db.sent_today()} из "
            f"{self.cfg.get('general.max_alerts_per_day', 12)}",
            f"• Порог скидки: авиа {int(float(self.cfg.get('flights.min_discount', 0.42)) * 100)} %, "
            f"поезда {int(float(self.cfg.get('trains.min_discount', 0.3)) * 100)} %",
            f"• Бюджет на ночь: {fmt_money(self.cfg.get('traveller', {}).get('nightly_budget', 4500))}",
            "",
            f"<b>Авиа:</b> {esc(self.scanner.status.get('flights'))}",
            f"<b>Поезда:</b> {esc(self.scanner.status.get('trains'))}",
        ]
        if scans:
            lines.append("\n<b>Последние проверки</b>")
            for s in scans:
                when = str(s["finished_at"])[11:16]
                lines.append(
                    f"• {when} {s['kind']}: проверено {s['checked']}, находок {s['found']}"
                    + (f" ⚠️ {esc(s['error'])}" if s["error"] else "")
                )
        enabled = [k for k, v in (self.cfg.get("trip_shapes", {}) or {}).items()
                   if isinstance(v, dict) and v.get("enabled")]
        lines.append("\n<b>Форматы:</b> " + ", ".join(enabled))
        await self.tg.send("\n".join(lines), chat_id, disable_preview=True)

    async def cmd_find(self, args: str, chat_id: str) -> None:
        if self._busy:
            await self.tg.send("Уже ищу, подожди немного.", chat_id)
            return
        await self.tg.send("Смотрю, что есть прямо сейчас…", chat_id)
        self._busy = True
        try:
            limit = int(args) if args.isdigit() else 3
            plans = await self.scanner.find_now(limit=min(limit, 5))
        finally:
            self._busy = False
        if not plans:
            threshold = int(float(self.cfg.get("flights.min_discount", 0.6)) * 100)
            await self.tg.send(
                f"Подходящих вариантов нет.\n\n"
                f"<i>Ничего дешевле обычного на {threshold} % и укладывающегося в бюджет "
                "сейчас не найдено. Показывать «лучшее из плохого» не буду — "
                "продолжаю следить сама.</i>", chat_id)
            return
        self._last_plans = plans
        self._last_plans_at = datetime.now()
        for plan in plans:
            await self.tg.send(render_card(plan), chat_id)
            await asyncio.sleep(0.5)

    async def cmd_details(self, args: str, chat_id: str) -> None:
        """Полный маршрут по последней находке: /details или /details 2."""
        index = max(int(args) - 1, 0) if args.strip().isdigit() else 0
        deals = self.db.recent_deals(10)

        # Что свежее — результаты /find или последнее автоуведомление?
        find_is_fresher = bool(self._last_plans) and (
            not deals or (self._last_plans_at is not None
                          and self._last_plans_at.isoformat() > str(deals[0]["sent_at"]))
        )
        if find_is_fresher and index < len(self._last_plans):
            await self.tg.send(render_plan(self._last_plans[index]), chat_id,
                               disable_preview=True)
            return

        if index >= len(deals):
            await self.tg.send("Столько находок ещё не было. Попробуй /last.", chat_id)
            return
        payload = deals[index].get("payload") or "{}"
        try:
            detail = json.loads(payload).get("detail")
        except json.JSONDecodeError:
            detail = None
        if not detail:
            await self.tg.send(
                "По этой находке подробностей не сохранилось — она из старой версии.",
                chat_id)
            return
        await self.tg.send(detail, chat_id, disable_preview=True)

    async def cmd_scan(self, args: str, chat_id: str) -> None:
        if self._busy:
            await self.tg.send("Проверка уже идёт.", chat_id)
            return
        await self.tg.send("Запускаю полную проверку. Займёт несколько минут.", chat_id)
        self._busy = True
        try:
            flights, trains = await self.scanner.scan_all()
        finally:
            self._busy = False
        await self.tg.send(
            f"Готово. Авиа-находок: {flights}, ж/д-находок: {trains}.\n"
            f"{esc(self.scanner.status.get('flights'))}\n"
            f"{esc(self.scanner.status.get('trains'))}", chat_id)

    async def cmd_last(self, args: str, chat_id: str) -> None:
        deals = self.db.recent_deals(10)
        if not deals:
            await self.tg.send("Пока ничего не присылала.", chat_id)
            return
        lines = ["<b>Последние находки</b>"]
        for d in deals:
            when = str(d["sent_at"])[:16].replace("T", " ")
            lines.append(f"• {when} — {esc(d['dest'])}, {fmt_money(d['price'])}")
        await self.tg.send("\n".join(lines), chat_id)

    async def cmd_threshold(self, args: str, chat_id: str) -> None:
        try:
            value = float(args.replace("%", "").replace(",", "."))
        except ValueError:
            await self.tg.send("Формат: /threshold 50 — слать при скидке от 50 %.", chat_id)
            return
        fraction = value / 100 if value > 1 else value
        fraction = min(max(fraction, 0.1), 0.9)
        self._save_override("flights.min_discount", fraction)
        await self.tg.send(
            f"Теперь присылаю авиабилеты со скидкой от {int(fraction * 100)} %."
            + (" Это довольно строго — уведомлений станет заметно меньше."
               if fraction > 0.55 else ""), chat_id)

    async def cmd_maxprice(self, args: str, chat_id: str) -> None:
        cleaned = args.strip().lower()
        if cleaned in ("нет", "off", "0", "none"):
            self._save_override("flights.max_ticket_price", None)
            await self.tg.send("Потолок цены на билет снят.", chat_id)
            return
        digits = "".join(c for c in cleaned if c.isdigit())
        if not digits:
            await self.tg.send(
                "Формат: /maxprice 40000 — не присылать билеты дороже 40 000 ₽.\n"
                "Снять ограничение: /maxprice off", chat_id)
            return
        self._save_override("flights.max_ticket_price", int(digits))
        await self.tg.send(
            f"Билеты дороже {fmt_money(int(digits))} присылать не буду, "
            "даже если скидка выдающаяся.", chat_id)

    async def cmd_cap(self, args: str, chat_id: str) -> None:
        """Потолок бюджета всей поездки: /cap vacation 120000"""
        caps = dict(self.cfg.get("budget_caps", {}) or {})
        parts = args.split()
        if len(parts) < 2:
            lines = ["<b>Потолок бюджета всей поездки</b>",
                     "<i>Билеты + жильё + еда + виза + транспорт + запас.</i>", ""]
            titles = {"weekend": "трип выходного дня", "vacation": "полноценный отпуск",
                      "long_trip": "длинная поездка", "multi_city": "сложный маршрут",
                      "long_stay": "длительное проживание"}
            for key, title in titles.items():
                value = caps.get(key)
                lines.append(f"• <code>{key}</code> — {title}: "
                             + (fmt_money(value) if value else "без потолка"))
            lines.append("\nИзменить: <code>/cap vacation 120000</code>")
            lines.append("Снять: <code>/cap multi_city off</code>")
            await self.tg.send("\n".join(lines), chat_id)
            return
        key = parts[0]
        if key not in ("weekend", "vacation", "long_trip", "multi_city", "long_stay"):
            await self.tg.send(f"Не знаю формат «{esc(key)}». Список: /cap", chat_id)
            return
        raw = parts[1].lower()
        if raw in ("off", "нет", "0", "none"):
            caps[key] = None
            self._save_override("budget_caps", caps)
            await self.tg.send(f"Потолок для «{esc(key)}» снят.", chat_id)
            return
        digits = "".join(c for c in raw if c.isdigit())
        if not digits:
            await self.tg.send("Формат: /cap vacation 120000", chat_id)
            return
        caps[key] = int(digits)
        self._save_override("budget_caps", caps)
        await self.tg.send(
            f"Потолок для «{esc(key)}»: {fmt_money(int(digits))} на всю поездку.", chat_id)

    async def cmd_budget(self, args: str, chat_id: str) -> None:
        digits = "".join(c for c in args if c.isdigit())
        if not digits:
            await self.tg.send("Формат: /budget 6000 — потолок за ночь жилья.", chat_id)
            return
        traveller = dict(self.cfg.get("traveller", {}) or {})
        traveller["nightly_budget"] = int(digits)
        self._save_override("traveller", traveller)
        await self.tg.send(f"Бюджет на ночь: {fmt_money(int(digits))}.", chat_id)

    async def cmd_shapes(self, args: str, chat_id: str) -> None:
        shapes = self.cfg.get("trip_shapes", {}) or {}
        if not args:
            lines = ["<b>Форматы поездок</b>"]
            for key, value in shapes.items():
                if not isinstance(value, dict):
                    continue
                mark = "✅" if value.get("enabled") else "⬜️"
                lines.append(
                    f"{mark} <code>{key}</code> — {esc(value.get('label', ''))} "
                    f"({value.get('min_nights')}–{value.get('max_nights')} ноч.)"
                )
            lines.append("\nВыключить: <code>/shapes weekend off</code>")
            await self.tg.send("\n".join(lines), chat_id)
            return
        parts = args.split()
        key = parts[0]
        state = parts[1].lower() in ("on", "вкл", "1", "true") if len(parts) > 1 else True
        if key not in shapes:
            await self.tg.send(f"Нет такого формата: {esc(key)}", chat_id)
            return
        section = dict(shapes[key])
        section["enabled"] = state
        self._save_override(f"trip_shapes.{key}", section)
        await self.tg.send(
            f"Формат «{esc(section.get('label', key))}» "
            f"{'включён' if state else 'выключен'}.", chat_id)

    async def cmd_visa(self, args: str, chat_id: str) -> None:
        code = args.strip().upper()
        if not code:
            await self.tg.send("Формат: /visa TH — визовая справка по стране.", chat_id)
            return
        if len(code) > 2:
            match = next((k for k, v in self.cfg.visas.items()
                          if isinstance(v, dict)
                          and str(v.get("name_ru", "")).lower().startswith(args.strip().lower())),
                         None)
            code = match or code
        from datetime import date, timedelta
        verdict = self.scanner.visa.assess(code, date.today() + timedelta(days=30), 7)
        await self.tg.send(verdict.text, chat_id, disable_preview=True)

    async def cmd_addtrain(self, args: str, chat_id: str) -> None:
        if "-" not in args:
            await self.tg.send("Формат: /addtrain Москва - Тюмень", chat_id)
            return
        src, _, dst = args.partition("-")
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            await self.tg.send("Формат: /addtrain Москва - Тюмень", chat_id)
            return
        extra = list(self.db.setting("extra_routes", []) or [])
        extra.append({"from": src, "to": dst, "km": None})
        self.db.set_setting("extra_routes", extra)
        await self.tg.send(
            f"Добавила маршрут {esc(src)} → {esc(dst)}. Первые пару недель буду копить "
            "статистику по ценам, потом начну ловить аномалии.", chat_id)

    async def cmd_reset(self, args: str, chat_id: str) -> None:
        removed = self.db.clear_today()
        await self.tg.send(
            f"Сбросила счётчик за сегодня (было {removed}). "
            "Дневной лимит снова свободен — можно запускать /scan.", chat_id)

    async def cmd_pause(self, args: str, chat_id: str) -> None:
        self.db.set_setting("paused", True)
        await self.tg.send("Радар на паузе. Включить: /resume", chat_id)

    async def cmd_resume(self, args: str, chat_id: str) -> None:
        self.db.set_setting("paused", False)
        await self.tg.send("Радар снова работает.", chat_id)

    async def cmd_test(self, args: str, chat_id: str) -> None:
        await self.tg.send(
            f"Связь есть ✅\nВремя на ноутбуке: {datetime.now():%d.%m.%Y %H:%M}", chat_id)

    # ------------------------------------------------------------------
    def _save_override(self, path: str, value) -> None:
        self.cfg.overrides[path] = value
        self.db.set_setting("overrides", self.cfg.overrides)
