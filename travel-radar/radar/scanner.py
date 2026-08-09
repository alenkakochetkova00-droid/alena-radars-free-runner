"""Сканер: находит аномально дешёвые билеты и присылает готовый маршрут.

Один проход:
  1. широкий обзор мира одним-двумя запросами (что вообще подешевело);
  2. отсев по чёрному списку, формату поездки, комфорту и визовой реальности;
  3. углублённый поиск дат по кандидатам;
  4. сборка маршрута и отправка в Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from .baseline import BaselineEngine
from .comfort import ComfortJudge, season_verdict
from .flights import FlightOffer, TravelpayoutsClient
from .planner import Planner, classify_shape, render_card, render_plan
from .rails import RzdClient, cheapest_by_class, is_comfortable_departure
from .util import fmt_money

log = logging.getLogger("radar.scanner")

MAX_DEEP_DIVE = 14          # сколько направлений углублённо разбираем за скан
TRAIN_ROUTES_PER_SCAN = 12  # маршрутов РЖД за скан (по кругу)
TRAIN_DATES_PER_ROUTE = 6


class Scanner:
    def __init__(self, config, storage, telegram) -> None:
        self.cfg = config
        self.db = storage
        self.tg = telegram
        self.baseline = BaselineEngine(storage, config)
        from .visa import VisaAdvisor  # локальный импорт: избегаем цикла
        self.visa = VisaAdvisor(config)
        self.planner = Planner(config, self.visa)
        self.comfort = ComfortJudge(config)
        self.status: dict = {"flights": "не запускался", "trains": "не запускался"}

    # ------------------------------------------------------------------
    # Общие проверки
    # ------------------------------------------------------------------
    def _blacklisted(self, dest: str) -> bool:
        country = self.cfg.country_of(dest)
        if country in set(self.cfg.get("destinations.blacklist_countries", []) or []):
            return True
        return dest in set(self.cfg.get("destinations.blacklist_cities", []) or [])

    def _within_horizon(self, when: date, section: str) -> bool:
        days = (when - date.today()).days
        return (int(self.cfg.get(f"{section}.min_days_ahead", 7)) <= days
                <= int(self.cfg.get(f"{section}.search_days_ahead", 240)))

    def _quiet_now(self) -> bool:
        quiet = self.cfg.get("general.quiet_hours", [0, 8]) or [0, 8]
        hour = datetime.now().hour
        start, end = int(quiet[0]), int(quiet[1])
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def _cap_reached(self) -> bool:
        return self.db.sent_today() >= int(self.cfg.get("general.max_alerts_per_day", 12))

    async def _deliver(self, plan, silent: bool = False) -> bool:
        dedupe_hours = int(self.cfg.get("general.dedupe_hours", 72))
        if self.db.already_sent(plan.fingerprint, dedupe_hours):
            return False
        if self._cap_reached():
            log.info("Дневной лимит уведомлений исчерпан, придержу %s", plan.fingerprint)
            return False
        # В уведомление уходит короткая карточка; полный маршрут кладём в базу
        # и отдаём по команде /details, чтобы чат не превращался в простыню.
        sent = await self.tg.send(render_card(plan), silent=silent or self._quiet_now())
        if sent:
            self.db.mark_sent(
                plan.fingerprint, plan.kind,
                plan.dest_codes[0] if plan.dest_codes else "",
                ",".join(plan.dest_codes), plan.budget.tickets,
                0.0, {"title": plan.title, "total": plan.budget.total,
                      "detail": render_plan(plan)},
            )
        return sent

    # ------------------------------------------------------------------
    # Авиабилеты
    # ------------------------------------------------------------------
    async def scan_flights(self) -> int:
        started = datetime.now()
        origins = self.cfg.get("flights.origins", ["MOW"]) or ["MOW"]
        found = checked = 0
        error: str | None = None

        async with TravelpayoutsClient(self.cfg.tp_token, self.cfg.tp_marker,
                                       self.cfg.get("general.currency", "rub")) as api:
            for origin in origins:
                pool: list[FlightOffer] = []

                latest = await api.latest_prices(origin=origin, one_way=False, limit=1000)
                pool.extend(latest)
                directions = await api.city_directions(origin)
                pool.extend(directions)
                # По умолчанию выключено: API поддерживает только эконом.
                # См. комментарий у flights.scan_business в config.yaml.
                if self.cfg.get("flights.scan_business", False):
                    business = await api.latest_prices(origin=origin, one_way=False,
                                                       limit=300, trip_class=1)
                    pool.extend(business)

                if not pool and api.last_error:
                    error = api.last_error
                    self.status["flights"] = f"ошибка: {api.last_error}"
                    break

                checked += len(pool)
                self._record_flight_observations(pool)

                candidates = self._rank_candidates(origin, pool)
                deep = candidates[:MAX_DEEP_DIVE]
                log.info("%s: собрано %s предложений, углублённо смотрю %s направлений",
                         origin, len(pool), len(deep))

                # 1) сразу проверяем то, что уже пришло с датами
                for offer in pool:
                    if await self._consider_flight(offer, api):
                        found += 1

                # 2) по лучшим направлениям добираем даты по месяцам
                for dest in deep:
                    for month in self._months_ahead():
                        offers = await api.cheapest_by_month(origin, dest, month, limit=30)
                        if not offers:
                            continue
                        self._record_flight_observations(offers)
                        for offer in offers:
                            # Эти цены уже свежие, повторная проверка не нужна
                            if await self._consider_flight(offer):
                                found += 1
                        if self._cap_reached():
                            break
                    if self._cap_reached():
                        break

                if self.cfg.get("trip_shapes.multi_city.enabled", False) and not self._cap_reached():
                    found += await self._scan_multicity(api, origin, candidates)

        if not error:
            self.status["flights"] = (f"ок, {started:%H:%M}: проверено {checked}, "
                                      f"находок {found}")
        self.db.log_scan("flight", started, checked, found, error)
        return found

    def _record_flight_observations(self, offers: list[FlightOffer]) -> None:
        rows = [
            ("flight", o.origin, o.dest, o.depart_date, o.return_date, o.price, None,
             {"transfers": o.transfers, "airline": o.airline})
            for o in offers if o.price and o.dest
        ]
        if rows:
            self.db.record_many(rows)

    def _rank_candidates(self, origin: str, offers: list[FlightOffer]) -> list[str]:
        """Направления, где текущая цена сильнее всего ниже нормальной."""
        best: dict[str, float] = {}
        for o in offers:
            if not o.dest or self._blacklisted(o.dest):
                continue
            if o.dest not in best or o.price < best[o.dest]:
                best[o.dest] = o.price
        only_known = self.cfg.get("flights.only_known_destinations", True)
        scored: list[tuple[float, str]] = []
        for dest, price in best.items():
            if only_known and not self.cfg.city(dest):
                continue  # не тратим запросы к API на то, что всё равно не пришлём
            normal, _ = self.baseline.normal_flight_price(origin, dest)
            if not normal:
                continue
            scored.append((1 - price / normal, dest))
        scored.sort(reverse=True)
        return [dest for score, dest in scored if score > 0.15]

    def _months_ahead(self) -> list[str]:
        horizon = int(self.cfg.get("flights.search_days_ahead", 240))
        months, cursor = [], date.today().replace(day=1)
        while (cursor - date.today()).days < horizon:
            months.append(f"{cursor:%Y-%m}")
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return months[:8]

    def _passes_basic(self, offer: FlightOffer) -> bool:
        """Общие условия для автоскана и ручного /find — чтобы они не расходились."""
        if not offer.dest or not offer.is_round_trip or self._blacklisted(offer.dest):
            return False
        if not offer.depart_date or not self._within_horizon(offer.depart_date, "flights"):
            return False
        if self.cfg.get("flights.only_known_destinations", True) and not self.cfg.city(offer.dest):
            return False
        return True

    def _within_budget(self, plan, shape_key: str) -> bool:
        """Потолок считается по полной стоимости поездки, а не по цене билета:
        билет за 30 000 в город с жильём по 15 000 за ночь — это не бюджетно."""
        caps = self.cfg.get("budget_caps", {}) or {}
        cap = caps.get(shape_key)
        if cap in (None, 0, False):
            return True
        if plan.budget.total > float(cap):
            log.info("%s отброшен: вся поездка %s дороже потолка %s для «%s»",
                     ",".join(plan.dest_codes), fmt_money(plan.budget.total),
                     fmt_money(float(cap)), shape_key)
            return False
        return True

    def _season_ok(self, offer: FlightOffer, discount: float) -> bool:
        """Дубай в июле не нужен, каким бы дешёвым он ни был."""
        if not self.cfg.get("comfort.season_filter", True):
            return True
        if not offer.depart_date:
            return True
        ok, _ = season_verdict(self.cfg.city(offer.dest), offer.depart_date.month)
        if ok:
            return True
        override = float(self.cfg.get("comfort.season_override_discount", 0.85))
        return discount >= override

    async def _consider_flight(self, offer: FlightOffer,
                               api: TravelpayoutsClient | None = None) -> bool:
        if not self._passes_basic(offer):
            return False

        shape = classify_shape(offer.depart_at, offer.return_at, self.cfg)
        if shape is None:
            return False

        meta = self.cfg.city(offer.dest)
        country = meta.get("country", "")
        km = meta.get("km") or offer.distance_km
        verdict = self.baseline.judge_flight(offer.origin, offer.dest, offer.price, km, country)
        if not verdict.is_deal:
            return False

        if not meta and self.cfg.get("flights.only_known_destinations", True):
            return False

        if not self._season_ok(offer, verdict.discount):
            log.debug("%s отброшен: неподходящий сезон", offer.dest)
            return False

        comfort = self.comfort.judge(offer, km, shape.key)
        if not comfort.ok:
            log.debug("%s отброшен: %s", offer.dest, comfort.blockers[0])
            return False

        max_vac = int((self.cfg.get("traveller", {}) or {}).get("max_vacation_days", 10))
        if shape.key != "long_stay" and shape.vacation_days > max_vac:
            return False

        # Свежая цена: широкие ручки отдают снимки суточной давности, из-за
        # чего цифра в уведомлении расходилась с реальной ценой в поиске.
        if api is not None:
            fresh = await api.verify(offer)
            if fresh is not None:
                drift = abs(fresh.price - offer.price) / max(offer.price, 1)
                if drift > 0.02:
                    log.info("%s: цена уточнена %s → %s", offer.dest,
                             fmt_money(offer.price), fmt_money(fresh.price))
                offer = fresh
                verdict = self.baseline.judge_flight(offer.origin, offer.dest,
                                                     offer.price, km, country)
                if not verdict.is_deal:
                    log.info("%s отброшен: после проверки цена уже не выгодна", offer.dest)
                    return False
                comfort = self.comfort.judge(offer, km, shape.key)
                if not comfort.ok:
                    return False

        plan = self.planner.plan_flight(offer, verdict, shape)
        if plan.blockers and shape.key != "long_stay":
            # Виза не успевает / поездка не помещается — не шлём как «готовый маршрут»
            log.info("%s отброшен: %s", offer.dest, plan.blockers[0])
            return False
        if not self._within_budget(plan, shape.key):
            return False

        delivered = await self._deliver(plan)
        if delivered:
            log.info("Отправлено: %s %s за %s (%s%%)", offer.dest, offer.depart_date,
                     fmt_money(offer.price), verdict.discount_pct)
        return delivered

    # ------------------------------------------------------------------
    # Мультимаршрут: Москва → A → B → Москва
    # ------------------------------------------------------------------
    async def _scan_multicity(self, api: TravelpayoutsClient, origin: str,
                              candidates: list[str]) -> int:
        cfg = self.cfg.get("trip_shapes.multi_city", {}) or {}
        min_nights = int(cfg.get("min_nights", 8))
        max_nights = int(cfg.get("max_nights", 25))
        per_stop = int(cfg.get("min_nights_per_stop", 3))

        by_region: dict[str, list[str]] = defaultdict(list)
        for dest in candidates[:20]:
            region = self.cfg.city(dest).get("region")
            if region:
                by_region[region].append(dest)

        found = 0
        for region, cities in by_region.items():
            if len(cities) < 2 or found >= 2:
                continue
            a, b = cities[0], cities[1]
            out = await api.cheapest_by_month(origin, a, self._months_ahead()[1],
                                              one_way=True, limit=10)
            if not out:
                continue
            leg1 = min(out, key=lambda o: o.price)
            if not leg1.depart_at:
                continue

            middle = await api.cheapest_by_month(a, b, f"{leg1.depart_at:%Y-%m}",
                                                 one_way=True, limit=10)
            middle = [o for o in middle if o.depart_at
                      and o.depart_at.date() >= leg1.depart_at.date() + timedelta(days=per_stop)]
            if not middle:
                continue
            leg2 = min(middle, key=lambda o: o.price)

            back = await api.cheapest_by_month(b, origin, f"{leg2.depart_at:%Y-%m}",
                                               one_way=True, limit=10)
            back = [o for o in back if o.depart_at
                    and o.depart_at.date() >= leg2.depart_at.date() + timedelta(days=per_stop)]
            if not back:
                continue
            leg3 = min(back, key=lambda o: o.price)

            nights = (leg3.depart_at.date() - leg1.depart_at.date()).days
            if not (min_nights <= nights <= max_nights):
                continue
            if not self._within_horizon(leg1.depart_at.date(), "flights"):
                continue

            total = leg1.price + leg2.price + leg3.price
            direct_a, _ = self.baseline.normal_flight_price(origin, a)
            direct_b, _ = self.baseline.normal_flight_price(origin, b)
            if not direct_a or not direct_b:
                continue
            # Честная база сравнения: во что обошлись бы две отдельные поездки
            # туда-обратно в те же страны.
            separate = direct_a + direct_b
            discount = 1 - total / separate
            threshold = float(self.cfg.get("flights.min_discount", 0.6))
            if discount < threshold:
                continue

            verdicts = [
                self.baseline.judge_flight(origin, a, leg1.price,
                                           self.cfg.city(a).get("km"), self.cfg.country_of(a)),
                self.baseline.judge_flight(origin, b, leg3.price,
                                           self.cfg.city(b).get("km"), self.cfg.country_of(b)),
            ]
            plan = self.planner.plan_multicity([leg1, leg2, leg3], verdicts, total, separate)
            if plan.blockers:
                continue
            if not self._within_budget(plan, "multi_city"):
                continue
            if await self._deliver(plan):
                found += 1
                log.info("Сложный маршрут отправлен: %s → %s → %s, экономия %s%%",
                         a, b, origin, int(discount * 100))
        return found

    # ------------------------------------------------------------------
    # Поезда
    # ------------------------------------------------------------------
    async def scan_trains(self) -> int:
        started = datetime.now()
        routes = self.cfg.rail_routes + (self.db.setting("extra_routes", []) or [])
        if not routes:
            return 0

        cursor = int(self.db.setting("train_cursor", 0) or 0)
        slice_routes = [routes[(cursor + i) % len(routes)]
                        for i in range(min(TRAIN_ROUTES_PER_SCAN, len(routes)))]
        self.db.set_setting("train_cursor", (cursor + len(slice_routes)) % len(routes))

        found = checked = 0
        error: str | None = None

        async with RzdClient(self.db) as rzd:
            alive, message = await rzd.ping()
            if not alive:
                self.status["trains"] = f"РЖД недоступна: {message}"
                self.db.log_scan("train", started, 0, 0, message)
                return 0

            for route in slice_routes:
                for when in self._train_dates():
                    offers = await rzd.search(route["from"], route["to"], when)
                    if not offers:
                        continue
                    checked += len(offers)
                    wanted = set(self.cfg.get("trains.classes", []) or [])
                    best = cheapest_by_class(
                        [o for o in offers
                         if (not wanted or o.klass in wanted or
                             o.klass.startswith(tuple(w[:4] for w in wanted)))
                         and is_comfortable_departure(o)]
                    )
                    self.db.record_many([
                        ("train", route["from"], route["to"], when, None, o.price, o.klass,
                         {"number": o.number})
                        for o in best.values()
                    ])
                    for offer in best.values():
                        verdict = self.baseline.judge_train(
                            route["from"], route["to"], offer.klass, offer.price,
                            route.get("km"), route,
                        )
                        if not verdict.is_deal:
                            continue
                        back = await self._find_return_train(rzd, route, when)
                        nights = ((back.depart_date - when).days if back else 2)
                        plan = self.planner.plan_train(offer, verdict, back, max(nights, 1), route)
                        if await self._deliver(plan):
                            found += 1
                    if self._cap_reached():
                        break
                if self._cap_reached():
                    break

            if rzd.last_error:
                error = rzd.last_error

        self.status["trains"] = (f"ок, {started:%H:%M}: маршрутов {len(slice_routes)}, "
                                 f"находок {found}")
        self.db.log_scan("train", started, checked, found, error)
        return found

    def _train_dates(self) -> list[date]:
        """Ближайшие выходные + пара будних дат в горизонте."""
        horizon = int(self.cfg.get("trains.search_days_ahead", 90))
        min_ahead = int(self.cfg.get("trains.min_days_ahead", 2))
        dates: list[date] = []
        cursor = date.today() + timedelta(days=min_ahead)
        while (cursor - date.today()).days <= horizon and len(dates) < TRAIN_DATES_PER_ROUTE:
            if cursor.weekday() in (4, 5):        # пятница и суббота — старт поездки
                dates.append(cursor)
            cursor += timedelta(days=1)
        return dates

    async def _find_return_train(self, rzd: RzdClient, route: dict, out_date: date):
        """Обратный поезд через 2–3 ночи — чтобы поездка была осмысленной."""
        for delta in (2, 3):
            when = out_date + timedelta(days=delta)
            offers = await rzd.search(route["to"], route["from"], when)
            usable = [o for o in offers if is_comfortable_departure(o)]
            if usable:
                return min(usable, key=lambda o: o.price)
        return None

    # ------------------------------------------------------------------
    async def scan_all(self) -> tuple[int, int]:
        flights = trains = 0
        if self.cfg.get("flights.enabled", True):
            try:
                flights = await self.scan_flights()
            except Exception as exc:  # noqa: BLE001 — один упавший скан не должен убивать сервис
                log.exception("Скан авиабилетов упал")
                self.status["flights"] = f"ошибка: {exc}"
        if self.cfg.get("trains.enabled", True):
            try:
                trains = await self.scan_trains()
            except Exception as exc:  # noqa: BLE001
                log.exception("Скан поездов упал")
                self.status["trains"] = f"ошибка: {exc}"
        self.db.prune(keep_days=180)
        return flights, trains

    # ------------------------------------------------------------------
    async def find_now(self, limit: int = 5) -> list:
        """Ручной поиск по команде /find — возвращает лучшие варианты без фильтра дедупа."""
        results = []
        async with TravelpayoutsClient(self.cfg.tp_token, self.cfg.tp_marker) as api:
            for origin in self.cfg.get("flights.origins", ["MOW"]):
                pool = await api.latest_prices(origin=origin, one_way=False, limit=1000)
                pool += await api.city_directions(origin)
                scored = []
                for offer in pool:
                    # Ровно те же фильтры, что и в автоскане: иначе /find
                    # показывал варианты, которые радар сам никогда не пришлёт.
                    if not self._passes_basic(offer):
                        continue
                    shape = classify_shape(offer.depart_at, offer.return_at, self.cfg)
                    if shape is None:
                        continue
                    meta = self.cfg.city(offer.dest)
                    km = meta.get("km") or offer.distance_km
                    verdict = self.baseline.judge_flight(
                        origin, offer.dest, offer.price, km, meta.get("country", ""))
                    # Ручной поиск не должен показывать то, что радар сам бы не
                    # прислал: иначе в тихий день выдаёт «лучшее из плохого»
                    # вроде скидки в 7 %.
                    if not verdict.is_deal:
                        continue
                    if not self._season_ok(offer, verdict.discount):
                        continue
                    if not self.comfort.judge(offer, km, shape.key).ok:
                        continue
                    scored.append((verdict.discount, offer, verdict, shape))
                scored.sort(key=lambda x: x[0], reverse=True)
                for _, offer, verdict, shape in scored[:limit]:
                    fresh = await api.verify(offer)
                    if fresh is not None:
                        offer = fresh
                        verdict = self.baseline.judge_flight(
                            origin, offer.dest, offer.price,
                            self.cfg.city(offer.dest).get("km") or offer.distance_km,
                            self.cfg.country_of(offer.dest))
                        if not verdict.is_deal:
                            continue
                    plan = self.planner.plan_flight(offer, verdict, shape)
                    if plan.blockers or not self._within_budget(plan, shape.key):
                        continue
                    results.append(plan)
        return results


async def run_once(config, storage, telegram) -> tuple[int, int]:
    scanner = Scanner(config, storage, telegram)
    return await scanner.scan_all()


async def periodic(scanner: Scanner, interval_minutes: int) -> None:
    while True:
        try:
            flights, trains = await scanner.scan_all()
            log.info("Скан завершён: авиа %s, поезда %s", flights, trains)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле сканирования")
        await asyncio.sleep(max(interval_minutes, 5) * 60)
