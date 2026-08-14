#!/usr/bin/env python3
"""Личный Telegram-радар разовых фриланс-задач.

Работает только с открытыми страницами и опубликованными заказчиком контактами.
Сторонние зависимости не требуются.

Логика отбора вынесена в matching.py — там же настраивается всё, что решает,
писать заказчику или нет.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import email.utils
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matching  # noqa: E402


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
                "AppleWebKit/537.36 Chrome/124 Safari/537.36 OrderRadar/2.0"
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
    contact_mode: str = "platform"
    posting_type: str = "mixed"
    response_cost: str = "free"
    category: str = ""
    contacts: list[str] = field(default_factory=list)
    responses: int | None = None
    raw_links: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        raw = f"{self.source}|{self.external_id}|{self.url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @property
    def dedup_key(self) -> str:
        """Один и тот же заказ часто дублируется в канале и на бирже."""
        normalized = re.sub(r"[^a-zа-я0-9 ]", "", matching.normalize(self.title).lower())
        normalized = " ".join(sorted(normalized.split()))[:120]
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]


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
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(MOSCOW)
    except ValueError:
        pass
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+в?\s*(\d{1,2}):(\d{2}))?", value)
    if match:
        day, month, year = map(int, match.group(1, 2, 3))
        hour = int(match.group(4) or min(now.hour, 12))
        minute = int(match.group(5) or 0)
        return min(datetime(year, month, day, hour, minute, tzinfo=MOSCOW), now)
    return now


def extract_contacts(
    text: str,
    links: Iterable[str],
    source_channel: str = "",
    extra_excluded: Iterable[str] = (),
) -> list[str]:
    """Только те контакты, которые заказчик опубликовал сам."""
    source_channel = source_channel.lower().lstrip("@")
    found: list[str] = []
    excluded = {source_channel} | {item.lower().lstrip("@") for item in extra_excluded}
    for handle in re.findall(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})", text):
        if handle.lower() not in excluded:
            found.append("@" + handle)
    for link in links:
        parsed = urllib.parse.urlparse(link if "://" in link else "https:" + link)
        if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"}:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] in {"s", "joinchat", "addlist", "+", "share"}:
            continue
        handle = parts[0].lstrip("+")
        if handle and handle.lower() not in excluded and not handle.isdigit():
            found.append("@" + handle)
    for address in re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, flags=re.I):
        found.append(address)
    for phone in re.findall(r"(?<!\d)(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)", text):
        found.append(clean_text(phone))
    return list(dict.fromkeys(found))


def extract_responses(text: str) -> int | None:
    match = re.search(r"(\d+)\s+(?:заяв|ответ|отклик)", text, flags=re.I)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Источники
# --------------------------------------------------------------------------

def channel_title(page: str) -> str:
    match = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    return clean_text(match.group(1)) if match else ""


def recurring_handles(bodies_and_links: list[tuple[str, list[str]]], channel: str,
                      ignored: Iterable[str]) -> set[str]:
    """Контакты, которые повторяются из поста в пост, принадлежат каналу, а не заказчику.

    В каналах-перекупщиках под каждым объявлением стоит один и тот же @username
    администратора. Писать туда бесполезно: сам заказ живёт на платной бирже,
    а такой контакт лишь выглядит прямым.
    """
    counts: collections.Counter = collections.Counter()
    for body, links in bodies_and_links:
        for contact in set(extract_contacts(body, links, channel, ignored)):
            if contact.startswith("@"):
                counts[contact.lower()] += 1
    threshold = max(3, int(len(bodies_and_links) * 0.3))
    return {handle for handle, count in counts.items() if count >= threshold}


def parse_telegram_source(source: dict[str, Any], cfg: dict[str, Any]) -> list[Listing]:
    channel = source["channel"].lstrip("@")
    page = fetch_url(f"https://t.me/s/{channel}")
    title = channel_title(page)
    parser = TelegramPostParser(channel)
    parser.feed(page)

    posts = [post for post in parser.posts if post.get("post")]
    bodies = [
        (matching.strip_channel_chrome(" ".join(post["text"]), title), post["links"])
        for post in posts
    ]
    ignored = list(cfg["ignored_contact_handles"])
    house_handles = recurring_handles(bodies, channel, ignored)
    if house_handles:
        LOG.info("@%s: контакты канала, а не заказчиков: %s", channel, ", ".join(sorted(house_handles)))
        ignored += [handle.lstrip("@") for handle in house_handles]

    listings: list[Listing] = []
    for post, (body, _) in zip(posts, bodies):
        post_path = post["post"]
        if len(body) < 40:
            continue
        headline = body[:120].rsplit(" ", 1)[0] if len(body) > 120 else body
        listings.append(
            Listing(
                source=f"Telegram · @{channel}",
                external_id=post_path,
                title=headline,
                description=body,
                url=f"https://t.me/{post_path}",
                published_at=parse_datetime(post.get("datetime", "")),
                contact_mode=source.get("contact_mode", "direct"),
                posting_type=source.get("posting_type", "mixed"),
                response_cost=source.get("response_cost", "free"),
                contacts=extract_contacts(body, post["links"], channel, ignored),
                raw_links=post["links"],
            )
        )
    return listings


def parse_flru_rss(source: dict[str, Any], cfg: dict[str, Any]) -> list[Listing]:
    """FL.ru отдаёт открытую ленту заказов с точным временем и бюджетом в заголовке."""
    page = fetch_url(source["url"], timeout=25)
    listings: list[Listing] = []
    for chunk in re.findall(r"<item>(.*?)</item>", page, flags=re.S):
        def tag(name: str) -> str:
            match = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", chunk, flags=re.S)
            return clean_text(re.sub(r"<[^>]+>", " ", match.group(1))) if match else ""

        link = tag("link")
        title = tag("title")
        if not link or not title:
            continue
        try:
            published = email.utils.parsedate_to_datetime(tag("pubDate")).astimezone(MOSCOW)
        except (TypeError, ValueError):
            published = datetime.now(MOSCOW)
        listings.append(
            Listing(
                source="FL.ru",
                external_id=re.search(r"/projects/(\d+)", link).group(1) if re.search(r"/projects/(\d+)", link) else link,
                title=title,
                description=tag("description"),
                url=link,
                published_at=published,
                contact_mode=source.get("contact_mode", "platform"),
                posting_type=source.get("posting_type", "mixed"),
                response_cost=source.get("response_cost", "free"),
                category=tag("category"),
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
        listings.append(
            Listing(
                source="Weblancer",
                external_id=item_url.rstrip("/").split("-")[-1],
                title=clean_text(" ".join(article["title"])) or body[:100],
                description=body,
                url=item_url,
                published_at=parse_datetime(body),
                contact_mode=source.get("contact_mode", "platform"),
                posting_type=source.get("posting_type", "mixed"),
                response_cost=source.get("response_cost", "free"),
                contacts=extract_contacts(body, article["links"], extra_excluded=cfg["ignored_contact_handles"]),
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
        body = clean_text(strip_tags(page[match.end():end]))
        listings.append(
            Listing(
                source="FreelanceJob",
                external_id=item_id,
                title=title,
                description=body,
                url=absolute_url(url, match.group("href")),
                published_at=parse_datetime(body),
                contact_mode=source.get("contact_mode", "platform"),
                posting_type=source.get("posting_type", "mixed"),
                response_cost=source.get("response_cost", "free"),
                contacts=extract_contacts(body, [], extra_excluded=cfg["ignored_contact_handles"]),
                responses=extract_responses(body),
            )
        )
    return listings


PARSERS = {
    "telegram_public": parse_telegram_source,
    "flru_rss": parse_flru_rss,
    "weblancer": parse_weblancer_source,
    "freelancejob": parse_freelancejob_source,
}


def collect(cfg: dict[str, Any]) -> list[Listing]:
    listings: list[Listing] = []
    free_only = cfg["filters"].get("only_free_response", False)
    for source in cfg["sources"]:
        if not source.get("enabled", True):
            continue
        if free_only and source.get("response_cost", "free") != "free":
            LOG.info("%s: пропущен — отклик платный", source.get("name", source["kind"]))
            continue
        parser = PARSERS.get(source["kind"])
        if parser is None:
            LOG.warning("Неизвестный источник: %s", source["kind"])
            continue
        try:
            found = parser(source, cfg)
            LOG.info("%s: получено %d записей", source.get("name", source["kind"]), len(found))
            listings.extend(found)
        except Exception as exc:
            LOG.warning("Источник %s недоступен: %s", source.get("name", source["kind"]), exc)
    return listings


# --------------------------------------------------------------------------
# Хранилище и Telegram
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen "
            "(key TEXT PRIMARY KEY, first_seen TEXT, source TEXT, title TEXT, sent INTEGER, score INTEGER)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS earnings "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, amount INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, source TEXT)"
        )
        self.db.commit()

    def record_response(self, source: str, when: datetime | None = None) -> None:
        self.db.execute(
            "INSERT INTO responses(created_at,source) VALUES(?,?)",
            ((when or datetime.now(MOSCOW)).isoformat(), source),
        )
        self.db.commit()

    def responses_used(self, source: str, period: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM responses WHERE source=? AND created_at LIKE ?",
            (source, period + "%"),
        ).fetchone()
        return int(row[0] or 0)

    def has_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, listing: Listing, sent: bool, score: int = 0) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen(key,first_seen,source,title,sent,score) VALUES(?,?,?,?,?,?)",
            (listing.key, datetime.now(timezone.utc).isoformat(), listing.source, listing.title[:200], int(sent), score),
        )
        self.db.commit()

    def mark_sent(self, key: str) -> None:
        self.db.execute("UPDATE seen SET sent=1 WHERE key=?", (key,))
        self.db.commit()

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def add_earning(self, amount: int) -> None:
        self.db.execute(
            "INSERT INTO earnings(created_at,amount) VALUES(?,?)",
            (datetime.now(MOSCOW).isoformat(), amount),
        )
        self.db.commit()

    def today_earnings(self) -> int:
        prefix = datetime.now(MOSCOW).date().isoformat() + "%"
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM earnings WHERE created_at LIKE ?", (prefix,)
        ).fetchone()
        return int(row[0] or 0)

    def sent_today(self) -> int:
        prefix = datetime.now(timezone.utc).date().isoformat() + "%"
        row = self.db.execute(
            "SELECT COUNT(*) FROM seen WHERE sent=1 AND first_seen LIKE ?", (prefix,)
        ).fetchone()
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


def format_alert(listing: Listing, assessment: matching.Assessment,
                 cfg: dict[str, Any] | None = None, quota_left: int | None = None) -> str:
    safe = html.escape
    # В карточке — одна строка, весь список правил доступен по команде /safety.
    safety = ("<i>🛡 Для оплаты нужен только номер карты. CVC и коды из СМС — никогда. "
              "Правила: /safety</i>") if (cfg or {}).get("safety_note") else ""
    money = lambda value: f"{value:,}".replace(",", " ")
    budget = f"{money(assessment.budget_rub)} ₽" if assessment.budget_rub else "не указан"
    if assessment.contact_mode == "direct" and listing.contacts:
        # Ссылка открывает выбор чата с уже набранным письмом — остаётся нажать «отправить».
        prefilled = urllib.parse.quote(assessment.reply)
        handles = " · ".join(
            f'<a href="https://t.me/{safe(c.lstrip("@"))}">{safe(c)}</a>' if c.startswith("@") else safe(c)
            for c in listing.contacts[:3]
        )
        contact_line = (
            f"{handles}\n"
            f'<a href="https://t.me/share/url?url={prefilled}">📤 Открыть письмо готовым к отправке</a>'
        )
    elif quota_left is not None:
        contact_line = (f"Отклик через площадку · осталось бесплатных в этом месяце: "
                        f"<b>{quota_left}</b>")
    elif listing.response_cost == "paid":
        contact_line = "⚠️ Отклик через площадку — <b>платный</b>"
    else:
        contact_line = "Отклик через площадку — ссылка внизу"
    details = "\n".join(f"• {safe(point)}" for point in assessment.details) or "• Подробностей в объявлении нет."
    price = (f"Заказчик указал: <b>{budget}</b>" if assessment.budget_rub
             else "Заказчик сумму <b>не указал</b>")
    flags = ""
    if assessment.red_flags:
        flags = "\n⚠️ " + safe(assessment.red_flags[0]) + "\n"
    return (
        f"🔥 <b>{assessment.score}/100 · {safe(assessment.skill_title)}</b>\n"
        f"<i>{safe(listing.source)} · {assessment.age_minutes} мин назад</i>\n\n"
        f"📌 <b>Задача</b>\n{safe(assessment.short_line)}\n\n"
        f"📄 <b>Подробнее</b>\n{details}\n\n"
        f"💰 <b>Стоимость</b>\n{price} (предложить: <b>{money(assessment.offer_rub)} ₽</b>)\n\n"
        f"✍️ <b>Куда писать</b>\n{contact_line}\n\n"
        f"📨 <b>Текст письма</b>\n<pre>{safe(assessment.reply)}</pre>\n\n"
        f"⏱ <b>Времени на работу</b>\n{safe(assessment.duration)} с запасом\n\n"
        f"🔗 <a href=\"{safe(listing.url)}\">Открыть заказ</a>\n"
        f"{flags}"
        f"\n{safety}"
    )


# --------------------------------------------------------------------------
# Основной цикл
# --------------------------------------------------------------------------

@dataclass
class ScanResult:
    matches: list[tuple[Listing, matching.Assessment]] = field(default_factory=list)
    funnel: collections.Counter = field(default_factory=collections.Counter)
    total: int = 0
    examples: dict[str, str] = field(default_factory=dict)


def scan(cfg: dict[str, Any], store: Store | None, now: datetime | None = None,
         ignore_seen: bool = False) -> ScanResult:
    now = now or datetime.now(MOSCOW)
    result = ScanResult()
    seen_subjects: set[str] = set()
    for listing in sorted(collect(cfg), key=lambda item: item.published_at, reverse=True):
        result.total += 1
        if store is not None and not ignore_seen and store.has_seen(listing.key):
            result.funnel["уже присылали"] += 1
            continue
        if listing.dedup_key in seen_subjects:
            result.funnel["дубль другого источника"] += 1
            continue
        assessment, rejection = matching.assess(
            title=listing.title,
            text=listing.description,
            published_at=listing.published_at.astimezone(MOSCOW),
            now=now,
            contacts=listing.contacts,
            contact_mode=listing.contact_mode,
            responses=listing.responses,
            cfg=cfg,
            category=listing.category,
            posting_type=listing.posting_type,
        )
        if rejection is not None:
            result.funnel[rejection.stage] += 1
            result.examples.setdefault(rejection.stage, f"{listing.title[:70]} — {rejection.reason}")
            if store is not None and not ignore_seen:
                store.mark_seen(listing, sent=False)
            continue
        seen_subjects.add(listing.dedup_key)
        result.matches.append((listing, assessment))
    result.matches.sort(key=lambda pair: pair[1].score, reverse=True)
    return result


def quota_for(source_name: str, cfg: dict[str, Any]) -> int | None:
    for source in cfg["sources"]:
        if source.get("name") == source_name or source.get("channel") == source_name:
            return source.get("monthly_quota")
    return None


def quota_state(source_name: str, cfg: dict[str, Any], store: Store,
                now: datetime | None = None) -> tuple[int | None, bool]:
    """Сколько бесплатных откликов осталось и можно ли потратить один сегодня.

    Бесплатный аккаунт FL.ru даёт ограниченное число ответов в месяц. Радар
    растягивает их равномерно до конца месяца, чтобы квота не сгорела за первые дни.
    """
    quota = quota_for(source_name, cfg)
    if quota is None:
        return None, True
    now = now or datetime.now(MOSCOW)
    used_month = store.responses_used(source_name, now.strftime("%Y-%m"))
    remaining = max(0, quota - used_month)
    if remaining == 0:
        return 0, False
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = max(1, days_in_month - now.day + 1)
    allowance = -(-remaining // days_left)  # округление вверх
    used_today = store.responses_used(source_name, now.strftime("%Y-%m-%d"))
    return remaining, used_today < allowance


def monitor_once(cfg: dict[str, Any], store: Store, bot: TelegramBot | None, chat_id: str) -> int:
    result = scan(cfg, store)
    limit = cfg["filters"]["max_alerts_per_cycle"]
    sent = 0
    for listing, assessment in result.matches[:limit]:
        remaining, allowed = quota_state(listing.source, cfg, store)
        if not allowed:
            LOG.info("%s: бесплатные отклики на сегодня исчерпаны, заявка отложена", listing.source)
            continue
        if bot and chat_id:
            bot.send(chat_id, format_alert(listing, assessment, cfg, remaining))
            sent += 1
        if remaining is not None:
            store.record_response(listing.source)
        store.mark_seen(listing, sent=True, score=assessment.score)
        store.mark_sent(listing.key)
    # Всё, что не поместилось в лимит цикла, намеренно не помечаем показанным —
    # иначе хорошая заявка исчезнет навсегда, так и не попав в Telegram.
    LOG.info("Просмотрено %d, подошло %d, отправлено %d", result.total, len(result.matches), sent)
    return sent


def print_report(result: ScanResult, cfg: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"ПРОСМОТРЕНО ОБЪЯВЛЕНИЙ: {result.total}")
    print(f"ПОДОШЛО: {len(result.matches)}")
    print("-" * 78)
    print("Куда делись остальные:")
    for stage, count in result.funnel.most_common():
        example = result.examples.get(stage, "")
        print(f"  {stage:<24} {count:>4}   {example[:70]}")
    print("=" * 78)
    for listing, assessment in result.matches:
        print()
        print(re.sub(r"<[^>]+>", "", html.unescape(format_alert(listing, assessment, cfg))))
        print("-" * 78)


# --------------------------------------------------------------------------
# Команды бота
# --------------------------------------------------------------------------

def status_text(cfg: dict[str, Any], store: Store) -> str:
    goal = int(store.get("daily_goal", str(cfg["daily_goal_rub"])))
    earned = store.today_earnings()
    money = lambda value: f"{value:,}".replace(",", " ")
    return (
        f"📡 Радар: {'на паузе' if store.get('paused', '0') == '1' else 'работает'}\n"
        f"Отправлено заявок сегодня: {store.sent_today()}\n"
        f"Цель на сегодня: {money(goal)} ₽\n"
        f"Отмечено заработанным: {money(earned)} ₽\n"
        f"Осталось до цели: {money(max(0, goal - earned))} ₽"
    )


def sources_text(cfg: dict[str, Any]) -> str:
    lines = []
    for source in cfg["sources"]:
        mark = "✅" if source.get("enabled", True) else "⏸"
        mode = "прямой контакт" if source.get("contact_mode") == "direct" else "через площадку"
        lines.append(f"{mark} {source.get('name', source['kind'])} — {mode}")
    return "Источники:\n" + "\n".join(lines)


def quota_text(cfg: dict[str, Any], store: Store) -> str:
    lines = ["📊 Бесплатные отклики"]
    limited = False
    for source in cfg["sources"]:
        quota = source.get("monthly_quota")
        if quota is None or not source.get("enabled", True):
            continue
        limited = True
        name = source.get("name", source["kind"])
        remaining, allowed = quota_state(name, cfg, store)
        today = "можно потратить сегодня" if allowed else "на сегодня хватит, копим на лучшее"
        lines.append(f"{name}: осталось {remaining} из {quota} — {today}")
    if not limited:
        lines.append("Ограниченных площадок нет: все источники с прямым контактом.")
    lines.append("\nЗасчитать отклик вручную: /spent FL.ru")
    return "\n".join(lines)


def handle_command(text: str, chat_id: str, cfg: dict[str, Any], store: Store, bot: TelegramBot) -> None:
    command, *args = text.strip().split()
    command = command.split("@")[0].lower()
    if command in {"/start", "/help"}:
        store.set("chat_id", chat_id)
        bot.send(
            chat_id,
            "✅ <b>Радар разовых задач подключён.</b>\n\n"
            "Присылаю только то, что можно взять и сдать сегодня: без вакансий, "
            "без постоянной занятости, свежее нескольких десятков минут.\n\n"
            "/status — цель и прогресс\n"
            "/goal 10000 — изменить цель на день\n"
            "/done 2500 — отметить заработок\n"
            "/quota — сколько бесплатных откликов осталось\n"
            "/safety — правила безопасной переписки\n"
            "/spent FL.ru — засчитать потраченный отклик\n"
            "/sources — список источников\n"
            "/pause и /resume — выключить и включить\n"
            "/test — показать пример карточки",
        )
    elif command == "/status":
        bot.send(chat_id, html.escape(status_text(cfg, store)))
    elif command == "/sources":
        bot.send(chat_id, html.escape(sources_text(cfg)))
    elif command == "/quota":
        bot.send(chat_id, html.escape(quota_text(cfg, store)))
    elif command == "/safety":
        rules = "\n".join(f"• {html.escape(rule)}" for rule in cfg.get("safety_note", []))
        bot.send(chat_id, f"🛡 <b>Правила безопасной переписки</b>\n\n{rules}")
    elif command == "/spent" and args:
        source = " ".join(args)
        store.record_response(source)
        bot.send(chat_id, html.escape(f"Отклик засчитан.\n\n{quota_text(cfg, store)}"))
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
        listing, assessment = sample_match(cfg)
        if assessment:
            bot.send(chat_id, format_alert(listing, assessment, cfg))
        else:
            bot.send(chat_id, "Тестовая заявка не прошла текущие фильтры.")
    else:
        bot.send(chat_id, "Не поняла команду. Используйте /help")


def sample_match(cfg: dict[str, Any]) -> tuple[Listing, matching.Assessment | None]:
    now = datetime.now(MOSCOW)
    listing = Listing(
        source="Тестовый источник",
        external_id="sample",
        title="Срочно нужно перевести техническую документацию, 10 страниц",
        description=(
            "Нужно перевести техническую документацию на 10 страниц с английского на русский "
            "с сохранением структуры и таблиц. Сделать надо сегодня. Бюджет 12 000 руб. "
            "Писать @example_customer"
        ),
        url="https://example.com/order",
        published_at=now,
        contact_mode="direct",
        contacts=["@example_customer"],
        responses=0,
    )
    assessment, _ = matching.assess(
        title=listing.title,
        text=listing.description,
        published_at=listing.published_at,
        now=now,
        contacts=listing.contacts,
        contact_mode=listing.contact_mode,
        responses=listing.responses,
        cfg=cfg,
    )
    return listing, assessment


def poll_commands_once(cfg: dict[str, Any], store: Store, bot: TelegramBot, timeout: int = 0) -> None:
    offset = int(store.get("update_offset", "0"))
    response = bot.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=json.dumps(["message"]))
    for update in response.get("result", []):
        offset = max(offset, int(update["update_id"]) + 1)
        store.set("update_offset", str(offset))
        message = update.get("message") or {}
        text = message.get("text", "")
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if text.startswith("/") and chat_id:
            handle_command(text, chat_id, cfg, store, bot)


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


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    profile = cfg.setdefault("profile", {})
    if os.getenv("WORK_PROFILE_NAME"):
        profile["name"] = os.environ["WORK_PROFILE_NAME"].strip()
    if os.getenv("WORK_PROFILE_TELEGRAM"):
        profile["telegram"] = os.environ["WORK_PROFILE_TELEGRAM"].strip()
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-радар разовых фриланс-задач")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Показать выдачу без отправки в Telegram")
    parser.add_argument("--report", action="store_true", help="Показать воронку отбора по всем объявлениям")
    parser.add_argument("--max-age", type=int, help="Переопределить окно свежести в минутах")
    parser.add_argument("--test", action="store_true", help="Проверить логику на тестовой заявке")
    parser.add_argument("--scheduled", action="store_true", help="Один облачный цикл: команды и поиск")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    cfg = load_config(Path(args.config))
    if args.max_age:
        cfg["filters"]["max_age_minutes"] = args.max_age
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    db_path = Path(os.getenv("ORDER_RADAR_DB_PATH", str(ROOT / cfg.get("database", "data/orders.db"))))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)

    if args.test:
        listing, assessment = sample_match(cfg)
        if not assessment:
            print("ОШИБКА: тестовая заявка не прошла фильтры")
            return 1
        print(re.sub(r"<[^>]+>", "", html.unescape(format_alert(listing, assessment, cfg))))
        return 0
    if args.dry_run or args.report:
        print_report(scan(cfg, store, ignore_seen=True), cfg)
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
            monitor_once(cfg, store, bot, chat_id)
        else:
            LOG.info("Цикл без поиска: нет chat_id или включена пауза")
        return 0
    run_bot(cfg, store, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
