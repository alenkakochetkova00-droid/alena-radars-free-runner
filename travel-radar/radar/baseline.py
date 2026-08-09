"""Что считать «нормальной» ценой и когда цена — аномалия.

Логика в двух словах:
  * пока своей статистики мало — опираемся на стартовую таблицу baselines.json;
  * как только накопилось ≥ 20 наблюдений по маршруту — считаем медиану по ним;
  * находкой считается цена ниже медианы на заданный процент И ниже жёсткого
    потолка по дальности, чтобы «−60 % от космической цены» не проходило.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .storage import Storage, percentile
from .util import distance_band, rail_distance_band

MIN_SAMPLES_FOR_OWN_STATS = 20
# Меньше этого числа наблюдений «нормой» считать нельзя: по одному замеру
# норма равна самой цене, скидка выходит 0 %, и новое направление навсегда
# остаётся невидимым. Лучше признать, что данных нет.
MIN_SAMPLES_FOR_FALLBACK = 8


@dataclass
class Verdict:
    is_deal: bool
    price: float
    normal: float
    discount: float           # 0.55 = дешевле обычного на 55 %
    band: str
    reason: str
    confidence: str           # "своя статистика" | "стартовая оценка"

    @property
    def discount_pct(self) -> int:
        return int(round(self.discount * 100))

    @property
    def saved(self) -> float:
        return max(0.0, self.normal - self.price)


class BaselineEngine:
    def __init__(self, storage: Storage, config) -> None:
        self.db = storage
        self.cfg = config

    # ------------------------------------------------------------------
    def normal_flight_price(self, origin: str, dest: str) -> tuple[float | None, str]:
        history = self.db.price_history("flight", origin, dest, days=120)
        if len(history) >= MIN_SAMPLES_FOR_OWN_STATS:
            # медиана «дешёвых» предложений: берём 50-й перцентиль всего, что видели
            value = percentile(history, 0.5)
            return (value, "своя статистика")
        seed = self.cfg.baselines.get(dest.upper())
        if seed:
            return (float(seed), "стартовая оценка")
        if len(history) >= MIN_SAMPLES_FOR_FALLBACK:
            return (percentile(history, 0.5), "мало данных")
        return (None, "нет данных")

    def normal_train_price(self, origin: str, dest: str, klass: str,
                           route_meta: dict | None = None) -> tuple[float | None, str]:
        history = self.db.price_history("train", origin, dest, days=120, klass=klass)
        if len(history) >= MIN_SAMPLES_FOR_OWN_STATS:
            return (percentile(history, 0.5), "своя статистика")
        if route_meta:
            key = {"Плацкарт": "base_plaz", "Купе": "base_kupe"}.get(klass)
            if key and route_meta.get(key):
                return (float(route_meta[key]), "стартовая оценка")
            # СВ/Люкс ≈ 1.9× купе, сидячий ≈ 0.7× плацкарта
            if klass in ("СВ", "Люкс") and route_meta.get("base_kupe"):
                return (float(route_meta["base_kupe"]) * 1.9, "стартовая оценка")
            if klass in ("Сидячий", "Общий") and route_meta.get("base_plaz"):
                return (float(route_meta["base_plaz"]) * 0.7, "стартовая оценка")
        if len(history) >= MIN_SAMPLES_FOR_FALLBACK:
            return (percentile(history, 0.5), "мало данных")
        return (None, "нет данных")

    # ------------------------------------------------------------------
    def judge_flight(self, origin: str, dest: str, price: float,
                     km: float | None = None, country: str = "") -> Verdict:
        normal, confidence = self.normal_flight_price(origin, dest)
        band = distance_band(km)
        caps = self.cfg.get("flights.hard_caps", {}) or {}
        cap = float(caps.get(band, 10**9))

        threshold = float(self.cfg.get("flights.min_discount", 0.42))
        wishlist = set(self.cfg.get("destinations.wishlist_countries", []) or [])
        if country and country in wishlist:
            threshold = max(0.20, threshold - 0.07)

        hard_max = self.cfg.get("flights.max_ticket_price")
        if hard_max and price > float(hard_max):
            return Verdict(False, price, normal or 0.0, 0.0, band,
                           f"дороже твоего потолка {int(float(hard_max)):,} ₽".replace(",", " "),
                           confidence)

        if normal is None:
            # Нормы нет — значит, и аномалии быть не может. Судить по одному
            # только потолку нельзя: любой дешёвый внутренний рейс (Москва —
            # Тамбов за 7 000 ₽) формально пройдёт и завалит чат мусором.
            # Молча копим наблюдения: через пару дней появится своя статистика.
            return Verdict(False, price, 0.0, 0.0, band,
                           "нет нормальной цены для сравнения — коплю статистику",
                           confidence)

        discount = 1 - (price / normal) if normal > 0 else 0.0

        # Потолок — защита от «−60 % от космического ценника», но выдающуюся
        # скидку он блокировать не должен: 62 000 ₽ до Буэнос-Айреса при обычных
        # 160 000 ₽ — это находка, даже если абсолютная цифра выше потолка.
        exceptional = float(self.cfg.get("flights.exceptional_discount", 0.55))
        stretch = float(self.cfg.get("flights.cap_stretch", 1.6))
        effective_cap = cap * stretch if discount >= exceptional else cap
        if price > effective_cap:
            return Verdict(False, price, normal, discount, band,
                           f"дороже потолка для этой дальности "
                           f"({int(effective_cap):,} ₽)".replace(",", " "),
                           confidence)
        if discount < threshold:
            return Verdict(False, price, normal, discount, band,
                           f"скидка {int(discount * 100)} % меньше порога "
                           f"{int(threshold * 100)} %", confidence)
        return Verdict(True, price, normal, discount, band,
                       f"дешевле обычного на {int(discount * 100)} %", confidence)

    def judge_train(self, origin: str, dest: str, klass: str, price: float,
                    km: float | None = None, route_meta: dict | None = None) -> Verdict:
        normal, confidence = self.normal_train_price(origin, dest, klass, route_meta)
        band = rail_distance_band(km)
        caps = self.cfg.get("trains.hard_caps", {}) or {}
        cap = float(caps.get(band, 10**9))
        threshold = float(self.cfg.get("trains.min_discount", 0.30))

        if normal is None:
            return Verdict(False, price, 0.0, 0.0, band,
                           "нет нормальной цены для сравнения — коплю статистику",
                           confidence)
        discount = 1 - (price / normal) if normal > 0 else 0.0
        if price > cap:
            return Verdict(False, price, normal, discount, band, "дороже потолка", confidence)
        if discount < threshold:
            return Verdict(False, price, normal, discount, band,
                           f"скидка {int(discount * 100)} % меньше порога", confidence)
        return Verdict(True, price, normal, discount, band,
                       f"дешевле обычного на {int(discount * 100)} %", confidence)

    # ------------------------------------------------------------------
    def seasonality_note(self, dest: str, when: date) -> str | None:
        """Не самый ли это «мёртвый сезон» — иначе дешевизна объяснима."""
        meta = self.cfg.city(dest)
        good = meta.get("months") or []
        if not good:
            return None
        if when.month in good:
            return "сезон подходящий — цена низкая не из-за погоды"
        return ("это не лучший месяц для направления — вероятно, поэтому и дёшево; "
                "посмотри прогноз погоды перед покупкой")
