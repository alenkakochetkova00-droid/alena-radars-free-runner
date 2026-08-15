#!/usr/bin/env python3
"""Мозг радара: отличает разовую задачу от вакансии, считает деньги и пишет отклик.

Модуль намеренно отделён от сбора данных: сюда приходит уже готовый текст
объявления, отсюда уходит решение «писать / не писать» с объяснением причины.
Всё, что можно настроить без правки кода, живёт в config.json.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


# --------------------------------------------------------------------------
# Нормализация текста
# --------------------------------------------------------------------------

# Хвост, который Telegram дорисовывает к каждому посту в веб-превью.
TELEGRAM_FOOTER = re.compile(
    r"\s*(?:\d[\d.,]*\s*[KMК]?\s*(?:views?|просмотр\w*)\s*)?"
    r"(?:edited\s*)?\d{1,2}:\d{2}\s*(?:✉️|➕|🗂|Забрать заказ|Связаться|Разместить|Категории|.){0,120}$",
    flags=re.I,
)

_PROMO_TAIL = re.compile(
    r"(?:подписаться|подпишись|наш канал|разместить вакансию|разместить заказ|"
    r"реклама|сотрудничество|по вопросам рекламы|чат канала|отклик через бота)\b.*$",
    flags=re.I | re.S,
)


def normalize(text: str) -> str:
    """Приводит текст к виду, пригодному для поиска ключевых слов."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip()


def strip_channel_chrome(body: str, channel_title: str = "") -> str:
    """Убирает шапку канала и служебный хвост, оставляя текст самого объявления.

    Название канала попадает в начало каждого поста и ломает фильтры: в шапке
    вроде «ЗАКАЗЫ НА ДИЗАЙН | ДИЗАЙНЕРЫ | ВАКАНСИИ» есть и «заказы», и «вакансии».
    """
    body = normalize(body)
    title = normalize(channel_title)
    if title and body.lower().startswith(title.lower()):
        body = body[len(title):].strip(" -–—|:·")
    body = re.sub(r"^Forwarded from [^:]{0,60}?(?=[А-ЯA-Z])", "", body).strip()
    body = TELEGRAM_FOOTER.sub("", body)
    body = _PROMO_TAIL.sub("", body)
    return body.strip()


# --------------------------------------------------------------------------
# Деньги
# --------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:[  .,]\d{3})+|\d+(?:[.,]\d+)?"

# Слова, рядом с которыми число — это цена задачи.
_BUDGET_CUE = (
    r"бюджет|оплата|стоимость|цена|ценник|вознаграждение|гонорар|заплач\w*|"
    r"плачу|оплачу|платим|за работу|за задачу|за проект|прайс"
)
# Слова, рядом с которыми число — это зарплата, а не бюджет разовой задачи.
_SALARY_CUE = (
    r"зарплат\w*|з/п|зп\b|оклад\w*|вилка|доход|ставка в месяц|в месяц|"
    r"ежемесячн\w*|мес\.|/мес|в мес\b|на руки|после вычета|до вычета"
)

_PER_UNIT_CUE = r"за\s+(?:1|один|одну|одно|каждый|каждую|каждое|шт|штук\w*|стр\w*|тыс\w*|знак\w*|слово|пост|текст|карточк\w*|макет|баннер)"


def _to_int(raw: str) -> int:
    """«89,000» → 89000, «15 000» → 15000, «20к» → 20000, «2.5к» → 2500."""
    value = raw.strip().lower().replace(" ", " ")
    multiplier = 1
    if re.search(r"[кk]$", value):
        multiplier = 1000
        value = value[:-1].strip()
    # Разделители тысяч: пробел, точка или запятая перед ровно тремя цифрами.
    value = re.sub(r"[ .,](?=\d{3}\b)", "", value)
    value = value.replace(",", ".")
    try:
        return int(round(float(value) * multiplier))
    except ValueError:
        return 0


def _sentence_prefix(lowered: str, position: int, window: int) -> str:
    """Контекст слева, но не дальше границы предложения.

    Иначе в «Зарплата 80 000 ₽ в месяц. Бюджет задачи 5 000 ₽» слова «в месяц»
    из первого предложения пометят бюджет задачи как зарплату.
    """
    start = max(
        lowered.rfind(".", 0, position),
        lowered.rfind(";", 0, position),
        lowered.rfind("\n", 0, position),
        position - window,
        0,
    )
    return lowered[start:position]


