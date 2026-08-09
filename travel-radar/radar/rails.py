"""Источник данных по поездам — публичный API pass.rzd.ru.

Работает по двухшаговому протоколу РЖД: первый запрос возвращает RID,
вторым забираем результат. Требует cookie-сессию и человеческие заголовки.
Если РЖД недоступна (бывает при запросах не из РФ), провайдер честно
сообщает об ошибке, а сканер продолжает работу по авиабилетам.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

import aiohttp

log = logging.getLogger("radar.rails")

TIMETABLE = "https://pass.rzd.ru/timetable/public/ru"
SUGGESTER = "https://pass.rzd.ru/suggester"
BUY_URL = "https://pass.rzd.ru/tickets/public/ru"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://pass.rzd.ru/",
    "X-Requested-With": "XMLHttpRequest",
}

CLASS_ALIASES = {
    "Плац": "Плацкарт", "Плацкарт": "Плацкарт",
    "Купе": "Купе", "СВ": "СВ", "Люкс": "Люкс", "Мягкий": "Люкс",
    "Сид": "Сидячий", "Сидячий": "Сидячий",
    "Общий": "Общий",
}


@dataclass
class TrainOffer:
    origin: str                 # название города отправления
    dest: str
    depart_date: date
    number: str
    brand: str
    depart_time: str
    arrive_time: str
    duration: str
    from_station: str
    to_station: str
    klass: str
    price: float
    free_seats: int = 0
    firm: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def booking_url(self) -> str:
        return (f"{BUY_URL}?STRUCTURE_ID=735&layer_id=5371&dir=0&tfl=3&checkSeats=1"
                f"&code0={self.raw.get('code0','')}&dt0={self.depart_date:%d.%m.%Y}"
                f"&code1={self.raw.get('code1','')}")

    @property
    def tutu_url(self) -> str:
        return "https://www.tutu.ru/poezda/"


class RzdClient:
    def __init__(self, storage=None, min_interval: float = 1.5) -> None:
        self.storage = storage
        self._min_interval = min_interval
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None
        self.available: bool = True

    async def __aenter__(self):
        jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=35), headers=HEADERS, cookie_jar=jar
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _throttle(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            delta = loop.time() - self._last_call
            if delta < self._min_interval:
                await asyncio.sleep(self._min_interval - delta)
            self._last_call = loop.time()

    async def _raw_get(self, url: str, params: dict) -> dict | list | None:
        assert self._session is not None, "клиент используется вне async with"
        await self._throttle()
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status >= 400:
                    self.last_error = f"РЖД: HTTP {resp.status}"
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.last_error = f"РЖД недоступна ({type(exc).__name__})"
            self.available = False
            return None
        except Exception as exc:  # noqa: BLE001 — не даём упасть всему скану
            self.last_error = f"РЖД: {exc}"
            return None

    # ------------------------------------------------------------------
    async def station_code(self, name: str) -> str | None:
        """Резолвит название города в код станции РЖД, с кешем в БД."""
        key = name.strip()
        if self.storage:
            cached = self.storage.station_code(key)
            if cached:
                return cached
        data = await self._raw_get(
            SUGGESTER, {"stationNamePart": key.upper()[:12], "lang": "ru", "compactMode": "y"}
        )
        if not isinstance(data, list) or not data:
            return None
        # Предпочитаем точное совпадение, иначе первый вариант
        best = next((x for x in data if str(x.get("n", "")).lower() == key.lower()), data[0])
        code = str(best.get("c") or "")
        if not code:
            return None
        if self.storage:
            self.storage.save_station(key, code, str(best.get("n", "")))
        return code

    async def search(self, origin: str, dest: str, when: date,
                     max_polls: int = 6) -> list[TrainOffer]:
        code0 = await self.station_code(origin)
        code1 = await self.station_code(dest)
        if not code0 or not code1:
            self.last_error = f"не найден код станции: {origin} / {dest}"
            return []

        params = {
            "layer_id": "5827", "dir": "0", "tfl": "3", "checkSeats": "1",
            "code0": code0, "code1": code1, "dt0": when.strftime("%d.%m.%Y"), "md": "0",
        }
        data = await self._raw_get(TIMETABLE, params)
        if not isinstance(data, dict):
            return []

        # Шаг 2: если вернулся RID — опрашиваем до готовности
        polls = 0
        while data.get("result") == "RID" and polls < max_polls:
            rid = data.get("RID") or data.get("rid")
            if not rid:
                break
            await asyncio.sleep(1.4)
            data = await self._raw_get(TIMETABLE, {**params, "rid": str(rid)})
            polls += 1
            if not isinstance(data, dict):
                return []

        if data.get("result") not in ("OK", "RID"):
            self.last_error = f"РЖД вернула result={data.get('result')}"
            return []

        self.available = True
        self.last_error = None
        return self._parse(data, origin, dest, when, code0, code1)

    def _parse(self, data: dict, origin: str, dest: str, when: date,
               code0: str, code1: str) -> list[TrainOffer]:
        offers: list[TrainOffer] = []
        for tp in data.get("tp") or []:
            for train in tp.get("list") or []:
                base = {
                    "number": str(train.get("number") or train.get("number2") or ""),
                    "brand": str(train.get("brand") or "").strip(),
                    "time0": str(train.get("time0") or ""),
                    "time1": str(train.get("time1") or ""),
                    "duration": self._duration(train),
                    "route0": str(train.get("station0") or train.get("route0") or origin),
                    "route1": str(train.get("station1") or train.get("route1") or dest),
                    "firm": bool(train.get("firm")),
                }
                for car in train.get("cars") or []:
                    tariff = car.get("tariff") or car.get("tariff2")
                    try:
                        price = float(str(tariff).replace(" ", "").replace(",", "."))
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    klass = CLASS_ALIASES.get(str(car.get("type") or "").strip(),
                                              str(car.get("type") or "").strip())
                    offers.append(TrainOffer(
                        origin=origin, dest=dest, depart_date=when,
                        number=base["number"], brand=base["brand"],
                        depart_time=base["time0"], arrive_time=base["time1"],
                        duration=base["duration"],
                        from_station=base["route0"], to_station=base["route1"],
                        klass=klass, price=price,
                        free_seats=int(car.get("freeSeats") or 0),
                        firm=base["firm"],
                        raw={"code0": code0, "code1": code1, "car": car},
                    ))
        return offers

    @staticmethod
    def _duration(train: dict) -> str:
        minutes = train.get("timeInWay") or train.get("timeInWayAll")
        if isinstance(minutes, str) and ":" in minutes:
            return minutes.replace(":", " ч ") + " мин"
        try:
            total = int(minutes)
        except (TypeError, ValueError):
            return "—"
        return f"{total // 60} ч {total % 60:02d} мин"

    async def ping(self) -> tuple[bool, str]:
        code = await self.station_code("Москва")
        if code:
            return True, f"OK (Москва = {code})"
        return False, self.last_error or "нет ответа от pass.rzd.ru"


def cheapest_by_class(offers: list[TrainOffer]) -> dict[str, TrainOffer]:
    """Самый дешёвый вариант в каждом классе за день."""
    best: dict[str, TrainOffer] = {}
    for offer in offers:
        current = best.get(offer.klass)
        if current is None or offer.price < current.price:
            best[offer.klass] = offer
    return best


def parse_hhmm(value: str) -> int:
    try:
        h, m = value.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -1


def is_comfortable_departure(offer: TrainOffer) -> bool:
    """Отсекает поезда с отправлением в 2–5 утра — это не отдых."""
    minutes = parse_hhmm(offer.depart_time)
    if minutes < 0:
        return True
    return not (2 * 60 <= minutes < 5 * 60)


def overnight_bonus(offer: TrainOffer) -> bool:
    """Ночной поезд экономит ночь в отеле — плюс к привлекательности."""
    dep = parse_hhmm(offer.depart_time)
    arr = parse_hhmm(offer.arrive_time)
    if dep < 0 or arr < 0:
        return False
    return dep >= 19 * 60 and 5 * 60 <= arr <= 11 * 60
