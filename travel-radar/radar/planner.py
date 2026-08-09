"""Планировщик: превращает найденный билет в готовый маршрут.

Отвечает на вопросы «сколько отпуска потрачу», «где жить», «что делать по дням»,
«сколько всего денег» и «что с визой» — то есть на всё, что обычно приходится
досчитывать руками после того, как увидела дешёвый билет.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .comfort import ComfortJudge, season_verdict
from .flights import FlightOffer
from .rails import TrainOffer, overnight_bonus
from .util import (
    MONTHS_NOM, WEEKDAYS_FULL, days_word, esc, fmt_date, fmt_money, nights_word,
)

# ---------------------------------------------------------------------------
# Формат поездки
# ---------------------------------------------------------------------------


@dataclass
class Shape:
    key: str
    label: str
    vacation_days: int
    fit_note: str


def classify_shape(depart: datetime | None, ret: datetime | None, cfg) -> Shape | None:
    """Проверяет, укладывается ли билет в один из разрешённых сценариев поездки."""
    if not depart or not ret:
        return None
    nights = (ret.date() - depart.date()).days
    if nights <= 0:
        return None
    shapes = cfg.get("trip_shapes", {}) or {}

    w = shapes.get("weekend", {})
    if w.get("enabled") and w.get("min_nights", 3) <= nights <= w.get("max_nights", 4):
        dep_wd, ret_wd = depart.weekday(), ret.weekday()
        dep_hour = depart.hour
        ok_out = (
            (dep_wd == 3 and dep_hour >= int(w.get("depart_after_hour", 16)))
            or (dep_wd == 4 and dep_hour <= int(w.get("depart_friday_before_hour", 11)))
        )
        ok_back = ret_wd in tuple(w.get("return_weekdays", [6, 0])) and (
            ret_wd != 6 or ret.hour >= int(w.get("return_after_hour", 15))
        )
        if ok_out and ok_back:
            note = ("вылет в четверг вечером, берёшь один день отпуска в пятницу, "
                    "возвращаешься в воскресенье вечером")
            if dep_wd == 4:
                note = "вылет утром в пятницу (один день отпуска), назад — вечером в воскресенье"
            return Shape("weekend", w.get("label", "Длинные выходные"),
                         int(w.get("vacation_days_used", 1)), note)

    v = shapes.get("vacation", {})
    if v.get("enabled") and v.get("min_nights", 5) <= nights <= v.get("max_nights", 9):
        vac_days = _workdays_between(depart.date(), ret.date())
        return Shape("vacation", v.get("label", "Стандартный отпуск"), vac_days,
                     f"нужно {days_word(vac_days)} отпуска (рабочих дней внутри поездки)")

    m = shapes.get("multi_city", {})
    if m.get("enabled") and m.get("min_nights", 8) <= nights <= m.get("max_nights", 25):
        vac_days = _workdays_between(depart.date(), ret.date())
        return Shape("long_trip", "Длинная поездка", vac_days,
                     f"нужно {days_word(vac_days)} отпуска")

    ls = shapes.get("long_stay", {})
    if ls.get("enabled") and ls.get("min_nights", 25) <= nights <= ls.get("max_nights", 180):
        return Shape("long_stay", ls.get("label", "Долгая зимовка"), 0,
                     "формат для удалённой работы или длинного отпуска без возврата в офис")

    return None


def _workdays_between(start: date, end: date) -> int:
    """Рабочие дни между вылетом и возвращением (включая день вылета)."""
    days, cur = 0, start
    while cur < end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# План
# ---------------------------------------------------------------------------


@dataclass
class Budget:
    tickets: float = 0.0
    stay: float = 0.0
    food: float = 0.0
    visa: float = 0.0
    transfers: float = 0.0
    insurance: float = 0.0
    reserve: float = 0.0

    @property
    def total(self) -> float:
        return sum((self.tickets, self.stay, self.food, self.visa,
                    self.transfers, self.insurance, self.reserve))


@dataclass
class TripPlan:
    kind: str                     # flight | train | multicity
    title: str
    shape: Shape | None
    depart: datetime | None
    ret: datetime | None
    nights: int
    dest_codes: list[str]
    countries: list[str]
    budget: Budget = field(default_factory=Budget)
    verdict_text: str = ""
    blocks: list[str] = field(default_factory=list)   # готовые HTML-блоки
    blockers: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    fingerprint: str = ""
    # Строки для короткой карточки — то, что видно в уведомлении сразу
    card: list[str] = field(default_factory=list)
    buy_url: str = ""


# ---------------------------------------------------------------------------
# Ссылки
# ---------------------------------------------------------------------------


def compare_links(origin: str, dest: str, depart: date, ret: date | None,
                  marker: str = "") -> list[tuple[str, str]]:
    """Одна и та же дата-пара на четырёх площадках — цены у них расходятся,
    поэтому сравнить стоит до покупки."""
    code = f"{origin}{depart:%d%m}{dest}" + (f"{ret:%d%m}" if ret else "") + "1"
    aviasales = f"https://www.aviasales.ru/search/{code}"
    if marker:
        aviasales += f"?marker={marker}"
    sky = (f"https://www.skyscanner.ru/transport/flights/{origin.lower()}/{dest.lower()}/"
           f"{depart:%y%m%d}/" + (f"{ret:%y%m%d}/" if ret else ""))
    tutu_route = f"{origin}{depart:%d.%m}{dest}" + (f"{ret:%d.%m}" if ret else "")
    tutu = f"https://avia.tutu.ru/offers/?route={tutu_route}&passengers=1000&class=Y"
    gq = urllib.parse.quote(
        f"Flights from {origin} to {dest} on {depart:%Y-%m-%d}"
        + (f" through {ret:%Y-%m-%d}" if ret else "")
    )
    google = f"https://www.google.com/travel/flights?q={gq}"
    return [("Aviasales", aviasales), ("Skyscanner", sky),
            ("Туту", tutu), ("Google Flights", google)]


def maps_link(query: str) -> str:
    return f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"


def weather_link(city_ru: str) -> str:
    return f"https://yandex.ru/pogoda/search?request={urllib.parse.quote(city_ru)}"


# ---------------------------------------------------------------------------
# Сборка плана по авиабилету
# ---------------------------------------------------------------------------


class Planner:
    def __init__(self, config, visa_advisor) -> None:
        self.cfg = config
        self.visa = visa_advisor
        self.comfort = ComfortJudge(config)

    # -- ограничения путешественницы ------------------------------------
    def offer_is_comfortable(self, offer: FlightOffer,
                             distance_km: float | None = None) -> tuple[bool, list[str]]:
        verdict = self.comfort.judge(offer, distance_km)
        return verdict.ok, verdict.blockers

    # -- основной билд ---------------------------------------------------
    def plan_flight(self, offer: FlightOffer, verdict, shape: Shape) -> TripPlan:
        dest = offer.dest
        meta = self.cfg.city(dest)
        country = meta.get("country", "")
        city_ru = meta.get("ru", dest)
        depart, ret = offer.depart_at, offer.return_at
        nights = offer.nights or 0
        checkin = depart.date() if depart else date.today()
        checkout = ret.date() if ret else checkin + timedelta(days=nights)

        origin_ru = self.cfg.city_name(offer.origin) if self.cfg.city(offer.origin) else "Москва"
        plan = TripPlan(
            kind="flight",
            title=f"{origin_ru} → {city_ru} → {origin_ru}",
            shape=shape, depart=depart, ret=ret, nights=nights,
            dest_codes=[dest], countries=[country] if country else [],
        )
        plan.buy_url = offer.booking_url if offer.link else offer.search_url(self.cfg.tp_marker)

        # 1. Что нашлось и почему это выгодно
        plan.blocks.append(self._block_headline(offer, verdict, meta, shape))

        if not meta:
            plan.blocks.append(
                f"❓ <b>Города {esc(dest)} нет в моём справочнике</b>\n"
                "Цена очень хорошая, но визу, жильё и трансфер придётся проверить самой — "
                "деталей по этому направлению у меня нет. Если оно интересно, добавь его "
                "в <code>data/cities.json</code>, и в следующий раз пришлю полный маршрут."
            )

        # 2. Перелёт
        comfort = self.comfort.judge(offer, meta.get("km") or offer.distance_km, shape.key)
        plan.blocks.append(self._block_flight(offer, meta, comfort))

        # 3. Виза
        visa = None
        if country:
            visa = self.visa.assess(country, checkin, nights)
            plan.blocks.append(visa.text)
            plan.blockers.extend(visa.blockers)
            plan.budget.visa = visa.cost_rub
            if shape.key == "long_stay" and not self.visa.can_stay_long(country):
                plan.blockers.append(
                    f"{visa.name_ru}: легально прожить несколько месяцев не получится."
                )

        # 4. Где жить
        plan.blocks.append(self._block_stay(meta, dest, checkin, checkout, nights))

        # 5. План по дням
        plan.blocks.append(self._block_itinerary(meta, city_ru, checkin, nights, shape))

        # 6. Бюджет
        self._fill_budget(plan, offer.price, meta, nights, dest)
        plan.blocks.append(self._block_budget(plan.budget, nights))

        # 7. Ссылки: одни и те же даты на четырёх площадках
        plan.links = compare_links(offer.origin, dest, checkin,
                                   checkout if offer.return_at else None,
                                   self.cfg.tp_marker)
        plan.fingerprint = (
            f"flight:{offer.origin}:{dest}:{checkin:%Y%m%d}:{checkout:%Y%m%d}:"
            f"{int(offer.price // 1000)}"
        )
        # 8. Короткая карточка — её и увидишь в уведомлении
        plan.card = self._card(offer, verdict, shape, meta, visa, plan,
                               checkin, checkout, comfort)
        return plan

    def _card(self, offer: FlightOffer, verdict, shape: Shape, meta: dict, visa,
              plan: TripPlan, checkin: date, checkout: date,
              comfort=None) -> list[str]:
        nights = plan.nights
        nice = self.cfg.nice_hotel_price(offer.dest)
        cabin = " · <b>БИЗНЕС-КЛАСС</b>" if offer.trip_class else ""
        lines = [
            f"<b>{esc(shape.label.upper())}</b>{cabin}",
            f"📅 {checkin:%d.%m} – {checkout:%d.%m} · {nights_word(nights)}"
            + (f" · отпуска {days_word(shape.vacation_days)}" if shape.vacation_days else ""),
            "",
            f"✈️ Билеты <b>{fmt_money(offer.price)}</b>"
            + (f"  <s>{fmt_money(verdict.normal)}</s>  −{verdict.discount_pct} %"
               if verdict.discount > 0.01 else ""),
            f"🏨 Жильё ≈ {fmt_money(nice)}/ночь → {fmt_money(nice * nights)}",
            f"🧮 Вся поездка ≈ <b>{fmt_money(plan.budget.total)}</b>",
            "",
        ]
        if offer.depart_at:
            lines.append(
                f"🛫 Туда {offer.depart_at:%d.%m} в {offer.depart_at:%H:%M}"
                + (f", {offer.duration_to // 60} ч" if offer.duration_to else "")
            )
        if offer.return_at:
            lines.append(
                f"🛬 Назад {offer.return_at:%d.%m} в {offer.return_at:%H:%M}"
                + (f", {offer.duration_back // 60} ч" if offer.duration_back else "")
            )
        if comfort is not None:
            lines.append(f"🔁 {esc(comfort.summary)}")
            fleet = self.comfort.fleet_line(comfort, offer.airline)
            if fleet:
                lines.append(esc(fleet))
            lines.append(esc(self.comfort.baggage_line(comfort)))
        if visa is not None:
            lines.append(visa.short)  # значок режима уже внутри
        ok_season, season_text = season_verdict(meta, checkin.month)
        lines.append(("🌤 " if ok_season else "⚠️ ") + esc(season_text))
        return lines

    # -- блоки -----------------------------------------------------------
    def _block_headline(self, offer: FlightOffer, verdict, meta: dict, shape: Shape) -> str:
        lines = [f"💰 <b>{fmt_money(offer.price)}</b> туда-обратно"]
        if verdict.discount > 0.01 and verdict.normal > 0:
            lines[0] += f" (обычно ≈ {fmt_money(verdict.normal)})"
            lines.append(f"📉 Дешевле обычного на <b>{verdict.discount_pct} %</b> — "
                         f"экономия {fmt_money(verdict.saved)}")
        lines.append(f"🗓 <b>{shape.label}</b>: {shape.fit_note}")
        if verdict.confidence != "своя статистика":
            lines.append("<i>Сравниваю со стартовой оценкой: своей статистики по маршруту "
                         "пока мало, через пару недель точность вырастет.</i>")
        return "\n".join(lines)

    def _block_flight(self, offer: FlightOffer, meta: dict, comfort=None) -> str:
        lines = ["✈️ <b>Перелёт</b>"]
        if offer.trip_class:
            lines.append("💺 <b>Бизнес-класс</b>")
        if offer.depart_at:
            lines.append(
                f"• Туда: {fmt_date(offer.depart_at.date())}, вылет в "
                f"{offer.depart_at:%H:%M}"
                + (f", в пути {offer.duration_to // 60} ч {offer.duration_to % 60:02d} мин"
                   if offer.duration_to else "")
                + (f", пересадок: {offer.transfers}" if offer.transfers else ", без пересадок")
            )
        if offer.return_at:
            lines.append(
                f"• Обратно: {fmt_date(offer.return_at.date())}, вылет в "
                f"{offer.return_at:%H:%M}"
                + (f", в пути {offer.duration_back // 60} ч {offer.duration_back % 60:02d} мин"
                   if offer.duration_back else "")
                + (f", пересадок: {offer.return_transfers}" if offer.return_transfers
                   else ", без пересадок")
            )
        if offer.airline:
            lines.append(f"• Авиакомпания: {esc(self.cfg.airline_name(offer.airline))} "
                         f"{esc(offer.flight_number)}")
        if comfort is not None:
            fleet = self.comfort.fleet_line(comfort, offer.airline)
            if fleet:
                lines.append(f"• {esc(fleet)}")
            lines.append(f"• {esc(self.comfort.baggage_line(comfort))}")
            for note in comfort.notes:
                lines.append(f"• {esc(note)}")
            for blocker in comfort.blockers:
                lines.append(f"⚠️ {esc(blocker)}")
        transfer = meta.get("transfer")
        if transfer:
            lines.append(f"🚕 <b>Из аэропорта:</b> {esc(transfer)}")
        return "\n".join(lines)

    def _block_stay(self, meta: dict, dest: str, checkin: date, checkout: date,
                    nights: int) -> str:
        """Только ориентир по цене красивого жилья — бронирование через
        туроператоров или trip.com, поэтому никаких ссылок на агрегаторы."""
        nice = self.cfg.nice_hotel_price(dest)
        lines = [
            f"🏨 <b>Где жить</b> ({nights_word(nights)}, "
            f"{checkin:%d.%m} → {checkout:%d.%m})",
            f"• Хорошее эстетичное жильё: ≈ {fmt_money(nice)} за ночь, "
            f"{fmt_money(nice * nights)} за поездку",
            "<i>Ориентир уровня trip.com — дизайн-отели и красивые апартаменты, "
            "не эконом.</i>",
        ]
        for area in (meta.get("areas") or [])[:3]:
            lines.append(f"• {esc(area)}")
        return "\n".join(lines)

    def _block_itinerary(self, meta: dict, city_ru: str, checkin: date, nights: int,
                         shape: Shape) -> str:
        lines = ["🗺 <b>План поездки</b>"]
        month_note = ""
        good = meta.get("months") or []
        if good:
            month_note = (
                f"{MONTHS_NOM[checkin.month - 1].capitalize()} — "
                + ("хороший месяц для направления." if checkin.month in good
                   else "не лучший месяц: проверь погоду и что работает из сезонного.")
            )
        if month_note:
            lines.append(f"🌤 {esc(month_note)} "
                         f'<a href="{weather_link(city_ru)}">Прогноз</a>')

        areas = meta.get("areas") or []
        base = areas[0].split("—")[0].strip() if areas else "центр"

        if shape.key == "long_stay":
            lines += [
                f"• <b>Неделя 1.</b> База в районе «{esc(base)}»: снять жильё помесячно "
                "(на месте всегда дешевле, чем онлайн), сим-карта, банковская карта если можно.",
                "• <b>Неделя 2–3.</b> Быт: спортзал, коворкинг, врач, продуктовые рынки. "
                "Оформить продление пребывания, если нужно — не тянуть до последней недели.",
                "• <b>Дальше.</b> Раз в 2–3 недели — поездка по стране на 2–3 дня, "
                "чтобы не выгореть от одного места.",
                "• <b>За 30 дней до конца.</b> Проверить дату окончания легального пребывания "
                "и купить билет назад заранее.",
            ]
            return "\n".join(lines)

        days = nights + 1
        arrival_note = ("Прилетаешь вечером — сегодня только заселение, ужин рядом с жильём "
                        "и спать." if shape.key == "weekend" else
                        "День прилёта: заселение, прогулка по району, ранний ужин без планов.")
        lines.append(f"• <b>День 1.</b> {esc(arrival_note)}")

        tip = meta.get("tip")
        highlights = [
            "Главный маршрут: исторический центр пешком, смотровая точка, локальный обед.",
            "Музей или парк утром, вторая половина дня — соседний район без туристов.",
            "Выезд за город на полдня (по расписанию местного транспорта — уточни накануне).",
            "Рынок с утра, шопинг и кофейни, вечером — район с барами.",
            "Свободный день: пляж/спа/спокойные прогулки, чтобы не приехать домой уставшей.",
            "Второй выезд за город или соседний город на день.",
            "Повтор любимого места из первых дней плюс подарки домой.",
        ]
        for i in range(2, min(days, 9)):
            if i == days:
                break
            lines.append(f"• <b>День {i}.</b> {esc(highlights[(i - 2) % len(highlights)])}")
        lines.append(
            f"• <b>День {days}.</b> Вылет. Заложи на дорогу в аэропорт время из блока выше "
            "плюс 2 часа на регистрацию (3 часа, если рейс с багажом и пересадкой)."
        )
        if tip:
            lines.append(f"💡 <b>Про город:</b> {esc(tip)}")
        lines.append(f'📍 <a href="{maps_link(city_ru)}">Открыть на карте</a>')
        return "\n".join(lines)

    def _fill_budget(self, plan: TripPlan, ticket_price: float, meta: dict, nights: int,
                     dest: str = "") -> None:
        b = plan.budget
        b.tickets = ticket_price
        b.stay = float(self.cfg.nice_hotel_price(dest) if dest
                       else meta.get("hotel", 4000)) * nights
        b.food = float(meta.get("food", 2000)) * (nights + 1)
        b.transfers = 2500 + float(meta.get("km", 3000)) * 0.15
        b.insurance = 180 * (nights + 1)
        b.reserve = (b.tickets + b.stay + b.food + b.visa + b.transfers) * 0.12

    def _block_budget(self, b: Budget, nights: int) -> str:
        rows = [
            ("Билеты", b.tickets), ("Жильё", b.stay), ("Еда и кафе", b.food),
            ("Виза и сборы", b.visa), ("Транспорт на месте", b.transfers),
            ("Страховка", b.insurance), ("Запас 12 %", b.reserve),
        ]
        lines = ["🧮 <b>Сколько стоит вся поездка</b>"]
        for name, value in rows:
            if value > 0:
                lines.append(f"• {name}: {fmt_money(value)}")
        lines.append(f"<b>Итого ≈ {fmt_money(b.total)}</b> "
                     f"({fmt_money(b.total / max(nights + 1, 1))} в день)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Поезда
    # ------------------------------------------------------------------
    def plan_train(self, there: TrainOffer, verdict, back: TrainOffer | None,
                   nights: int, route_meta: dict) -> TripPlan:
        title = (f"{there.origin} → {there.dest} → {there.origin}" if back
                 else f"{there.origin} → {there.dest}")
        plan = TripPlan(
            kind="train", title=f"{title} · поезд",
            shape=Shape("train", "Поездка по России", _workdays_between(
                there.depart_date, there.depart_date + timedelta(days=nights)), ""),
            depart=datetime.combine(there.depart_date, datetime.min.time()),
            ret=None, nights=nights, dest_codes=[there.dest], countries=["RU"],
        )
        total = there.price + (back.price if back else 0)

        head = [
            f"🚂 <b>{esc(title)}</b>",
            f"💰 {fmt_money(there.price)} в одну сторону ({esc(there.klass)}), "
            f"обычно ≈ {fmt_money(verdict.normal)} — дешевле на "
            f"<b>{verdict.discount_pct} %</b>",
        ]
        if back:
            head.append(f"↩️ Обратный билет на {fmt_date(back.depart_date)}: "
                        f"{fmt_money(back.price)} ({esc(back.klass)})")
            head.append(f"🧾 Оба билета: <b>{fmt_money(total)}</b>")
        plan.blocks.append("\n".join(head))

        det = [
            "🕓 <b>Поезд</b>",
            f"• {esc(there.number)}"
            + (f" «{esc(there.brand)}»" if there.brand else "")
            + f", отправление {fmt_date(there.depart_date)} в {esc(there.depart_time)}, "
              f"прибытие в {esc(there.arrive_time)}, в пути {esc(there.duration)}",
            f"• {esc(there.from_station)} → {esc(there.to_station)}",
        ]
        if there.free_seats:
            det.append(f"• Свободных мест: {there.free_seats} — "
                       + ("разбирают быстро, брать сегодня" if there.free_seats < 12
                          else "запас есть"))
        if overnight_bonus(there):
            det.append("🌙 Ночной поезд: экономишь ночь в отеле и целый день на месте.")
        if route_meta.get("note"):
            det.append(f"ℹ️ {esc(route_meta['note'])}")
        plan.blocks.append("\n".join(det))

        b = plan.budget
        b.tickets = total
        # Хороший бутик-отель или красивые апартаменты в российском городе
        nice_night = 6000
        b.stay = nice_night * nights
        b.food = 2000 * (nights + 1)
        b.transfers = 1200
        b.reserve = (b.tickets + b.stay + b.food) * 0.1
        plan.blocks.append(self._block_budget(b, nights))

        plan.buy_url = there.booking_url
        plan.links = [("Купить на РЖД", there.booking_url)]
        plan.fingerprint = (f"train:{there.origin}:{there.dest}:{there.depart_date:%Y%m%d}:"
                            f"{there.klass}:{int(there.price // 100)}")
        plan.card = [
            f"📅 {there.depart_date:%d.%m} · {nights_word(nights)}",
            "",
            f"🚂 Билет <b>{fmt_money(there.price)}</b> ({esc(there.klass)})"
            + (f"  <s>{fmt_money(verdict.normal)}</s>  −{verdict.discount_pct} %"
               if verdict.discount > 0.01 else ""),
            f"🕓 Отправление {esc(there.depart_time)}, в пути {esc(there.duration)}",
            f"🏨 Жильё ≈ {fmt_money(nice_night)}/ночь → {fmt_money(nice_night * nights)}",
            f"🧮 Вся поездка ≈ <b>{fmt_money(b.total)}</b>",
        ]
        if back:
            plan.card.insert(3, f"↩️ Обратно {back.depart_date:%d.%m}: "
                                f"{fmt_money(back.price)} ({esc(back.klass)})")
        if there.free_seats and there.free_seats < 12:
            plan.card.append(f"⚠️ Мест осталось {there.free_seats} — разберут быстро")
        return plan

    # ------------------------------------------------------------------
    # Мультимаршрут
    # ------------------------------------------------------------------
    def plan_multicity(self, legs: list[FlightOffer], verdicts: list,
                       total_price: float, separate_cost: float | None = None) -> TripPlan:
        stops = [leg.dest for leg in legs[:-1]] or [legs[0].dest]
        names = [self.cfg.city_name(c) for c in stops]
        first, last = legs[0], legs[-1]
        origin_ru = self.cfg.city_name(first.origin)
        nights = ((last.depart_at.date() - first.depart_at.date()).days
                  if first.depart_at and last.depart_at else 0)
        countries = [self.cfg.country_of(c) for c in stops if self.cfg.country_of(c)]
        full_route = " → ".join([origin_ru, *names, origin_ru])

        plan = TripPlan(
            kind="multicity",
            title=full_route,
            shape=Shape("multi_city", "Сложный маршрут",
                        _workdays_between(first.depart_at.date(),
                                          last.depart_at.date()) if first.depart_at else 0,
                        "несколько стран за одну поездку"),
            depart=first.depart_at, ret=last.return_at or last.depart_at,
            nights=nights, dest_codes=stops, countries=countries,
        )

        # Выгода считается честно: сколько стоило бы съездить в те же страны
        # двумя отдельными поездками туда-обратно.
        saved = max(0.0, (separate_cost or 0) - total_price)
        discount_pct = int(round(saved / separate_cost * 100)) if separate_cost else 0

        head = [
            f"🌍 <b>{esc(full_route)}</b>",
            f"💰 Все перелёты: <b>{fmt_money(total_price)}</b> за {nights_word(nights)}",
        ]
        if separate_cost:
            head.append(
                f"📉 Двумя отдельными поездками вышло бы {fmt_money(separate_cost)} — "
                f"экономия {fmt_money(saved)} ({discount_pct} %)"
            )
        plan.blocks.append("\n".join(head))

        seg_lines = ["✈️ <b>Перелёты по порядку</b>"]
        for number, leg in enumerate(legs, 1):
            if not leg.depart_at:
                continue
            seg_lines.append(
                f"{number}. {fmt_date(leg.depart_at.date())} в {leg.depart_at:%H:%M} — "
                f"<b>{esc(self.cfg.city_name(leg.origin))} → "
                f"{esc(self.cfg.city_name(leg.dest))}</b>, {fmt_money(leg.price)}"
                + (f", пересадок {leg.transfers}" if leg.transfers else ", прямой")
                + (f", {leg.duration_to // 60} ч" if leg.duration_to else "")
            )
        plan.blocks.append("\n".join(seg_lines))

        # Виза по каждой стране
        cursor = first.depart_at.date() if first.depart_at else date.today()
        per_stop = max(3, nights // max(len(stops), 1))
        for stop in stops:
            country = self.cfg.country_of(stop)
            if not country:
                continue
            visa = self.visa.assess(country, cursor, per_stop)
            plan.blocks.append(visa.text)
            plan.blockers.extend(visa.blockers)
            plan.budget.visa += visa.cost_rub
            cursor += timedelta(days=per_stop)

        # Жильё и бюджет по всем остановкам
        b = plan.budget
        b.tickets = total_price
        stay_cursor = first.depart_at.date() if first.depart_at else date.today()
        stay_lines = ["🏨 <b>Где жить по остановкам</b>"]
        for stop in stops:
            meta = self.cfg.city(stop)
            city_ru = meta.get("ru", stop)
            checkout = stay_cursor + timedelta(days=per_stop)
            nice = self.cfg.nice_hotel_price(stop)
            b.stay += nice * per_stop
            b.food += float(meta.get("food", 2000)) * per_stop
            areas = (meta.get("areas") or ["центр"])[0]
            stay_lines.append(
                f"• <b>{esc(city_ru)}</b> ({per_stop} ноч., {stay_cursor:%d.%m}–"
                f"{checkout:%d.%m}): {esc(areas)}, ≈ {fmt_money(nice)}/ночь"
            )
            stay_cursor = checkout
        plan.blocks.append("\n".join(stay_lines))

        b.transfers = 3000 * len(stops)
        b.insurance = 180 * (nights + 1)
        b.reserve = (b.tickets + b.stay + b.food + b.visa + b.transfers) * 0.12
        plan.blocks.append(self._block_budget(b, nights))

        plan.buy_url = legs[0].search_url(self.cfg.tp_marker)
        plan.links = []
        for leg in legs:
            if not leg.depart_at:
                continue
            label = (f"{self.cfg.city_name(leg.origin)[:12]}–"
                     f"{self.cfg.city_name(leg.dest)[:12]}")
            plan.links.append(
                (label, compare_links(leg.origin, leg.dest, leg.depart_at.date(),
                                      None, self.cfg.tp_marker)[0][1])
            )
        plan.fingerprint = ("multi:" + "-".join(stops) + ":"
                            + (f"{first.depart_at:%Y%m%d}" if first.depart_at else "")
                            + f":{int(total_price // 1000)}")

        card = [f"<b>СЛОЖНЫЙ МАРШРУТ</b>"]
        if first.depart_at and last.depart_at:
            card.append(f"📅 {first.depart_at:%d.%m} – {last.depart_at:%d.%m} · "
                        f"{nights_word(nights)} · {len(set(countries))} страны")
        card.append("")
        for number, leg in enumerate(legs, 1):
            if not leg.depart_at:
                continue
            card.append(
                f"{number}. {leg.depart_at:%d.%m} "
                f"{esc(self.cfg.city_name(leg.origin))} → "
                f"{esc(self.cfg.city_name(leg.dest))} — {fmt_money(leg.price)}"
            )
        card.append("")
        card.append(f"✈️ Все перелёты <b>{fmt_money(total_price)}</b>")
        if separate_cost:
            card.append(f"📉 Отдельными поездками — {fmt_money(separate_cost)}, "
                        f"экономия <b>{discount_pct} %</b>")
        card.append(f"🏨 Жильё ≈ {fmt_money(b.stay)} за {nights_word(nights)}")
        card.append(f"🧮 Вся поездка ≈ <b>{fmt_money(b.total)}</b>")
        for stop in stops:
            country = self.cfg.country_of(stop)
            if country:
                visa = self.visa.assess(country, first.depart_at.date() if first.depart_at
                                        else date.today(), per_stop)
                card.append(f"{esc(self.cfg.city_name(stop))}: {visa.short}")
        plan.card = card
        return plan


# ---------------------------------------------------------------------------
# Рендер в сообщение Telegram
# ---------------------------------------------------------------------------


def render_card(plan: TripPlan) -> str:
    """Короткая карточка: всё, что нужно, чтобы за десять секунд решить
    «беру / не беру». Подробности — по команде /details."""
    parts = [f"🔥 <b>{esc(plan.title)}</b>", *plan.card]
    if plan.blockers:
        parts.append("⛔️ " + esc(plan.blockers[0]))
    if plan.links:
        parts.append("\n🔗 " + " · ".join(
            f'<a href="{esc(url)}">{esc(name)}</a>' for name, url in plan.links[:4]))
    elif plan.buy_url:
        parts.append(f'\n🔗 <a href="{esc(plan.buy_url)}">Купить билет</a>')
    parts.append("<code>/details</code> — весь маршрут с планом по дням")
    return "\n".join(parts)


def render_plan(plan: TripPlan) -> str:
    header = f"🔥 <b>{esc(plan.title)}</b>"
    if plan.depart:
        wd = WEEKDAYS_FULL[plan.depart.weekday()]
        header += f"\n<i>{esc(wd.capitalize())}, {fmt_date(plan.depart.date())}</i>"
    parts = [header, *plan.blocks]

    if plan.blockers:
        parts.append("⛔️ <b>Что мешает</b>\n" + "\n".join(f"• {esc(b)}" for b in plan.blockers))

    if plan.links:
        parts.append("🔗 <b>Сравнить цену на площадках</b>\n"
                     + " · ".join(f'<a href="{esc(url)}">{esc(name)}</a>'
                                  for name, url in plan.links))
    parts.append("<i>Цена найдена агрегатором и может отличаться от той, что покажет "
                 "площадка: тарифы меняются в течение минут. Открывай сразу несколько "
                 "ссылок — расхождение в 10–20 % между площадками это норма.</i>")
    return "\n\n".join(parts)