def find_money(text: str, window: int = 60) -> list[dict[str, Any]]:
    """Все денежные суммы с контекстом слева и справа.

    Возвращает список словарей: сумма, признак зарплаты, признак «за единицу».
    Контекст важнее самой суммы — именно он отличает «бюджет 15 000 ₽ за задачу»
    от «зарплата от 80 000 ₽ в месяц».
    """
    text = normalize(text)
    lowered = text.lower()
    results: list[dict[str, Any]] = []
    pattern = re.compile(
        # «15 000 ₽», «20к руб», а также «20к» вообще без валюты
        rf"(?P<num>{_NUM})\s*(?P<k>[кk]\b)?\s*(?P<cur>₽|руб(?:лей|ля|ль|\.)?|р\.|rub)"
        rf"|(?P<num2>\d{{1,4}}(?:[.,]\d+)?)\s*(?P<k2>[кk])\b",
        flags=re.I,
    )
    for match in pattern.finditer(text):
        raw = match.group("num") or match.group("num2") or ""
        if match.group("k") or match.group("k2"):
            raw += "к"
        amount = _to_int(raw)
        if amount < 100:
            continue
        left = _sentence_prefix(lowered, match.start(), window)
        right = lowered[match.end(): match.end() + 40]
        results.append(
            {
                "amount": amount,
                "salary": bool(re.search(_SALARY_CUE, left + " " + right)),
                "explicit": bool(re.search(_BUDGET_CUE, left)),
                "per_unit": bool(re.search(_PER_UNIT_CUE, left + " " + right)),
                "range_low": bool(re.search(r"(?:от|from)\s*$", left.strip()[-6:] or "")),
            }
        )
    # Суммы в долларах — редко, но встречаются.
    for match in re.finditer(rf"(?:\$|usd\s*)({_NUM})|({_NUM})\s*(?:\$|usd)", text, flags=re.I):
        amount = _to_int(match.group(1) or match.group(2) or "")
        if amount > 0:
            left = lowered[max(0, match.start() - window): match.start()]
            results.append(
                {
                    "amount": amount,
                    "usd": True,
                    "salary": bool(re.search(_SALARY_CUE, left)),
                    "explicit": bool(re.search(_BUDGET_CUE, left)),
                    "per_unit": False,
                    "range_low": False,
                }
            )
    return results


@dataclass
class Budget:
    amount: int | None = None
    is_salary: bool = False
    per_unit: bool = False
    source: str = ""


