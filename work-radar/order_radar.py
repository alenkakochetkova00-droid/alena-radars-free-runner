#!/usr/bin/env python3
"""Личный Telegram-радар публичных фриланс-заказов.

Работает только с открытыми страницами и опубликованными заказчиком контактами.
Сторонние зависимости не требуются.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
MOSCOW = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("order-radar")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def absolute_url(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, html.unescape(href or ""))


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36 OrderRadar/1.0"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


@dataclass
class Listing:
    source: str
    external_id: str
    title: str
    description: str
    url: str
    published_at: datetime
    budget_rub: int | None = None
    contacts: list[str] = field(default_factory=list)
    responses: int | None = None
    raw_links: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        raw = f"{self.source}|{self.external_id}|{self.url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class Assessment:
    category: str
    quantity: int
    market_price_rub: int
    offer_price_rub: int
    effort_hours: float
    hourly_rub: int
    budget_ratio: float
    age_minutes: int
    score: int
    reasons: list[str]
    reply: str


class TextExtractor(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        attrs_d = dict(attrs)
        if tag == "a" and attrs_d.get("href"):
            self.links.append(absolute_url(self.base_url, attrs_d["href"] or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and data.strip():
            self.parts.append(data.strip())

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts))


class TelegramPostParser(HTMLParser):
    def __init__(self, channel: str) -> None:
        super().__init__(convert_charrefs=True)
        self.channel = channel.lstrip("@").lower()
        self.posts: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        classes = (attrs_d.get("class") or "").split()
        if tag == "div" and "tgme_widget_message_wrap" in classes and self.current is None:
            self.current = {"text": [], "links": [], "post": "", "datetime": ""}
            self.depth = 1
            return
        if self.current is None:
            return
        if tag == "div":
            self.depth += 1
        if attrs_d.get("data-post"):
            self.current["post"] = attrs_d["data-post"]
        if tag == "time" and attrs_d.get("datetime"):
            self.current["datetime"] = attrs_d["datetime"]
        if tag == "a" and attrs_d.get("href"):
            self.current["links"].append(html.unescape(attrs_d["href"] or ""))

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or tag != "div":
            return
        self.depth -= 1
        if self.depth == 0:
            self.posts.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None and data.strip():
            self.current["text"].append(data.strip())


class WeblancerParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.articles: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.depth = 0
        self.in_h2 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "article" and self.current is None:
            self.current = {"text": [], "title": [], "url": "", "links": []}
            self.depth = 1
            return
        if self.current is None:
            return
        if tag == "article":
            self.depth += 1
        if tag == "h2":
            self.in_h2 = True
        if tag == "a" and attrs_d.get("href"):
            link = absolute_url(self.base_url, attrs_d["href"] or "")
            self.current["links"].append(link)
            if self.in_h2 and not self.current["url"]:
                self.current["url"] = link

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "h2":
            self.in_h2 = False
        if tag == "article":
            self.depth -= 1
            if self.depth == 0:
                self.articles.append(self.current)
                self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is None or not data.strip():
            return
        self.current["text"].append(data.strip())
        if self.in_h2:
            self.current["title"].append(data.strip())


def parse_datetime(value: str, default_now: datetime | None = None) -> datetime:
    now = default_now or datetime.now(MOSCOW)
    value = clean_text(value)
    if not value:
        return now
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(MOSCOW)
    except ValueError:
        pass
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+в?\s*(\d{1,2}):(\d{2}))?", value)
    if match:
        day, month, year = map(int, match.group(1, 2, 3))
        hour = int(match.group(4) or min(now.hour, 12))
        minute = int(match.group(5) or 0)
        parsed = datetime(year, month, day, hour, minute, tzinfo=MOSCOW)
        return min(parsed, now)
    return now


def parse_number(raw: str) -> float:
    value = raw.lower().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    multiplier = 1
    if value.endswith(("k", "к")):
        multiplier = 1000
        value = value[:-1]
    try:
        return float(value) * multiplier
    except ValueError:
        return 0


def parse_budget_rub(text: str, usd_rate: float = 80.0) -> int | None:
    text = clean_text(text).lower()
    rub_patterns = [
        r"(?:бюджет|оплата|стоимость|цена|вознаграждение)?\s*(?:от|до)?\s*((?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?(?:\s*(?:к|k))?)\s*(?:₽|руб(?:лей|ля|ль)?|р\.)",
        r"(\d+(?:[.,]\d+)?\s*(?:к|k))\b",
    ]
    values: list[int] = []
    for pattern in rub_patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            number = parse_number(match.group(1))
            if number >= 300:
                values.append(int(number))
    number_pattern = r"(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?"
    for match in re.finditer(rf"(?:\$|usd\s*)({number_pattern})|({number_pattern})\s*(?:\$|usd)", text, flags=re.I):
        raw = match.group(1) or match.group(2)
        number = parse_number(raw)
        if number > 0:
            values.append(int(number * usd_rate))
    return max(values) if values else None


def extract_contacts(
    text: str,
    links: Iterable[str],
    source_channel: str = "",
    extra_excluded: Iterable[str] = (),
) -> list[str]:
    source_channel = source_channel.lower().lstrip("@")
    found: list[str] = []
    excluded = {
        source_channel,
        "share",
        "telegram",
        "durov",
        "designwork_vacansii",
        "digitaltender",
        "freelancechoice",
        "zakaz_design",
        "getclient",
        "design_freevacancies",
        "weblancer_net",
        "freelancejobru",
        "keyframes",
        "context",
    }
    excluded.update(item.lower().lstrip("@") for item in extra_excluded)
    for handle in re.findall(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})", text):
        if handle.lower() not in excluded:
            found.append("@" + handle)
    for link in links:
        parsed = urllib.parse.urlparse(link if "://" in link else "https:" + link)
        if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"}:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] in {"s", "joinchat", "+"}:
            continue
        handle = parts[0].lstrip("+").lower()
        if handle and handle not in excluded and not handle.isdigit():
            found.append("@" + parts[0].lstrip("+"))
    for email in re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, flags=re.I):
        found.append(email)
    for phone in re.findall(r"(?<!\d)(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)", text):
        found.append(clean_text(phone))
    return list(dict.fromkeys(found))


def extract_responses(text: str) -> int | None:
    match = re.search(r"(\d+)\s+(?:заяв|ответ)", text, flags=re.I)
    return int(match.group(1)) if match else None


def parse_telegram_source(source: dict[str, Any], cfg: dict[str, Any]) -> list[Listing]:
    channel = source["channel"].lstrip("@")
    url = f"https://t.me/s/{channel}"
    parser = TelegramPostParser(channel)
    parser.feed(fetch_url(url))
    listings: list[Listing] = []
    for post in parser.posts:
        body = clean_text(" ".join(post["text"]))
        post_path = post.get("post") or ""
        if not body or not post_path:
            continue
        post_url = f"https://t.me/{post_path}"
        title = body[:110].rsplit(" ", 1)[0] if len(body) > 110 else body
        contacts = extract_contacts(body, post["links"], channel, cfg.get("ignored_contact_handles", []))
        listings.append(
            Listing(
                source=f"Telegram · @{channel}",
                external_id=post_path,
                title=title,
                description=body,
                url=post_url,
                published_at=parse_datetime(post.get("datetime", "")),
                budget_rub=parse_budget_rub(body, cfg["usd_rate_rub"]),
                contacts=contacts,
                raw_links=post["links"],
            )
        )
    return listings


def parse_weblancer_source(source: dict[str, Any], cfg: dict[str, Any]) -> list[Listing]:
    url = source["url"]
    parser = WeblancerParser(url)
    parser.feed(fetch_url(url))
    listings: list[Listing] = []
    for article in parser.articles:
        body = clean_text(" ".join(article["text"]))
        item_url = article["url"]
        if not item_url:
            continue
        external_id = item_url.rstrip("/").split("-")[-1]
        listings.append(
            Listing(
                source="Weblancer",
                external_id=external_id,
                title=clean_text(" ".join(article["title"])) or body[:100],
                description=body,
                url=item_url,
                published_at=parse_datetime(body),
                budget_rub=parse_budget_rub(body, cfg["usd_rate_rub"]),
                contacts=extract_contacts(body, article["links"], extra_excluded=cfg.get("ignored_contact_handles", [])),
                responses=extract_responses(body),
                raw_links=article["links"],
            )
        )
    return listings


def strip_tags(fragment: str) -> str:
    parser = TextExtractor()
    parser.feed(fragment)
    return parser.text


def parse_freelancejob_source(source: dict[str, Any], cfg: dict[str, Any]) -> list[Listing]:
    url = source["url"]
    page = fetch_url(url)
    link_re = re.compile(
        r'<a[^>]+href=["\'](?P<href>/vacancy/(?P<id>\d+)/)["\'][^>]*>(?P<title>.*?)</a>',
        flags=re.I | re.S,
    )
    matches = list(link_re.finditer(page))
    listings: list[Listing] = []
    used: set[str] = set()
    for index, match in enumerate(matches):
        item_id = match.group("id")
        title = strip_tags(match.group("title"))
        if item_id in used or not title or title.isdigit():
            continue
        used.add(item_id)
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page), match.end() + 8000)
        body = clean_text(title + " " + strip_tags(page[match.end():end]))
        item_url = absolute_url(url, match.group("href"))
        listings.append(
            Listing(
                source="FreelanceJob",
                external_id=item_id,
                title=title,
                description=body,
                url=item_url,
                published_at=parse_datetime(body),
                budget_rub=parse_budget_rub(body, cfg["usd_rate_rub"]),
                contacts=extract_contacts(body, [], extra_excluded=cfg.get("ignored_contact_handles", [])),
                responses=extract_responses(body),
            )
        )
    return listings


def enrich_listing(listing: Listing, cfg: dict[str, Any]) -> Listing:
    if listing.source.startswith("Telegram") or listing.contacts:
        return listing
    try:
        page = fetch_url(listing.url)
        parser = TextExtractor(listing.url)
        parser.feed(page)
        contacts = extract_contacts(parser.text, parser.links, extra_excluded=cfg.get("ignored_contact_handles", []))
        if contacts:
            listing.contacts = contacts
        if len(parser.text) > len(listing.description):
            listing.description = parser.text[:8000]
        listing.budget_rub = listing.budget_rub or parse_budget_rub(parser.text, cfg["usd_rate_rub"])
    except Exception as exc:
        LOG.debug("Не удалось открыть карточку %s: %s", listing.url, exc)
    return listing


def classify(text: str, categories: dict[str, Any]) -> tuple[str, int]:
    lowered = text.lower()
    best_category = "other"
    best_hits = 0
    for name, settings in categories.items():
        hits = sum(1 for keyword in settings["keywords"] if keyword.lower() in lowered)
        if hits > best_hits:
            best_category, best_hits = name, hits
    return best_category, best_hits


def extract_quantity(text: str, category: str) -> int:
    nouns = {
        "marketplace": r"карточ\w*|слайд\w*|товар\w*",
        "banner": r"баннер\w*|креатив\w*|облож\w*",
        "text": r"пост\w*|стат\w*|текст\w*",
        "tilda": r"страниц\w*|лендинг\w*|сайт\w*",
    }.get(category, r"вариант\w*|макет\w*")
    matches = re.findall(rf"(\d{{1,3}})\s*(?:{nouns})", text, flags=re.I)
    sensible = [int(value) for value in matches if 1 <= int(value) <= 100]
    return max(sensible) if sensible else 1


def rounded_price(value: float) -> int:
    if value < 3000:
        return max(500, int(round(value / 100) * 100))
    return max(1000, int(round(value / 500) * 500))


def make_reply(listing: Listing, category: str, quantity: int, offer: int, effort: float, cfg: dict[str, Any]) -> str:
    due = "сегодня" if effort <= 6 else f"за {max(1, round(effort / 6))} рабочих дня"
    tg = cfg.get("profile", {}).get("telegram", "")
    templates = {
        "marketplace": (
            "Здравствуйте! Готова сделать {quantity} карточек/слайдов для маркетплейса. "
            "Сначала соберу один главный слайд и пришлю на согласование, затем быстро адаптирую "
            "утверждённый стиль на весь комплект. Проверю читаемость на мобильном экране и подготовлю "
            "файлы в нужном размере. Стоимость — {offer} ₽, срок — {due}. Могу приступить сейчас."
        ),
        "banner": (
            "Здравствуйте! Готова выполнить задачу с баннерами прямо сейчас. Аккуратно соберу композицию, "
            "проверю читаемость и адаптирую макеты под нужные форматы. Первый вариант покажу в течение часа. "
            "Стоимость — {offer} ₽, весь комплект подготовлю {due}."
        ),
        "logo_packaging": (
            "Здравствуйте! Готова разработать визуальную концепцию по вашему заданию. Сначала предложу "
            "2 направления, после выбора доработаю сильнейшее и подготовлю файлы для использования и печати. "
            "Стоимость — {offer} ₽, первый вариант покажу в течение суток. Могу начать сегодня."
        ),
        "tilda": (
            "Здравствуйте! Изучила задачу и готова собрать сайт на Tilda: продуманная структура, аккуратный "
            "дизайн, адаптация под телефон и подключение форм. До начала зафиксируем состав блоков, чтобы цена "
            "не менялась в процессе. Стоимость — {offer} ₽, срок — {due}. Могу приступить сегодня."
        ),
        "website": (
            "Здравствуйте! Готова выполнить задачу по сайту. Начну со структуры и первого экрана, затем соберу "
            "остальные разделы, мобильную версию и проверю формы. Стоимость — {offer} ₽, срок — {due}. "
            "Могу сегодня уточнить материалы и сразу начать работу."
        ),
        "text": (
            "Здравствуйте! Готова подготовить {quantity} материалов по вашему заданию. Сначала пришлю один "
            "вариант на согласование тона, затем сделаю весь комплект в едином стиле. Стоимость — {offer} ₽, "
            "срок — {due}. Могу начать сейчас."
        ),
        "other": (
            "Здравствуйте! Внимательно прочитала задачу и готова приступить сегодня. Сначала согласуем "
            "ожидаемый результат, затем я покажу первый промежуточный вариант и после подтверждения закончу "
            "работу. Стоимость — {offer} ₽, срок — {due}."
        ),
    }
    reply = templates[category].format(quantity=quantity, offer=f"{offer:,}".replace(",", " "), due=due)
    if tg:
        reply += f"\n\nTelegram: {tg}"
    return reply


def assess(listing: Listing, cfg: dict[str, Any], now: datetime | None = None) -> Assessment | None:
    now = now or datetime.now(MOSCOW)
    text = f"{listing.title} {listing.description}"
    lowered = text.lower()
    if any(word.lower() in lowered for word in cfg["exclude_keywords"]):
        return None
    category, keyword_hits = classify(text, cfg["categories"])
    if category == "other" or keyword_hits == 0:
        return None
    if cfg["filters"]["require_public_contact"] and not listing.contacts:
        return None
    if cfg["filters"]["require_budget"] and not listing.budget_rub:
        return None
    age_minutes = max(0, int((now - listing.published_at.astimezone(MOSCOW)).total_seconds() / 60))
    max_age = cfg["filters"]["max_age_minutes"]
    if age_minutes > max_age:
        return None
    rate = cfg["categories"][category]
    quantity = extract_quantity(text, category)
    market = rounded_price(rate["base_price_rub"] + max(0, quantity - 1) * rate["per_unit_rub"])
    effort = max(rate["minimum_hours"], rate["base_hours"] + max(0, quantity - 1) * rate["per_unit_hours"])
    budget = listing.budget_rub or market
    ratio = budget / max(market, 1)
    if ratio < cfg["filters"]["minimum_budget_to_market_ratio"]:
        return None
    offer = rounded_price(min(market, budget * cfg["filters"]["offer_share_of_budget"]))
    hourly = int(offer / max(effort, 0.25))
    if hourly < cfg["filters"]["minimum_effective_hourly_rub"]:
        return None
    freshness_score = max(0, 35 * (1 - age_minutes / max_age))
    value_score = min(25, 12 + max(0, ratio - 1) * 20)
    contact_score = 25 if listing.contacts else 0
    competition_score = 10 if listing.responses is None else max(0, 10 - listing.responses / 2)
    score = min(100, round(freshness_score + value_score + contact_score + competition_score + min(5, keyword_hits)))
    if score < cfg["filters"]["minimum_score"]:
        return None
    reasons = [
        f"бюджет примерно в {ratio:.1f}× выше расчётной рыночной цены" if ratio >= 1.2 else "бюджет выше минимальной рыночной планки",
        f"публичный контакт: {', '.join(listing.contacts[:2])}",
        f"эффективная ставка около {hourly:,} ₽/час".replace(",", " "),
    ]
    if listing.responses is not None and listing.responses <= 5:
        reasons.append(f"низкая конкуренция: {listing.responses} откликов")
    reply = make_reply(listing, category, quantity, offer, effort, cfg)
    return Assessment(category, quantity, market, offer, effort, hourly, ratio, age_minutes, score, reasons, reply)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT, source TEXT, title TEXT, sent INTEGER, score INTEGER)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS earnings (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, amount INTEGER)"
        )
        self.db.commit()

    def has_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, listing: Listing, sent: bool, score: int = 0) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen(key,first_seen,source,title,sent,score) VALUES(?,?,?,?,?,?)",
            (listing.key, datetime.now(timezone.utc).isoformat(), listing.source, listing.title, int(sent), score),
        )
        self.db.commit()

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def add_earning(self, amount: int) -> None:
        self.db.execute("INSERT INTO earnings(created_at,amount) VALUES(?,?)", (datetime.now(MOSCOW).isoformat(), amount))
        self.db.commit()

    def today_earnings(self) -> int:
        prefix = datetime.now(MOSCOW).date().isoformat() + "%"
        row = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM earnings WHERE created_at LIKE ?", (prefix,)).fetchone()
        return int(row[0] or 0)


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **data: Any) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(self.base + method, data=encoded)
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload

    def send(self, chat_id: str, text: str) -> None:
        self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4090],
            parse_mode="HTML",
            disable_web_page_preview="true",
        )


def format_alert(listing: Listing, assessment: Assessment) -> str:
    safe = html.escape
    budget = f"{listing.budget_rub:,} ₽".replace(",", " ") if listing.budget_rub else "не указан"
    offer = f"{assessment.offer_price_rub:,} ₽".replace(",", " ")
    market = f"{assessment.market_price_rub:,} ₽".replace(",", " ")
    contacts = " · ".join(safe(item) for item in listing.contacts[:4])
    description = listing.description[:850]
    if len(listing.description) > 850:
        description += "…"
    reasons = "\n".join(f"• {safe(reason)}" for reason in assessment.reasons)
    return (
        f"🔥 <b>Сильная заявка — {assessment.score}/100</b>\n\n"
        f"<b>{safe(listing.title[:180])}</b>\n"
        f"Источник: {safe(listing.source)} · примерно {assessment.age_minutes} мин назад\n"
        f"Бюджет заказчика: <b>{budget}</b>\n"
        f"Предложить: <b>{offer}</b> (рынок ≈ {market})\n"
        f"Оценка времени: ≈ {assessment.effort_hours:.1f} ч\n"
        f"Контакт: <b>{contacts}</b>\n\n"
        f"<b>Почему стоит писать:</b>\n{reasons}\n\n"
        f"<b>Задание:</b>\n{safe(description)}\n\n"
        f"<b>Готовый ответ:</b>\n<pre>{safe(assessment.reply)}</pre>\n"
        f'<a href="{safe(listing.url)}">Открыть оригинал</a>'
    )


def collect(cfg: dict[str, Any]) -> list[Listing]:
    listings: list[Listing] = []
    for source in cfg["sources"]:
        if not source.get("enabled", True):
            continue
        try:
            kind = source["kind"]
            if kind == "telegram_public":
                current = parse_telegram_source(source, cfg)
            elif kind == "weblancer":
                current = parse_weblancer_source(source, cfg)
            elif kind == "freelancejob":
                current = parse_freelancejob_source(source, cfg)
            else:
                LOG.warning("Неизвестный источник: %s", kind)
                continue
            LOG.info("%s: найдено %d записей", source.get("name", kind), len(current))
            listings.extend(current)
        except Exception as exc:
            LOG.warning("Источник %s временно недоступен: %s", source.get("name", source.get("kind")), exc)
    # Открываем только несколько самых свежих карточек, чтобы не нагружать площадки.
    details_left = int(cfg.get("max_detail_fetch_per_cycle", 8))
    detail_sources = set(cfg.get("detail_fetch_sources", []))
    for listing in sorted(listings, key=lambda item: item.published_at, reverse=True):
        if details_left <= 0:
            break
        if listing.source in detail_sources and not listing.contacts:
            enrich_listing(listing, cfg)
            details_left -= 1
    return listings


def monitor_once(cfg: dict[str, Any], store: Store, bot: TelegramBot | None, chat_id: str, dry_run: bool = False) -> list[tuple[Listing, Assessment]]:
    matches: list[tuple[Listing, Assessment]] = []
    for listing in sorted(collect(cfg), key=lambda item: item.published_at, reverse=True):
        if not dry_run and store.has_seen(listing.key):
            continue
        assessment = assess(listing, cfg)
        sent = False
        if assessment:
            matches.append((listing, assessment))
        if not dry_run:
            store.mark_seen(listing, sent=False, score=assessment.score if assessment else 0)
    matches.sort(key=lambda pair: pair[1].score, reverse=True)
    matches = matches[: cfg["filters"]["max_alerts_per_cycle"]]
    for listing, assessment in matches:
        message = format_alert(listing, assessment)
        if dry_run:
            print("\n" + "=" * 80 + "\n" + re.sub(r"<[^>]+>", "", html.unescape(message)))
        elif bot and chat_id:
            bot.send(chat_id, message)
            store.db.execute("UPDATE seen SET sent=1 WHERE key=?", (listing.key,))
            store.db.commit()
    return matches


def sample_listing() -> Listing:
    return Listing(
        source="Тестовый источник",
        external_id="sample",
        title="Создать 10 карточек товара для Wildberries",
        description=(
            "Нужно сделать 10 карточек товара для Wildberries. Фото и характеристики предоставим. "
            "Бюджет 15 000 руб. Писать @example_customer"
        ),
        url="https://example.com/order",
        published_at=datetime.now(MOSCOW),
        budget_rub=15000,
        contacts=["@example_customer"],
        responses=0,
    )


def status_text(cfg: dict[str, Any], store: Store) -> str:
    goal = int(store.get("daily_goal", str(cfg["daily_goal_rub"])))
    earned = store.today_earnings()
    remaining = max(0, goal - earned)
    paused = store.get("paused", "0") == "1"
    return (
        f"📡 Радар: {'на паузе' if paused else 'работает'}\n"
        f"Цель на сегодня: {goal:,} ₽\n".replace(",", " ")
        + f"Отмечено заработанным: {earned:,} ₽\n".replace(",", " ")
        + f"Осталось до цели: {remaining:,} ₽".replace(",", " ")
    )


def handle_command(text: str, chat_id: str, cfg: dict[str, Any], store: Store, bot: TelegramBot) -> None:
    command, *args = text.strip().split()
    command = command.split("@")[0].lower()
    if command in {"/start", "/help"}:
        store.set("chat_id", chat_id)
        bot.send(
            chat_id,
            "✅ <b>Радар заказов подключён.</b>\n\n"
            "/status — цель и прогресс\n"
            "/goal 10000 — изменить цель на день\n"
            "/done 2500 — отметить заработок\n"
            "/pause — приостановить уведомления\n"
            "/resume — возобновить\n"
            "/test — показать тестовую карточку",
        )
    elif command == "/status":
        bot.send(chat_id, html.escape(status_text(cfg, store)))
    elif command == "/goal" and args and args[0].isdigit():
        store.set("daily_goal", args[0])
        bot.send(chat_id, f"Новая цель: <b>{int(args[0]):,} ₽</b>".replace(",", " "))
    elif command == "/done" and args and args[0].isdigit():
        store.add_earning(int(args[0]))
        bot.send(chat_id, html.escape(status_text(cfg, store)))
    elif command == "/pause":
        store.set("paused", "1")
        bot.send(chat_id, "Уведомления приостановлены. Для запуска: /resume")
    elif command == "/resume":
        store.set("paused", "0")
        bot.send(chat_id, "✅ Уведомления снова включены.")
    elif command == "/test":
        listing = sample_listing()
        assessment = assess(listing, cfg)
        if assessment:
            bot.send(chat_id, format_alert(listing, assessment))
        else:
            bot.send(chat_id, "Тестовая заявка не прошла текущие фильтры.")
    else:
        bot.send(chat_id, "Не поняла команду. Используйте /help")


def run_bot(cfg: dict[str, Any], store: Store, token: str) -> None:
    bot = TelegramBot(token)
    last_scan = 0.0
    LOG.info("Бот запущен")
    while True:
        try:
            poll_commands_once(cfg, store, bot, timeout=20)
            chat_id = store.get("chat_id", os.getenv("TELEGRAM_CHAT_ID", ""))
            if chat_id and store.get("paused", "0") != "1" and time.time() - last_scan >= cfg["poll_interval_seconds"]:
                monitor_once(cfg, store, bot, chat_id)
                last_scan = time.time()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            LOG.exception("Ошибка цикла: %s", exc)
            time.sleep(15)


def poll_commands_once(
    cfg: dict[str, Any], store: Store, bot: TelegramBot, timeout: int = 0
) -> None:
    offset = int(store.get("update_offset", "0"))
    response = bot.call(
        "getUpdates",
        offset=offset,
        timeout=timeout,
        allowed_updates=json.dumps(["message"]),
    )
    for update in response.get("result", []):
        offset = max(offset, int(update["update_id"]) + 1)
        store.set("update_offset", str(offset))
        message = update.get("message") or {}
        text = message.get("text", "")
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if text.startswith("/") and chat_id:
            handle_command(text, chat_id, cfg, store, bot)


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    profile = cfg.setdefault("profile", {})
    if os.getenv("WORK_PROFILE_NAME"):
        profile["name"] = os.environ["WORK_PROFILE_NAME"].strip()
    if os.getenv("WORK_PROFILE_TELEGRAM"):
        profile["telegram"] = os.environ["WORK_PROFILE_TELEGRAM"].strip()
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-радар фриланс-заказов")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Проверить источники и вывести совпадения без Telegram")
    parser.add_argument("--test", action="store_true", help="Проверить классификацию на тестовой заявке")
    parser.add_argument("--scheduled", action="store_true", help="Один облачный цикл: команды и поиск")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    cfg = load_config(Path(args.config))
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    default_db = ROOT / cfg.get("database", "data/orders.db")
    db_path = Path(os.getenv("ORDER_RADAR_DB_PATH", str(default_db)))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    if args.test:
        listing = sample_listing()
        assessment = assess(listing, cfg)
        if not assessment:
            print("ERROR: тестовая заявка не прошла фильтр")
            return 1
        print(json.dumps(asdict(assessment), ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        monitor_once(cfg, store, bot=None, chat_id="", dry_run=True)
        return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Не задан TELEGRAM_BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен BotFather.", file=sys.stderr)
        return 2
    if args.scheduled:
        bot = TelegramBot(token)
        poll_commands_once(cfg, store, bot)
        chat_id = store.get("chat_id", os.getenv("TELEGRAM_CHAT_ID", ""))
        if chat_id and store.get("paused", "0") != "1":
            matches = monitor_once(cfg, store, bot, chat_id)
            LOG.info("Облачный цикл завершён: уведомлений %d", len(matches))
        else:
            LOG.info("Облачный цикл без поиска: нет chat_id или включена пауза")
        return 0
    run_bot(cfg, store, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
