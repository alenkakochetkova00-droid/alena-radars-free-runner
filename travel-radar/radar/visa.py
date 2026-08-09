"""Визовый модуль для гражданки РФ: что нужно, сколько стоит, успею ли."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .util import days_word, esc, fmt_money

REGIME_LABEL = {
    "visa_free": "🟢 Виза не нужна",
    "eta": "🟡 Электронное разрешение",
    "e_visa": "🟡 Электронная виза",
    "voa": "🟡 Виза по прилёте",
    "embassy": "🟠 Виза в консульстве",
    "hard": "🔴 Виза заранее, для граждан РФ сложно",
}

# Сколько дней реально нужно на оформление, с запасом
LEAD_TIME_DAYS = {
    "visa_free": 0, "voa": 0, "eta": 5, "e_visa": 10, "embassy": 30, "hard": 60,
}


@dataclass
class VisaVerdict:
    country: str
    name_ru: str
    regime: str
    ok_for_dates: bool
    ok_for_duration: bool
    stay_days: int | None
    cost_rub: int
    lead_days: int
    blockers: list[str]
    warnings: list[str]
    text: str
    link: str = ""

    @property
    def is_blocking(self) -> bool:
        return bool(self.blockers)

    @property
    def short(self) -> str:
        """Одна строка для короткой карточки."""
        icon = {"visa_free": "🟢", "eta": "🟡", "e_visa": "🟡", "voa": "🟡",
                "embassy": "🟠", "hard": "🔴"}.get(self.regime, "❓")
        if self.regime == "visa_free":
            # Для Беларуси и подобных срок условный — не морочим голову цифрой
            if not self.stay_days or self.stay_days >= 365:
                body = "виза не нужна, живи сколько хочешь"
            else:
                body = f"виза не нужна, до {self.stay_days} дней"
        elif self.regime == "voa":
            body = "виза по прилёте"
            if self.cost_rub:
                body += f", ≈ {self.cost_rub:,} ₽".replace(",", " ")
        elif self.regime in ("eta", "e_visa"):
            body = "электронная виза онлайн"
            if self.cost_rub:
                body += f", ≈ {self.cost_rub:,} ₽".replace(",", " ")
            body += f", ~{self.lead_days} дн."
        else:
            body = f"виза заранее, ≈ {self.cost_rub:,} ₽".replace(",", " ")
            body += f", ~{self.lead_days} дн."
        if self.blockers:
            body += " — не успеваешь"
        return f"{icon} {body}"


class VisaAdvisor:
    def __init__(self, config) -> None:
        self.cfg = config
        self.db: dict = config.visas

    def _entry(self, country: str) -> dict | None:
        raw = self.db.get(country.upper())
        if not raw:
            return None
        if raw.get("inherit"):
            parent = dict(self.db.get(raw["inherit"], {}))
            parent.update({k: v for k, v in raw.items() if k != "inherit"})
            parent["_inherited_from"] = raw["inherit"]
            return parent
        return raw

    def has_visa_already(self, country: str) -> bool:
        entry = self._entry(country) or {}
        schengen = entry.get("_inherited_from") == "SCHENGEN" or country.upper() == "SCHENGEN"
        traveller = self.cfg.get("traveller", {}) or {}
        if schengen and traveller.get("has_schengen_visa"):
            return True
        if country.upper() == "US" and traveller.get("has_us_visa"):
            return True
        if country.upper() == "GB" and traveller.get("has_uk_visa"):
            return True
        return False

    # ------------------------------------------------------------------
    def assess(self, country: str, depart: date, nights: int) -> VisaVerdict:
        entry = self._entry(country)
        if not entry:
            return VisaVerdict(
                country=country, name_ru=country, regime="unknown", ok_for_dates=True,
                ok_for_duration=True, stay_days=None, cost_rub=0, lead_days=0,
                blockers=[], warnings=["Страны нет в визовой базе — проверь правила вручную."],
                text=f"❓ <b>{esc(country)}</b> — данных по визе нет, уточни на сайте консульства.",
            )

        regime = entry.get("regime", "unknown")
        name = entry.get("name_ru", country)
        stay = entry.get("stay_days")
        cost = int(entry.get("cost_rub") or 0)
        lead = LEAD_TIME_DAYS.get(regime, 14)
        days_until = (depart - date.today()).days
        already = self.has_visa_already(country)

        blockers: list[str] = []
        warnings: list[str] = []

        if already:
            lead = 0
            warnings.append("У тебя уже есть действующая виза этой категории — оформлять ничего не нужно.")
        elif regime in ("embassy", "hard") and days_until < lead:
            blockers.append(
                f"До вылета {days_word(days_until)}, а на визу нужно минимум {days_word(lead)}. "
                "Без уже открытой визы билет брать нельзя."
            )
        elif days_until < lead:
            warnings.append(
                f"Оформление занимает ~{days_word(lead)}, до вылета {days_word(days_until)} — "
                "подавайся сразу после покупки."
            )

        ok_duration = True
        if stay and nights + 1 > stay:
            ok_duration = False
            long_stay = entry.get("long_stay")
            if long_stay and long_stay != "—":
                warnings.append(
                    f"Поездка на {nights} ноч. длиннее безвизовых {stay} дней. Варианты: {long_stay}"
                )
            else:
                blockers.append(
                    f"Поездка ({nights} ноч.) не помещается в разрешённые {stay} дней, "
                    "а способа продлить у страны нет."
                )

        passport_until = (self.cfg.get("traveller", {}) or {}).get("passport_valid_until")
        if passport_until:
            try:
                pv = date.fromisoformat(str(passport_until))
                ret = depart.toordinal() + nights
                months6 = date.fromordinal(ret).replace(day=1)
                need_until = date(months6.year + (months6.month + 5) // 12,
                                  (months6.month + 5) % 12 + 1, 1)
                if pv < need_until:
                    warnings.append(
                        f"Загранпаспорт действует до {pv:%d.%m.%Y} — многие страны требуют "
                        "запас 6 месяцев после возвращения. Проверь."
                    )
            except (ValueError, TypeError):
                pass

        if entry.get("risk") in ("high", "very_high"):
            warnings.append("Направление с высоким визовым риском — заранее оцени шансы на отказ.")

        return VisaVerdict(
            country=country.upper(), name_ru=name, regime=regime,
            ok_for_dates=not blockers, ok_for_duration=ok_duration,
            stay_days=stay, cost_rub=cost, lead_days=lead,
            blockers=blockers, warnings=warnings,
            text=self.render(entry, name, regime, depart, nights, blockers, warnings),
            link=entry.get("link", ""),
        )

    # ------------------------------------------------------------------
    def render(self, entry: dict, name: str, regime: str, depart: date, nights: int,
               blockers: list[str], warnings: list[str]) -> str:
        lines = [f"🛂 <b>Виза: {esc(name)}</b>", REGIME_LABEL.get(regime, "❓ Режим неизвестен")]

        stay = entry.get("stay_days")
        if stay:
            period = entry.get("period_days")
            limit = f"до {stay} дней"
            if period:
                limit += f" в течение {period} дней"
            fits = "✓ поездка помещается" if nights + 1 <= stay else "✗ поездка длиннее лимита"
            lines.append(f"• Разрешённое пребывание: {limit} — {fits}")

        processing = entry.get("processing")
        if processing and processing != "—":
            lines.append(f"• Оформление: {esc(processing)}")

        cost = int(entry.get("cost_rub") or 0)
        lines.append(f"• Стоимость: {'бесплатно' if not cost else '≈ ' + fmt_money(cost)}")

        docs = entry.get("docs") or []
        if docs:
            lines.append("• Документы: " + esc(", ".join(docs)))

        long_stay = entry.get("long_stay")
        if long_stay and long_stay != "—":
            lines.append(f"• Если захочется остаться дольше: {esc(long_stay)}")

        payments = entry.get("payments")
        if payments:
            lines.append(f"💳 <b>Деньги на месте:</b> {esc(payments)}")

        notes = entry.get("notes")
        if notes:
            lines.append(f"ℹ️ {esc(notes)}")

        for b in blockers:
            lines.append(f"⛔️ {esc(b)}")
        for w in warnings:
            lines.append(f"⚠️ {esc(w)}")

        link = entry.get("link")
        if link:
            lines.append(f'🔗 <a href="{esc(link)}">Официальный источник</a>')

        return "\n".join(lines)

    # ------------------------------------------------------------------
    def can_stay_long(self, country: str) -> bool:
        """Есть ли легальный путь прожить в стране месяцы, а не недели."""
        entry = self._entry(country)
        if not entry:
            return False
        if (entry.get("stay_days") or 0) >= 90:
            return True
        long_stay = entry.get("long_stay") or ""
        return bool(long_stay) and long_stay != "—"

    def total_visa_cost(self, countries: list[str]) -> int:
        seen, total = set(), 0
        for c in countries:
            if c in seen:
                continue
            seen.add(c)
            entry = self._entry(c) or {}
            if not self.has_visa_already(c):
                total += int(entry.get("cost_rub") or 0)
        return total
