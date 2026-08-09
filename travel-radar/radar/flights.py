"""Источник авиаданных — Travelpayouts (Aviasales) Data API.

Бесплатный токен: https://www.travelpayouts.com → Инструменты → API.
Все запросы троттлятся, ошибки не роняют скан.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import aiohttp

log = logging.getLogger("radar.flights")

BASE = "https://api.travelpayouts.com"
AVIASALES = "https://www.aviasales.ru"


@dataclass
class FlightOffer:
    origin: str
    dest: str
    price: float
    depart_at: datetime | None
    return_at: datetime | None
    airline: str = ""
    flight_number: str = ""
    transfers: int = 0
    return_transfers: int = 0
    duration_to: int = 0          # минуты
    duration_back: int = 0
    origin_airport: str = ""
    dest_airport: str = ""
    link: str = ""
    distance_km: float = 0.0
    trip_class: int = 0          # 0 эконом, 1 бизнес, 2 первый
    raw: dict = field(default_factory=dict)

    @property
    def depart_date(self) -> date | None:
        return self.depart_at.date() if self.depart_at else None

    @property
    def return_date(self) -> date | None:
        return self.return_at.date() if self.return_at else None

    @property
    def is_round_trip(self) -> bool:
        return self.return_at is not None

    @property
    def nights(self) -> int | None:
        if not (self.depart_at and self.return_at):
            return None
        return (self.return_at.date() - self.depart_at.date()).days

    @property
    def booking_url(self) -> str:
        if self.link:
            return f"{AVIASALES}{self.link}" if self.link.startswith("/") else self.link
        return self.search_url()

    def search_url(self, marker: str = "") -> str:
        """Ссылка на поиск Aviasales вида /search/MOW0410BKK18101."""
        if not self.depart_at:
            return f"{AVIASALES}/"
        code = f"{self.origin}{self.depart_at:%d%m}{self.dest}"
        if self.return_at:
            code += f"{self.return_at:%d%m}"
        code += "1"
        url = f"{AVIASALES}/search/{code}"
        return f"{url}?marker={marker}" if marker else url


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class TravelpayoutsClient:
    def __init__(self, token: str, marker: str = "", currency: str = "rub",
                 min_interval: float = 1.1) -> None:
        self.token = token
        self.marker = marker
        self.currency = currency
        self._min_interval = min_interval
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40),
            headers={"X-Access-Token": self.token, "Accept-Encoding": "gzip"},
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _get(self, path: str, params: dict) -> dict | None:
        if not self.token:
            self.last_error = "нет TRAVELPAYOUTS_TOKEN"
            return None
        assert self._session is not None, "клиент используется вне async with"
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("currency", self.currency)
        params["token"] = self.token

        async with self._lock:
            delta = asyncio.get_running_loop().time() - self._last_call
            if delta < self._min_interval:
                await asyncio.sleep(self._min_interval - delta)
            self._last_call = asyncio.get_running_loop().time()

        for attempt in range(3):
            try:
                async with self._session.get(f"{BASE}{path}", params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    if resp.status == 401:
                        self.last_error = "Travelpayouts: токен отклонён (401)"
                        log.error(self.last_error)
                        return None
                    if resp.status >= 400:
                        self.last_error = f"Travelpayouts {path}: HTTP {resp.status}"
                        log.warning(self.last_error)
                        return None
                    self.last_error = None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(2 * (attempt + 1))
        log.warning("Не удалось получить %s: %s", path, self.last_error)
        return None

    # ------------------------------------------------------------------
    def _to_offers(self, rows) -> list[FlightOffer]:
        offers: list[FlightOffer] = []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            # v3 отдаёт price, v2/prices/latest — value
            price = row.get("price", row.get("value"))
            if not price:
                continue
            transfers = row.get("transfers", row.get("number_of_changes")) or 0
            offers.append(
                FlightOffer(
                    origin=(row.get("origin") or "").upper(),
                    dest=(row.get("destination") or "").upper(),
                    price=float(price),
                    depart_at=_dt(row.get("departure_at") or row.get("depart_date")),
                    return_at=_dt(row.get("return_at") or row.get("return_date")),
                    airline=row.get("airline") or "",
                    flight_number=str(row.get("flight_number") or ""),
                    transfers=int(transfers),
                    return_transfers=int(row.get("return_transfers") or transfers),
                    duration_to=int(row.get("duration_to") or row.get("duration") or 0),
                    duration_back=int(row.get("duration_back") or 0),
                    origin_airport=row.get("origin_airport") or "",
                    dest_airport=row.get("destination_airport") or "",
                    link=row.get("link") or "",
                    distance_km=float(row.get("distance") or 0),
                    trip_class=int(row.get("trip_class") or 0),
                    raw=row,
                )
            )
        return offers

    # ------------------------------------------------------------------
    async def city_directions(self, origin: str) -> list[FlightOffer]:
        """Самые дешёвые направления из города — один запрос на весь мир."""
        data = await self._get("/v1/city-directions", {"origin": origin})
        if not data:
            return []
        return self._to_offers(data.get("data", {}))

    async def cheapest_by_month(self, origin: str, dest: str, month: str,
                                one_way: bool = False, direct: bool | None = None,
                                limit: int = 50) -> list[FlightOffer]:
        """month — 'YYYY-MM'. Возвращает варианты по разным датам этого месяца."""
        params = {
            "origin": origin,
            "destination": dest,
            "departure_at": month,
            "one_way": str(bool(one_way)).lower(),
            "sorting": "price",
            "unique": "false",
            "limit": limit,
            "page": 1,
            "market": "ru",
        }
        if direct is not None:
            params["direct"] = str(direct).lower()
        data = await self._get("/aviasales/v3/prices_for_dates", params)
        if not data:
            return []
        return self._to_offers(data.get("data", []))

    async def prices_for_dates(self, origin: str, dest: str, depart: date,
                               ret: date | None = None) -> list[FlightOffer]:
        params = {
            "origin": origin,
            "destination": dest,
            "departure_at": depart.isoformat(),
            "return_at": ret.isoformat() if ret else None,
            "one_way": "false" if ret else "true",
            "sorting": "price",
            "limit": 30,
            "market": "ru",
        }
        data = await self._get("/aviasales/v3/prices_for_dates", params)
        if not data:
            return []
        return self._to_offers(data.get("data", []))

    async def latest_prices(self, origin: str | None = None, dest: str | None = None,
                            one_way: bool = False, limit: int = 1000,
                            trip_class: int = 0) -> list[FlightOffer]:
        """Самые дешёвые билеты, найденные другими пользователями за 48 часов."""
        params = {
            "origin": origin,
            "destination": dest,
            "one_way": str(bool(one_way)).lower(),
            "period_type": "year",
            "page": 1,
            "limit": limit,
            "show_to_affiliates": "true",
            "sorting": "price",
            "trip_class": trip_class,
        }
        data = await self._get("/v2/prices/latest", params)
        if not data:
            return []
        offers = self._to_offers(data.get("data", []))
        for offer in offers:
            offer.trip_class = offer.trip_class or trip_class
        return offers

    async def verify(self, offer: FlightOffer) -> FlightOffer | None:
        """Перепроверяет цену свежим запросом на конкретные даты.

        Широкие ручки отдают кешированные снимки цен (иногда сутками), из-за
        чего в поиске цифра оказывается другой. Перед отправкой уведомления
        добираем актуальную цену на эти же даты.
        """
        if not offer.depart_date:
            return None
        fresh = await self.prices_for_dates(offer.origin, offer.dest,
                                            offer.depart_date, offer.return_date)
        if not fresh:
            return None
        best = min(fresh, key=lambda o: o.price)
        best.trip_class = best.trip_class or offer.trip_class
        if not best.distance_km:
            best.distance_km = offer.distance_km
        return best

    async def month_matrix(self, origin: str, dest: str, month: str) -> list[FlightOffer]:
        """Сгруппированные цены по дням месяца — дешевле по количеству запросов."""
        params = {
            "origin": origin, "destination": dest,
            "departure_at": month, "group_by": "departure_at",
            "market": "ru", "trip_class": 0,
        }
        data = await self._get("/aviasales/v3/grouped_prices", params)
        if not data:
            return []
        return self._to_offers(data.get("data", {}))

    async def ping(self) -> tuple[bool, str]:
        data = await self._get("/v1/city-directions", {"origin": "MOW"})
        if data and data.get("success") is not False:
            return True, f"OK, направлений: {len(data.get('data', {}))}"
        return False, self.last_error or "нет ответа"