def extract_budget(text: str, usd_rate: float = 80.0) -> Budget:
    """Бюджет именно за задачу. Зарплата за месяц сюда не попадает.

    Старая версия брала максимум из всех чисел в тексте — из-за этого месячный
    оклад 80 000 ₽ превращался в «бюджет задачи» и давал ложное «в 32 раза выше рынка».
    """
    found = find_money(text, )
    if not found:
        return Budget()
    for item in found:
        if item.get("usd"):
            item["amount"] = int(item["amount"] * usd_rate)

    salary_only = [item for item in found if item["salary"]]
    task_money = [item for item in found if not item["salary"]]
    if not task_money:
        return Budget(amount=None, is_salary=True, source="только зарплатные суммы")

    explicit = [item for item in task_money if item["explicit"]]
    pool = explicit or task_money
    per_unit = any(item["per_unit"] for item in pool)
    # Диапазон «от 1 000 до 3 000 ₽» — ориентируемся на верхнюю границу,
    # но заказчик почти всегда держит в голове нижнюю, поэтому берём медиану.
    amounts = sorted(item["amount"] for item in pool)
    if len(amounts) >= 2 and amounts[-1] <= amounts[0] * 6:
        # «От 20 000 до 60 000 ₽»: заказчик держит в голове нижнюю границу,
        # поэтому берём нижнюю середину, а не верх вилки.
        amount = amounts[(len(amounts) - 1) // 2]
    else:
        amount = amounts[0] if per_unit else amounts[-1]
    return Budget(
        amount=amount,
        is_salary=bool(salary_only) and not explicit,
        per_unit=per_unit,
        source="явно указан бюджет" if explicit else "сумма из текста",
    )


# --------------------------------------------------------------------------
# Вакансия или разовая задача
# --------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def _hits(text: str, patterns: Iterable[str]) -> list[str]:
    """Возвращает не сами регулярки, а то, что реально нашлось в объявлении.

    Так причина отказа читается человеком: «признак вакансии: испытательный срок»,
    а не «признак вакансии: испытательн\\w+ срок».
    """
    lowered = normalize(text).lower()
    found: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            found.append(match.group(0).strip())
    return found


def classify_posting(text: str, cfg: dict[str, Any], posting_type: str = "mixed") -> Verdict:
    """True — это разовая задача. False — вакансия, подписка или долгий проект.

    `posting_type` — что за источник. На бирже проектов всё по определению задача,
    и требовать слов «нужно сделать» там незачем: заголовок «Работа в Эксель»
    формально не содержит ни одного маркера, но это ровно тот заказ, который нужен.
    """
    rules = cfg["posting_rules"]
    strong = _hits(text, rules["vacancy_strong"])
    weak = _hits(text, rules["vacancy_weak"])
    task = _hits(text, rules["task_markers"])

    if strong:
        return Verdict(False, [f"признак вакансии: «{strong[0]}»"])
    if len(weak) >= rules["vacancy_weak_threshold"] and len(task) < 2:
        return Verdict(False, [f"похоже на вакансию: {', '.join(weak[:3])}"])
    if posting_type != "tasks" and not task and rules["require_task_marker"]:
        return Verdict(False, ["нет признаков разовой задачи («нужно сделать», объём, срок)"])
    return Verdict(True, [f"разовая задача: «{task[0]}»"] if task else ["разовая задача"])


# --------------------------------------------------------------------------
# Что умеет искусственный интеллект
# --------------------------------------------------------------------------

@dataclass
class SkillMatch:
    skill_id: str
    title: str
    hits: int
    terms: list[str]
    quantity: int
    minutes: int


def match_skill(text: str, cfg: dict[str, Any], title: str = "") -> SkillMatch | None:
    """Находит навык из каталога, которым задача закрывается быстро и качественно.

    При равном числе совпадений выигрывает навык, слова которого стоят в заголовке:
    «Дописать сайт на Laravel» — это разработка, даже если в описании упомянут MySQL.
    """
    lowered = normalize(text).lower()
    headline = normalize(title).lower()
    best: SkillMatch | None = None
    best_rank: tuple[int, int] = (-1, -1)
    for skill_id, skill in cfg["skills"].items():
        terms = [pattern for pattern in skill["match"] if re.search(pattern, lowered)]
        if not terms:
            continue
        in_title = sum(1 for pattern in terms if headline and re.search(pattern, headline))
        quantity = extract_quantity(lowered, skill.get("unit_pattern", ""))
        minutes = estimate_minutes(skill, quantity)
        rank = (len(terms) + in_title, in_title)
        if rank > best_rank:
            best_rank = rank
            best = SkillMatch(skill_id, skill["title"], len(terms), terms, quantity, minutes)
    return best


def blocked_by_skillset(text: str, cfg: dict[str, Any]) -> list[str]:
    """Работа, которую нельзя сдать быстро и удалённо: съёмка, 3D, звонки, доступы."""
    return _hits(text, cfg["not_for_us"])


def extract_quantity(text: str, unit_pattern: str) -> int:
    if not unit_pattern:
        return 1
    values: list[int] = []
    for match in re.finditer(rf"(\d{{1,4}})\s*(?:шт\.?\s*)?(?:{unit_pattern})", text, flags=re.I):
        values.append(int(match.group(1)))
    for match in re.finditer(rf"(?:{unit_pattern})\D{{0,12}}?(\d{{1,4}})\s*(?:шт|штук)", text, flags=re.I):
        values.append(int(match.group(1)))
    sensible = [value for value in values if 1 <= value <= 300]
    return max(sensible) if sensible else 1


def estimate_minutes(skill: dict[str, Any], quantity: int) -> int:
    minutes = skill["base_minutes"] + max(0, quantity - 1) * skill.get("per_unit_minutes", 0)
    return int(min(minutes, skill.get("max_minutes", 240)))


# --------------------------------------------------------------------------
# Срочность и срок
# --------------------------------------------------------------------------

URGENCY = [
    (r"\bсрочн", 3),
    (r"\bгорит\b|\bасап\b|\basap\b", 3),
    (r"сегодня|до вечера|к вечеру|в течение дня|прямо сейчас|сейчас нужно", 3),
    (r"завтра|к утру|до утра", 2),
    (r"на этой неделе|до пятницы|до конца недели", 1),
    (r"нужно вчера|вчера надо", 3),
]


def urgency_level(text: str) -> tuple[int, str]:
    lowered = normalize(text).lower()
    best, label = 0, ""
    for pattern, weight in URGENCY:
        if re.search(pattern, lowered) and weight > best:
            best = weight
            label = re.search(pattern, lowered).group(0)
    return best, label


def due_phrase(minutes: int) -> str:
    if minutes <= 60:
        return "в течение часа"
    if minutes <= 180:
        return f"сегодня, примерно за {max(2, round(minutes / 60))} часа"
    return "сегодня"


# --------------------------------------------------------------------------
# Цена
# --------------------------------------------------------------------------

def round_price(value: float) -> int:
    if value < 5000:
        return int(round(value / 100) * 100)
    return int(round(value / 500) * 500)


def make_offer(budget: Budget, skill: dict[str, Any], quantity: int, cfg: dict[str, Any]) -> tuple[int, str]:
    """Цена от бюджета заказчика, а не от абстрактного прайса.

    Логика простая: если человек готов платить 10 000, предлагаем около 7 000 —
    заметно дешевле конкурентов, но без демпинга. Если бюджета нет, считаем
    по нижней планке навыка.
    """
    floor = skill["min_price_rub"] + max(0, quantity - 1) * skill.get("per_unit_price_rub", 0)
    floor = round_price(floor)
    if not budget.amount:
        return floor, "бюджет не указан, цена по нижней планке"
    total = budget.amount * quantity if budget.per_unit else budget.amount
    share = cfg["pricing"]["offer_share_of_budget"]
    # Ниже планки навыка не опускаемся: если бюджет меньше, задачу просто не берём —
    # решение об этом принимает assess(), здесь честно возвращаем нижнюю границу.
    offer = round_price(max(floor, min(total * share, total * cfg["pricing"]["max_share_of_budget"])))
    return offer, f"бюджет {total:,} ₽".replace(",", " ")


# --------------------------------------------------------------------------
# Итоговая оценка
# --------------------------------------------------------------------------

@dataclass
class Assessment:
    skill_id: str
    skill_title: str
    subject: str
    short_line: str
    details: list[str]
    duration: str
    quantity: int
    minutes: int
    offer_rub: int | None
    floor_rub: int
    budget_rub: int | None
    hourly_rub: int
    age_minutes: int
    score: int
    reasons: list[str]
    red_flags: list[str]
    plan: list[str]
    reply: str
    contact_mode: str


@dataclass
class Rejection:
    stage: str
    reason: str


def clean_subject(title: str, text: str) -> str:
    """Короткая формулировка задачи, которую не стыдно процитировать заказчику."""
    subject = normalize(title)
    subject = re.sub(r"\s*\((?:Бюджет|бюджет)[^)]*\)\s*", " ", subject)
    subject = re.sub(r"#[\w_]+", " ", subject)  # хештеги канала — не часть задачи
    subject = re.sub(r"^(?:📌|🌕|🔥|⚜️|❗|‼️|\W)+", "", subject).strip()
    # Отрезаем канцелярские зачины, пока они есть: «Срочно требуется написать…» → «написать…»
    for _ in range(3):
        shorter = re.sub(
            r"^(?:срочно|очень срочно|требуется|требуются|нужен|нужна|нужно|нужны|ищем|ищу|"
            r"объявление|вакансия|заказ|задача)[!,.:\s]+", "", subject, flags=re.I
        ).strip()
        if shorter == subject or len(shorter) < 12:
            break
        subject = shorter
    if len(subject) < 12:
        sentence = re.split(r"(?<=[.!?])\s", normalize(text))[0]
        subject = sentence[:110]
    subject = subject.rstrip(" .,:;—-")
    if len(subject) > 110:
        subject = subject[:110].rsplit(" ", 1)[0] + "…"
    if subject:
        subject = subject[0].upper() + subject[1:]
    return subject or "вашей задаче"


def plural(count: int, forms: list[str]) -> str:
    """«1 страницу», «2 страницы», «10 страниц» — три формы русского счётного слова."""
    one, few, many = (forms + forms[-1:] * 3)[:3]
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


_NOISE_SENTENCE = re.compile(
    r"оплата на любую карту|разместить|категории|связаться с заказчиком|"
    r"открыть оригинал|подписаться|наш канал|бюджет\s*:|〰|подробности в личн|"
    r"жду ваших откликов|пишите в личные|заранее спасибо|всем привет|добрый день|здравствуйте",
    flags=re.I,
)


def clean_fragment(value: str) -> str:
    """Убирает эмодзи, разделители и хвосты бирж, оставляя человеческий текст."""
    value = re.sub(r"[\U0001F300-\U0001FAFF☀-➿️⬀-⯿]", " ", value)
    value = re.sub(r"#[\w_]+", " ", value)
    value = re.sub(r"[〰➖—–-]{2,}", " ", value)
    value = re.sub(r"\(Бюджет[^)]*\)", " ", value, flags=re.I)
    value = re.sub(r"https?://\S+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .,;:—-")


def short_task_line(subject: str, skill: dict[str, Any], quantity: int) -> str:
    """Одна строка: что нужно сделать и в каком объёме."""
    line = clean_fragment(subject)
    forms = skill.get("unit_forms")
    if quantity > 1 and forms and not re.search(rf"\b{quantity}\b", line):
        line += f" — {quantity} {plural(quantity, forms)}"
    if line and not line.endswith("."):
        line += "."
    return line[0].upper() + line[1:] if line else "Задача без описания."


def key_points(text: str, limit: int = 5) -> list[str]:
    """Выжимка из описания: только предложения по делу, без воды и реквизитов."""
    body = clean_fragment(normalize(text))
    points: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|(?:^|\s)[•·*]\s*|\n", body):
        sentence = sentence.strip(" .,;:—-")
        if _NOISE_SENTENCE.search(sentence):
            continue
        # Короткие фразы про срок важнее длины: «Сделать надо сегодня» терять нельзя.
        if len(sentence) < 25 and not re.search(r"сегодня|завтра|срочн|дедлайн|к вечеру|до \d", sentence, re.I):
            continue
        if len(sentence) > 190:
            sentence = sentence[:190].rsplit(" ", 1)[0] + "…"
        if sentence[0].islower():
            sentence = sentence[0].upper() + sentence[1:]
        if sentence not in points:
            points.append(sentence)
        if len(points) >= limit:
            break
    return points


def human_duration(minutes: int, margin: float = 1.6) -> str:
    """Оценка с запасом: лучше пообещать больше и сдать раньше."""
    padded = minutes * margin
    if padded <= 35:
        return "около 30 минут"
    if padded <= 50:
        return "около 45 минут"
    if padded <= 75:
        return "около часа"
    if padded <= 105:
        return "около полутора часов"
    hours = round(padded / 60)
    return f"около {hours} {plural(hours, ['часа', 'часов', 'часов'])}"


def build_reply(subject: str, skill: dict[str, Any], quantity: int, offer: int | None,
                minutes: int, cfg: dict[str, Any]) -> str:
    """Короткий отклик, который доказывает, что объявление прочитано.

    Длинные универсальные шаблоны заказчик узнаёт с первой строки и не отвечает.
    Работает другое: конкретика из задания + срок + цена + один простой вопрос.
    """
    profile = cfg.get("profile", {})
    action = skill["offer_line"]
    forms = skill.get("unit_forms") or []
    if "{n}" in action:
        if quantity > 1 and forms:
            action = action.replace("{n}", str(quantity)).replace("{unit}", plural(quantity, forms))
        elif forms:
            # Одна штука — число только утяжеляет фразу.
            action = re.sub(r"\{n\}\s*", "", action).replace("{unit}", forms[0])
        else:
            action = re.sub(r"\{n\}\s*\{unit\}\s*|\{n\}\s*", "", action)
    # Без названного бюджета цену не выдумываем, а спрашиваем: назвать 1 000 ₽
    # там, где заказчик рассчитывал на 150, — верный способ получить отказ.
    price_line = (f"Срок — {due_phrase(minutes)}, стоимость — {money(offer)} ₽." if offer
                  else f"Срок — {due_phrase(minutes)}. Сориентируйте по бюджету, "
                       f"и я сразу назову точную цену.")
    lines = [
        f"Здравствуйте! По задаче «{subject}» — готова взять сейчас.",
        action,
        price_line,
        skill["hook"],
    ]
    reply = "\n".join(line for line in lines if line)
    telegram = profile.get("telegram", "")
    if telegram:
        reply += f"\n\nTelegram: {telegram}"
    return reply


def assess(
    *,
    title: str,
    text: str,
    published_at: datetime | None,
    now: datetime,
    contacts: list[str],
    contact_mode: str,
    responses: int | None,
    cfg: dict[str, Any],
    category: str = "",
    posting_type: str = "mixed",
    response_cost: str = "free",
    max_age_minutes: int | None = None,
) -> tuple[Assessment | None, Rejection | None]:
    """Главная функция: пропускать объявление или нет и на каких условиях."""
    filters = cfg["filters"]
    body = normalize(f"{title}. {text}")

    # 1. Свежесть — самый дешёвый и самый важный отсев. В каналах пост тонет за час,
    #    а на бирже заказ висит днями, поэтому окно задаётся отдельно по источнику.
    #    Если биржа не публикует время, возраст неизвестен: отсекать по нему нельзя.
    #    Признаком новизны там служит сам факт, что заявку видим впервые.
    age_limit = max_age_minutes or filters["max_age_minutes"]
    age_minutes = max(0, int((now - published_at).total_seconds() / 60)) if published_at else 0
    if published_at and age_minutes > age_limit:
        return None, Rejection("свежесть", f"объявлению {age_minutes} мин, лимит {age_limit}")

    # 2. Мошеннические схемы.
    scam = _hits(body, cfg["scam_markers"])
    if scam:
        return None, Rejection("скам", f"подозрительная схема: «{scam[0]}»")

    # 3. Разовая задача, а не вакансия.
    posting = classify_posting(body, cfg, posting_type)
    if not posting.ok:
        return None, Rejection("вакансия", posting.reasons[0])

    # 4. Работа, которую нельзя быстро сдать удалённо.
    blocked = blocked_by_skillset(body, cfg)
    if blocked:
        return None, Rejection("не наш профиль", f"требуется ручная работа: {blocked[0]}")

    # 5. Совпадение с каталогом навыков.
    skill_match = match_skill(body, cfg, title)
    if skill_match is None:
        return None, Rejection("профиль", "задача не попадает ни в один рабочий навык")
    skill = cfg["skills"][skill_match.skill_id]
    if skill_match.minutes > filters["max_task_minutes"]:
        return None, Rejection(
            "объём", f"оценка {skill_match.minutes} мин, лимит {filters['max_task_minutes']} мин"
        )

    # 6. Контакт: писать напрямую или откликаться на площадке.
    if contact_mode == "direct" and not contacts and filters["require_direct_contact"]:
        return None, Rejection("контакт", "в объявлении нет открытого контакта заказчика")

    # 7. Деньги.
    budget = extract_budget(body, cfg["pricing"]["usd_rate_rub"])
    floor, budget_note = make_offer(budget, skill, skill_match.quantity, cfg)
    # Сумму спрашивают, а не угадывают, — если есть чем спросить: прямой контакт
    # либо бесплатный отклик на площадке. Требовать бюджет имеет смысл только там,
    # где обращение стоит денег и промах по цене обходится дорого.
    can_ask = (contact_mode == "direct" and bool(contacts)) or response_cost == "free"
    if budget.amount is None and filters["require_budget"] and not can_ask:
        return None, Rejection("бюджет", "сумма не указана, а спросить негде")

    offer: int | None = floor if budget.amount else None
    hourly = 0
    if offer is not None:
        if budget.amount and offer > budget.amount:
            return None, Rejection(
                "бюджет ниже планки",
                f"заказчик готов на {budget.amount} ₽, нижняя цена за такую работу — {offer} ₽",
            )
        hourly = int(offer / max(skill_match.minutes / 60, 0.25))
        if offer < filters["minimum_offer_rub"]:
            return None, Rejection("цена", f"{offer} ₽ ниже минимума {filters['minimum_offer_rub']} ₽")
        if hourly < filters["minimum_hourly_rub"]:
            return None, Rejection("ставка", f"{hourly} ₽/час ниже минимума {filters['minimum_hourly_rub']}")
        # Неправдоподобно высокая ставка за короткую работу — это разобранная
        # зарплата или цена-заглушка, а не бюджет задачи.
        if hourly > filters["minimum_hourly_rub"] * 15:
            return None, Rejection(
                "сумма неправдоподобна",
                f"{offer} ₽ за {skill_match.minutes} мин — похоже на зарплату или ошибку разбора",
            )

    # 8. Баллы.
    urgency, urgency_word = urgency_level(body)
    freshness = 30 * max(0.0, 1 - age_minutes / age_limit) if published_at else 18
    fit = min(20, 8 + skill_match.hits * 4)
    # Без бюджета судить о деньгах нельзя — ставим нейтральную середину.
    money = min(15, hourly / filters["minimum_hourly_rub"] * 7) if offer else 7
    contact_points = 15 if (contacts and contact_mode == "direct") else 6
    competition = 8 if responses is None else max(0, 8 - responses)
    # Много простых задач выгоднее двух долгих: короткая работа получает надбавку.
    simplicity = 12 * max(0.0, 1 - skill_match.minutes / filters["max_task_minutes"])
    score = int(round(min(
        100, freshness + fit + money + contact_points + competition + simplicity + urgency * 2
    )))
    if score < filters["minimum_score"]:
        return None, Rejection("баллы", f"{score} из 100, порог {filters['minimum_score']}")

    # 9. Объяснение и предупреждения.
    reasons = [
        f"свежее объявление: {age_minutes} мин назад",
        f"попадает в навык «{skill_match.title}», работы примерно на {skill_match.minutes} мин",
    ]
    if budget.amount:
        reasons.append(f"{budget_note}, предлагаем ниже — заметное преимущество перед конкурентами")
    if urgency:
        reasons.append(f"заказчику нужно быстро: «{urgency_word}»")
    if contacts and contact_mode == "direct":
        reasons.append(f"можно писать напрямую: {', '.join(contacts[:2])}")
    if responses is not None and responses <= 3:
        reasons.append(f"мало откликов: {responses}")

    red_flags: list[str] = []
    if not budget.amount:
        red_flags.append(f"бюджет не указан — спросите цену, ваша нижняя планка {floor} ₽")
    if hourly > filters["minimum_hourly_rub"] * 6:
        red_flags.append("ставка выглядит слишком хорошей — вероятно, объём работ больше, чем в тексте")
    if budget.is_salary:
        red_flags.append("в тексте есть месячные суммы — проверьте, что это оплата за задачу")
    if budget.amount and budget.amount > offer * 8:
        red_flags.append("бюджет непропорционально большой для такого объёма — уточните состав работ")
    if contact_mode == "platform":
        red_flags.append("прямого контакта нет, отклик только через площадку")
    if skill_match.quantity > 20:
        red_flags.append(f"большой объём ({skill_match.quantity} шт.) — согласуйте партиями")

    subject = clean_subject(title, text)
    assessment = Assessment(
        skill_id=skill_match.skill_id,
        skill_title=skill_match.title,
        subject=subject,
        short_line=short_task_line(subject, skill, skill_match.quantity),
        details=key_points(text),
        duration=human_duration(skill_match.minutes),
        quantity=skill_match.quantity,
        minutes=skill_match.minutes,
        offer_rub=offer,
        floor_rub=floor,
        budget_rub=budget.amount,
        hourly_rub=hourly,
        age_minutes=age_minutes,
        score=score,
        reasons=reasons,
        red_flags=red_flags,
        plan=skill["plan"],
        reply=build_reply(subject, skill, skill_match.quantity, offer, skill_match.minutes, cfg),
        contact_mode=contact_mode,
    )
    return assessment, None
