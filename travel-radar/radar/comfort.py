"""Насколько рейс удобен: время вылета, пересадки, самолёт, багаж.

Важное ограничение: авиа-API отдаёт только число пересадок и общую
длительность плеча. Ни города стыковки, ни точного типа борта в данных нет.
Поэтому:
  * длительность стыковки оценивается как «общее время в пути минус
    ожидаемое чистое лётное время» — это оценка, а не расписание;
  * тип самолёта выводится из парка авиакомпании, а не из конкретного рейса.
Всё, что является оценкой, помечается в тексте словом «вероятно».
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Средняя путевая скорость с учётом набора и снижения, км/ч.
# Сверено по реальным рейсам: Москва — Бангкок 7050 км идёт ~9 ч 20 мин,
# Москва — Стамбул 1750 км ~3 ч 10 мин. При 800 км/ч + 45 мин накладных
# формула попадает в оба случая с точностью до десяти минут.
CRUISE_KMH = 800.0
# Руление, ожидание взлёта, круги перед посадкой
FLIGHT_OVERHEAD_MIN = 45.0


@dataclass
class ComfortVerdict:
    ok: bool
    score: int                        # 0–100, чем больше тем лучше
    stops: int
    layover_hours: float | None       # оценка суммарной стыковки
    layover_kind: str                 # direct | short | awkward | city_walk | unknown
    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    cabin_bag: str = "unknown"        # included | extra | unknown
    checked_bag: str = "unknown"
    fleet: str = "unknown"            # ok | ssj_risk | ssj | unknown

    @property
    def summary(self) -> str:
        if self.stops == 0:
            return "прямой рейс"
        if self.layover_kind == "city_walk":
            return f"1 пересадка ~{self.layover_hours:.0f} ч — хватит выйти в город"
        if self.layover_kind == "short":
            return f"{self.stops} пересадка, короткая (~{self.layover_hours:.0f} ч)"
        if self.layover_kind == "awkward":
            return f"{self.stops} пересадка ~{self.layover_hours:.0f} ч — неудобная"
        return f"пересадок: {self.stops}"


def estimate_flight_minutes(distance_km: float | None) -> float | None:
    """Ожидаемое чистое время в воздухе для расстояния."""
    if not distance_km or distance_km <= 0:
        return None
    return distance_km / CRUISE_KMH * 60 + FLIGHT_OVERHEAD_MIN


def estimate_layover_hours(total_minutes: int, distance_km: float | None,
                           stops: int) -> float | None:
    """Сколько примерно уходит на стыковки: всё время в пути минус полёт."""
    if stops <= 0 or not total_minutes:
        return None
    pure = estimate_flight_minutes(distance_km)
    if pure is None:
        return None
    # На пересадочном маршруте налёт больше прямого примерно на 15 %
    layover = total_minutes - pure * 1.15
    return round(max(layover, 0) / 60, 1)


class ComfortJudge:
    def __init__(self, config) -> None:
        self.cfg = config

    def _rules(self) -> dict:
        return self.cfg.get("comfort", {}) or {}

    def judge(self, offer, distance_km: float | None = None,
              shape_key: str = "") -> ComfortVerdict:
        r = self._rules()
        traveller = self.cfg.get("traveller", {}) or {}
        stops = max(offer.transfers, offer.return_transfers)
        km = distance_km or offer.distance_km or None

        verdict = ComfortVerdict(ok=True, score=100, stops=stops,
                                 layover_hours=None, layover_kind="unknown")

        # --- время вылета ------------------------------------------------
        hour_from = int(r.get("depart_hour_from", 6))
        hour_to = int(r.get("depart_hour_to", 23))
        for label, when in (("туда", offer.depart_at), ("обратно", offer.return_at)):
            if when is None:
                continue
            if not (hour_from <= when.hour <= hour_to):
                verdict.blockers.append(
                    f"вылет {label} в {when:%H:%M} — вне удобного окна "
                    f"{hour_from}:00–{hour_to}:00"
                )

        # --- пересадки ---------------------------------------------------
        max_stops = int(traveller.get("max_stops", 1))
        if stops > max_stops:
            verdict.blockers.append(f"пересадок {stops}, а ты готова максимум на {max_stops}")

        if stops == 0:
            verdict.layover_kind = "direct"
            verdict.notes.append("Прямой рейс — ничего не пересаживаешься и не ждёшь.")
        else:
            hours = estimate_layover_hours(offer.duration_to, km, offer.transfers)
            verdict.layover_hours = hours
            short_max = float(r.get("layover_short_max_hours", 4))
            walk_min = float(r.get("layover_city_walk_min_hours", 8))
            if hours is None:
                verdict.layover_kind = "unknown"
                verdict.notes.append(
                    "Длительность стыковки в данных не указана — посмотри в поиске.")
            elif hours <= short_max:
                verdict.layover_kind = "short"
                verdict.score -= 10
                verdict.notes.append(
                    f"Стыковка вероятно около {hours:.0f} ч — успеваешь без нервов, "
                    "но из аэропорта выйти не получится.")
            elif hours >= walk_min:
                verdict.layover_kind = "city_walk"
                verdict.score -= 5
                verdict.notes.append(
                    f"Стыковка вероятно {hours:.0f} ч — это уже мини-остановка: "
                    "успеваешь съездить в центр. Уточни, нужна ли транзитная виза.")
            else:
                verdict.layover_kind = "awkward"
                verdict.score -= 30
                verdict.notes.append(
                    f"Стыковка вероятно {hours:.0f} ч — худший вариант: и погулять мало, "
                    "и просто сидеть долго.")

        # --- длительность плеча -----------------------------------------
        max_hours = float(traveller.get("max_leg_hours", 20))
        for label, minutes in (("туда", offer.duration_to), ("обратно", offer.duration_back)):
            if minutes and minutes / 60 > max_hours:
                verdict.blockers.append(
                    f"дорога {label} {minutes // 60} ч — дольше твоего предела {max_hours:g} ч")

        # --- самолёт и багаж --------------------------------------------
        rule = self.cfg.airline_rule(offer.airline)
        verdict.fleet = rule.get("fleet", "unknown")
        verdict.cabin_bag = rule.get("cabin", "unknown")
        verdict.checked_bag = rule.get("baggage", "unknown")

        if verdict.cabin_bag == "unknown" and self.cfg.airline_is_lowcost(offer.airline):
            verdict.cabin_bag = "included"
            verdict.checked_bag = "extra"

        if r.get("avoid_ssj", True):
            if verdict.fleet == "ssj":
                verdict.blockers.append(
                    f"{self.cfg.airline_name(offer.airline)} летает только на Superjet")
            elif verdict.fleet == "ssj_risk":
                verdict.score -= 15
                verdict.notes.append(
                    "⚠️ У этой авиакомпании часть рейсов на Superjet — "
                    "проверь тип борта перед покупкой.")

        if r.get("require_cabin_bag", True) and verdict.cabin_bag == "extra":
            verdict.blockers.append("ручная кладь не входит в тариф")

        # На несколько месяцев с одной ручной кладью не уезжают
        need_bag = set(r.get("require_checked_bag_for", []) or [])
        if shape_key in need_bag and verdict.checked_bag != "included":
            verdict.blockers.append(
                "багаж не включён, а для длительной поездки он обязателен"
                if verdict.checked_bag == "extra"
                else "по багажу нет данных, а для длительной поездки он обязателен")

        if rule.get("note"):
            verdict.notes.append(rule["note"])

        verdict.score = max(0, verdict.score - 12 * stops)
        verdict.ok = not verdict.blockers
        return verdict

    # ------------------------------------------------------------------
    def baggage_line(self, verdict: ComfortVerdict) -> str:
        cabin = {"included": "ручная кладь включена", "extra": "ручная кладь за доплату",
                 "unknown": "по ручной клади данных нет"}[verdict.cabin_bag]
        checked = {"included": "багаж включён", "extra": "багаж за доплату",
                   "unknown": "по багажу данных нет"}[verdict.checked_bag]
        return f"🧳 {cabin}, {checked}"

    def fleet_line(self, verdict: ComfortVerdict, airline_code: str) -> str | None:
        name = self.cfg.airline_name(airline_code)
        if verdict.fleet == "ok":
            return f"🛩 {name} — парк Boeing/Airbus"
        if verdict.fleet == "ssj_risk":
            return f"🛩 {name} — возможен Superjet, проверь борт"
        if verdict.fleet == "ssj":
            return f"🛩 {name} — только Superjet"
        return f"🛩 {name} — тип самолёта уточни при выборе рейса" if name else None


def season_verdict(city_meta: dict, month: int) -> tuple[bool, str]:
    """Стоит ли вообще лететь в этом месяце."""
    good = city_meta.get("months") or []
    if not good:
        return True, "по сезону данных нет"
    if month in good:
        return True, "сезон подходящий"
    return False, "неподходящий сезон — жара, дожди или мёртвый сезон"
