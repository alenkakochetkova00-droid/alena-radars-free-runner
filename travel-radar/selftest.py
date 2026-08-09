#!/usr/bin/env python3
"""Самопроверка без сети: логика форматов поездки, виз, бюджета и вёрстки сообщения.

Запуск:  ./.venv/bin/python selftest.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.baseline import BaselineEngine          # noqa: E402
from radar.config import get_config                # noqa: E402
from radar.flights import FlightOffer              # noqa: E402
from radar.planner import (  # noqa: E402
    Planner, classify_shape, compare_links, render_card, render_plan,
)
from radar.rails import TrainOffer                 # noqa: E402
from radar.storage import Storage                  # noqa: E402
from radar.visa import VisaAdvisor                 # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'✓' if condition else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def next_weekday(target: int, weeks_ahead: int = 6) -> date:
    d = date.today() + timedelta(weeks=weeks_ahead)
    return d + timedelta(days=(target - d.weekday()) % 7)


def main() -> int:
    cfg = get_config()
    db = Storage(Path(__file__).parent / "selftest.sqlite3")
    visa = VisaAdvisor(cfg)
    planner = Planner(cfg, visa)
    engine = BaselineEngine(db, cfg)

    print("\n1. Форматы поездки")
    thu = next_weekday(3)
    sun = thu + timedelta(days=3)
    shape = classify_shape(datetime.combine(thu, datetime.min.time().replace(hour=19)),
                           datetime.combine(sun, datetime.min.time().replace(hour=21)), cfg)
    check("четверг 19:00 → воскресенье 21:00 = длинные выходные",
          shape is not None and shape.key == "weekend",
          shape.label if shape else "не распознано")

    bad = classify_shape(datetime.combine(thu, datetime.min.time().replace(hour=8)),
                         datetime.combine(thu + timedelta(days=2),
                                          datetime.min.time().replace(hour=10)), cfg)
    check("четверг утром → суббота утром отбрасывается", bad is None,
          "ок" if bad is None else bad.label)

    mon = next_weekday(0)
    vac = classify_shape(datetime.combine(mon, datetime.min.time().replace(hour=10)),
                         datetime.combine(mon + timedelta(days=7),
                                          datetime.min.time().replace(hour=10)), cfg)
    check("7 ночей = стандартный отпуск", vac is not None and vac.key == "vacation",
          f"{vac.label}, отпуска {vac.vacation_days} дн." if vac else "нет")

    long_stay = classify_shape(datetime.combine(mon, datetime.min.time()),
                               datetime.combine(mon + timedelta(days=90), datetime.min.time()), cfg)
    check("90 ночей = зимовка", long_stay is not None and long_stay.key == "long_stay",
          long_stay.label if long_stay else "нет")

    threshold = int(float(cfg.get("flights.min_discount", 0.7)) * 100)
    print(f"\n2. Детектор аномалий (порог {threshold} %)")
    v = engine.judge_flight("MOW", "BKK", 18000, km=7050, country="TH")
    check("Бангкок за 18 000 ₽ — находка", v.is_deal, f"{v.discount_pct} % от {int(v.normal)} ₽")
    v2 = engine.judge_flight("MOW", "BKK", 26400, km=7050, country="TH")
    check(f"Бангкок за 26 400 ₽ (−59 %) — не проходит порог {threshold} %",
          not v2.is_deal, v2.reason)
    v3 = engine.judge_flight("MOW", "EZE", 45000, km=13500, country="AR")
    check("Буэнос-Айрес за 45 000 ₽ вместо 160 000 — находка",
          v3.is_deal, f"{v3.discount_pct} %")
    v4 = engine.judge_flight("MOW", "EZE", 95000, km=13500, country="AR")
    check("Буэнос-Айрес за 95 000 ₽ — не находка", not v4.is_deal, v4.reason)
    check("санитарный потолок ближней дальности",
          engine.judge_flight("MOW", "IST", 7000, km=1750, country="TR").is_deal
          and not engine.judge_flight("MOW", "IST", 24000, km=1750, country="TR").is_deal,
          "Стамбул: 7 000 ✓ / 24 000 ✗")
    v5 = engine.judge_flight("MOW", "GOI", 17000, km=5100, country="IN")
    check("Гоа за 17 000 ₽ вместо 60 000 — находка", v5.is_deal, f"{v5.discount_pct} %")

    cfg.overrides["flights.max_ticket_price"] = 15000
    v6 = engine.judge_flight("MOW", "GOI", 17000, km=5100, country="IN")
    check("твёрдый потолок /maxprice перебивает всё", not v6.is_deal, v6.reason)
    cfg.overrides.pop("flights.max_ticket_price")

    db.record("flight", "MOW", "ZZZ", 5000)
    normal, conf = engine.normal_flight_price("MOW", "ZZZ")
    check("одно наблюдение не становится «нормой»", normal is None,
          f"норма={normal}, источник={conf}")

    v7 = engine.judge_flight("MOW", "TBW", 7755, km=400, country="RU")
    check("дешёвый рейс без нормы не считается находкой", not v7.is_deal, v7.reason)

    print("\n3. Визы")
    th = visa.assess("TH", date.today() + timedelta(days=40), 7)
    check("Таиланд на 7 ночей — без визы", th.regime == "visa_free" and not th.blockers)
    th_long = visa.assess("TH", date.today() + timedelta(days=40), 120)
    check("Таиланд на 120 ночей — предупреждение о продлении",
          bool(th_long.warnings), th_long.warnings[0][:60] if th_long.warnings else "")
    schengen = visa.assess("IT", date.today() + timedelta(days=20), 5)
    check("Италия через 20 дней — блокер по срокам визы",
          bool(schengen.blockers), schengen.blockers[0][:70] if schengen.blockers else "нет")
    schengen_far = visa.assess("IT", date.today() + timedelta(days=120), 5)
    check("Италия через 120 дней — проходит с инструкцией", not schengen_far.blockers)
    check("наследование шенгена работает", "Шенген" in schengen.text or "виза" in schengen.text.lower())
    check("зимовка возможна в Грузии", visa.can_stay_long("GE"))
    check("зимовка невозможна в Гонконге", not visa.can_stay_long("HK"))

    print("\n4. Полный маршрут")
    offer = FlightOffer(
        origin="MOW", dest="BKK", price=18000,
        depart_at=datetime.combine(next_weekday(0), datetime.min.time().replace(hour=13, minute=40)),
        return_at=datetime.combine(next_weekday(0) + timedelta(days=7),
                                   datetime.min.time().replace(hour=23, minute=10)),
        airline="TK", flight_number="1234", transfers=1, return_transfers=1,
        duration_to=810, duration_back=790, distance_km=7050,
    )
    shape = classify_shape(offer.depart_at, offer.return_at, cfg)
    verdict = engine.judge_flight("MOW", "BKK", offer.price, 7050, "TH")
    plan = planner.plan_flight(offer, verdict, shape)
    text = render_plan(plan)
    check("маршрут собрался", len(plan.blocks) >= 6, f"блоков: {len(plan.blocks)}")
    check("бюджет посчитан", plan.budget.total > offer.price,
          f"итого {int(plan.budget.total)} ₽")
    check("сообщение помещается в Telegram", len(text) < 8000, f"{len(text)} символов")
    check("нет незакрытых HTML-тегов",
          text.count("<b>") == text.count("</b>") and text.count("<a ") == text.count("</a>"))

    print("\n4б. Короткая карточка")
    card = render_card(plan)
    check("название маршрута читаемое", "Москва → Бангкок → Москва" in card,
          card.split("\n")[0])
    check("карточка компактная", len(card) < 1300, f"{len(card)} символов")
    check("в карточке есть цена, жильё и итог",
          "Билеты" in card and "Жильё" in card and "Вся поездка" in card)
    check("в карточке есть виза одной строкой", "виза не нужна" in card)
    banned = ("booking.com", "ostrovok", "airbnb")
    check("нет ссылок на агрегаторы жилья",
          not any(b in card.lower() for b in banned)
          and not any(b in text.lower() for b in banned))
    check("цена жилья — уровень trip.com, а не эконом",
          cfg.nice_hotel_price("BKK") > cfg.city("BKK").get("hotel", 0),
          f"{cfg.nice_hotel_price('BKK')} ₽ против эконома "
          f"{cfg.city('BKK').get('hotel')} ₽")
    check("нулевая скидка не показывается",
          "на 0 %" not in card and "на 0 %" not in text)

    print("\n4в. Удобство рейса")
    from radar.comfort import ComfortJudge, season_verdict  # noqa: PLC0415
    judge = ComfortJudge(cfg)

    def mk(dest="BKK", hour=13, stops=0, airline="TK", dur=None, km=7050, ret_hour=20):
        d = datetime.combine(next_weekday(0), datetime.min.time().replace(hour=hour))
        return FlightOffer(
            origin="MOW", dest=dest, price=18000, depart_at=d,
            return_at=d + timedelta(days=7, hours=ret_hour - hour),
            airline=airline, transfers=stops, return_transfers=stops,
            duration_to=dur if dur is not None else 600, duration_back=600, distance_km=km)


    check("прямой рейс проходит", judge.judge(mk()).ok)
    night = judge.judge(mk(hour=4))
    check("вылет в 4 утра отбрасывается", not night.ok,
          night.blockers[0] if night.blockers else "")
    ssj = judge.judge(mk(airline="A4"))
    check("рейс только на Superjet отбрасывается", not ssj.ok,
          ssj.blockers[0] if ssj.blockers else "")
    risk = judge.judge(mk(airline="SU"))
    check("Superjet-риск помечен, но не блокирует",
          risk.ok and any("Superjet" in n for n in risk.notes))
    direct_minutes = judge and 0
    from radar.comfort import estimate_flight_minutes  # noqa: PLC0415
    direct_minutes = estimate_flight_minutes(7050)
    check("оценка лётного времени похожа на правду",
          520 <= direct_minutes <= 600,
          f"Москва–Бангкок ≈ {direct_minutes / 60:.1f} ч, реально 9 ч 20 мин")

    walk = judge.judge(mk(stops=1, dur=1300))
    check("длинная стыковка = мини-остановка в городе",
          walk.layover_kind == "city_walk", walk.summary)
    awkward = judge.judge(mk(stops=1, dur=1100))
    check("неудобная стыковка помечена", awkward.layover_kind == "awkward", awkward.summary)
    short = judge.judge(mk(stops=1, dur=850))
    check("короткая стыковка помечена", short.layover_kind == "short", short.summary)
    check("багаж определяется по авиакомпании",
          judge.judge(mk(airline="TK")).checked_bag == "included"
          and judge.judge(mk(airline="DP")).checked_bag == "extra",
          "Turkish: включён / Победа: за доплату")
    lowcost = judge.judge(mk(airline="PC"))
    check("ручная кладь есть у лоукостера, багажа нет",
          lowcost.cabin_bag == "included" and lowcost.checked_bag == "extra")

    pobeda_stay = judge.judge(mk(airline="DP"), shape_key="long_stay")
    check("на длительное проживание рейс без багажа отбрасывается",
          not pobeda_stay.ok,
          pobeda_stay.blockers[0] if pobeda_stay.blockers else "")
    pobeda_weekend = judge.judge(mk(airline="DP"), shape_key="weekend")
    check("на выходные тот же рейс без багажа проходит", pobeda_weekend.ok)
    turkish_stay = judge.judge(mk(airline="TK"), shape_key="long_stay")
    check("рейс с включённым багажом проходит на длительное проживание",
          turkish_stay.ok)

    ok_july, _ = season_verdict(cfg.city("DXB"), 7)
    ok_jan, _ = season_verdict(cfg.city("DXB"), 1)
    check("Дубай в июле — не сезон, в январе — сезон", not ok_july and ok_jan)

    print("\n4г. Названия и площадки")
    check("BOM превращается в Мумбаи", cfg.city_name("BOM") == "Мумбаи", cfg.city_name("BOM"))
    check("RTW превращается в Саратов", cfg.city_name("RTW") == "Саратов")
    check("код авиакомпании превращается в название",
          cfg.airline_name("TK") == "Turkish Airlines", cfg.airline_name("TK"))
    links = dict(compare_links("MOW", "BKK", date(2026, 9, 21), date(2026, 9, 28)))
    check("четыре площадки для сравнения цены", len(links) == 4, ", ".join(links))
    check("ссылки ведут на разные сайты", len({u.split("/")[2] for u in links.values()}) == 4)

    print("\n4д. Категория поездки жирным")
    for key, expect in (("weekend", "ТРИП ВЫХОДНОГО ДНЯ"),
                        ("vacation", "СТАНДАРТНЫЙ ОТПУСК"),
                        ("long_stay", "ДЛИТЕЛЬНОЕ ПРОЖИВАНИЕ")):
        label = (cfg.get(f"trip_shapes.{key}", {}) or {}).get("label", "")
        check(f"категория «{expect.lower()}» названа по-человечески",
              label.upper() == expect, label)
    check("категория выделена жирным в карточке",
          card.startswith("🔥") and "<b>СТАНДАРТНЫЙ ОТПУСК</b>" in card)

    print("\n4е. Сложный маршрут")
    base = next_weekday(0)

    def leg(o, d, day, price, dur=600):
        dt = datetime.combine(base + timedelta(days=day),
                              datetime.min.time().replace(hour=11))
        return FlightOffer(origin=o, dest=d, price=price, depart_at=dt,
                           return_at=None, duration_to=dur, transfers=0)

    legs = [leg("MOW", "EZE", 0, 52000), leg("EZE", "SCL", 7, 12000),
            leg("SCL", "MOW", 14, 58000)]
    separate = 160000 + 175000          # два отдельных перелёта туда-обратно
    mplan = planner.plan_multicity(legs, [], sum(x.price for x in legs), separate)
    mcard = render_card(mplan)
    check("маршрут показан целиком с Москвой на обоих концах",
          "Москва → Буэнос-Айрес → Сантьяго → Москва" in mcard, mcard.split("\n")[0])
    check("категория помечена", "<b>СЛОЖНЫЙ МАРШРУТ</b>" in mcard)
    check("все три перелёта перечислены",
          all(f"{i}." in mcard for i in (1, 2, 3)),
          f"{len(legs)} перелёта")
    check("выгода посчитана от двух отдельных поездок",
          "экономия" in mcard and "%" in mcard,
          [ln for ln in mcard.split("\n") if "экономия" in ln][:1])
    check("виза по каждой стране в карточке",
          "Буэнос-Айрес:" in mcard and "Сантьяго:" in mcard)

    print("\n4ж. Потолок бюджета")
    caps = cfg.get("budget_caps", {}) or {}
    check("потолок выходных 50 000", caps.get("weekend") == 50000, str(caps.get("weekend")))
    check("потолок отпуска 100 000", caps.get("vacation") == 100000, str(caps.get("vacation")))

    class FakeDb:
        def setting(self, *a, **k):
            return {}

    from radar.scanner import Scanner  # noqa: PLC0415

    class Probe(Scanner):
        def __init__(self, cfg):
            self.cfg = cfg

    probe = Probe(cfg)
    total_label = f"{int(plan.budget.total)} ₽"
    check("отпуск за 87 296 ₽ проходит потолок 100 000",
          probe._within_budget(plan, "vacation"), total_label)
    check("он же не проходит как выходные (потолок 50 000)",
          not probe._within_budget(plan, "weekend"), total_label)
    check("сложный маршрут за 369 000 ₽ отбрасывается потолком 200 000",
          not probe._within_budget(mplan, "multi_city"),
          f"{int(mplan.budget.total)} ₽")

    print("\n5. Поезд")
    train = TrainOffer(
        origin="Москва", dest="Казань", depart_date=date.today() + timedelta(days=14),
        number="002Й", brand="ПРЕМИУМ", depart_time="23:55", arrive_time="09:10",
        duration="9 ч 15 мин", from_station="МОСКВА КАЗ", to_station="КАЗАНЬ ПАСС",
        klass="Плацкарт", price=1300, free_seats=8,
    )
    tv = engine.judge_train("Москва", "Казань", "Плацкарт", 1300, 800,
                            {"base_plaz": 2800, "base_kupe": 5200})
    check("плацкарт за 1300 вместо 2800 — находка", tv.is_deal, f"{tv.discount_pct} %")
    tplan = planner.plan_train(train, tv, None, 2, {"km": 800, "base_plaz": 2800})
    ttext = render_plan(tplan)
    tcard = render_card(tplan)
    check("маршрут по поезду собрался", len(ttext) > 300, f"{len(ttext)} символов")
    check("ночной поезд отмечен как бонус", "Ночной поезд" in ttext)
    check("карточка поезда короткая и с ценой", len(tcard) < 700 and "Билет" in tcard,
          f"{len(tcard)} символов")
    check("предупреждение о малом числе мест", "Мест осталось" in tcard)

    print("\n" + "=" * 60)
    if FAILED:
        print(f"ПРОВАЛЕНО {len(FAILED)}: " + ", ".join(FAILED))
        return 1
    print("Все проверки пройдены.")
    print("=" * 60)

    def plain(s: str) -> str:
        for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>", "<s>", "</s>"):
            s = s.replace(tag, "")
        return s

    print("\n--- ТАК ВЫГЛЯДИТ УВЕДОМЛЕНИЕ ---\n")
    print(plain(card))
    print("\n--- А ТАК — /details ---\n")
    print(plain(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
