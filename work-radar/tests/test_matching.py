"""Регрессия на реальных объявлениях, которые радар присылал зря."""

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matching  # noqa: E402

MOSCOW = ZoneInfo("Europe/Moscow")

# Объявление, из-за которого всё и затевалось: вакансия в штат, месячная зарплата,
# 3D в Unreal Engine. Старый радар оценил её на 95/100 и предложил писать.
UNREAL_VACANCY = (
    "ОБЪЯВЛЕНИЕ Требуется 3D Motion Designer / Generalist (Unreal Engine) "
    "Компания: GameGears.Online Формат работы: Удаленка Занятость: Part-time на постоянной основе. "
    "Ищем на проектную занятость, по результатам работы можем взять в штат. "
    "Что предстоит делать: рекламные креативы, весь пайплайн только на Unreal Engine. "
    "Зарплата: 80 000 ₽. Контакт @korobkowave"
)

ARBITRAGE_VACANCY = (
    "Требуется графический дизайнер. Мы арбитражная команда, которая работает с 2016 года "
    "и ищем в команду графического дизайнера. Задачи: создание рекламных креативов. "
    "От тебя: полная занятость, опыт создания статических рекламных креативов. "
    "Зарплата 40 000 ₽ в месяц. @alinapolukey"
)

GOOD_TASK = (
    "Срочно нужно перевести техническую документацию, 10 страниц, с английского на русский "
    "с сохранением таблиц. Нужно сделать сегодня. Бюджет 12 000 руб. Писать @customer_name"
)


def config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


class BudgetTests(unittest.TestCase):
    def test_plain_amount(self):
        self.assertEqual(matching.extract_budget("Бюджет 15 000 руб.").amount, 15000)

    def test_thousand_suffix(self):
        self.assertEqual(matching.extract_budget("Оплата 20к").amount, 20000)

    def test_comma_separated_thousands(self):
        """Kwork пишет «От 89,000 ₽ до 200,000 ₽» — старый парсер это терял."""
        budget = matching.extract_budget("Бюджет: От 89,000 ₽ до 200,000 ₽")
        self.assertIsNotNone(budget.amount)
        self.assertEqual(budget.amount, 89000)

    def test_range_takes_lower_half(self):
        self.assertEqual(matching.extract_budget("Бюджет: От 20,000 ₽ до 60,000 ₽").amount, 20000)

    def test_salary_is_not_a_task_budget(self):
        """Месячный оклад не должен превращаться в бюджет разовой задачи."""
        budget = matching.extract_budget("Зарплата 80 000 ₽ в месяц")
        self.assertIsNone(budget.amount)

    def test_task_budget_wins_over_salary_mention(self):
        budget = matching.extract_budget("Зарплата на позиции 80 000 ₽ в месяц. Бюджет задачи 5 000 ₽")
        self.assertEqual(budget.amount, 5000)


class VacancyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_unreal_vacancy_rejected(self):
        self.assertFalse(matching.classify_posting(UNREAL_VACANCY, self.cfg).ok)

    def test_arbitrage_vacancy_rejected(self):
        self.assertFalse(matching.classify_posting(ARBITRAGE_VACANCY, self.cfg).ok)

    def test_real_task_accepted(self):
        self.assertTrue(matching.classify_posting(GOOD_TASK, self.cfg).ok)

    def test_task_board_does_not_need_marker_words(self):
        """«Работа в Эксель» на бирже проектов — это заказ, а не вакансия."""
        self.assertFalse(matching.classify_posting("Работа в Эксель", self.cfg).ok)
        self.assertTrue(matching.classify_posting("Работа в Эксель", self.cfg, "tasks").ok)


class SkillsetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_unreal_is_out_of_scope(self):
        self.assertTrue(matching.blocked_by_skillset(UNREAL_VACANCY, self.cfg))

    def test_fence_mesh_is_not_3d_modelling(self):
        """«3D-сетки Гиттер» — это забор, а не 3D-моделирование."""
        text = "Написать текст про ограждения: забор из евроштакетника, профнастила, 3D-сетки Гиттер"
        self.assertEqual(matching.blocked_by_skillset(text, self.cfg), [])

    def test_translation_quantity_from_abbreviation(self):
        match = matching.match_skill("нужно перевести документацию на 10 стр с английского", self.cfg)
        self.assertEqual(match.skill_id, "translation")
        self.assertEqual(match.quantity, 10)


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_offer_is_below_customer_budget(self):
        """Заказчик готов на 10 000 — предлагаем около 7 000, как и задумано."""
        skill = self.cfg["skills"]["texts_seo"]
        offer, _ = matching.make_offer(matching.Budget(amount=10000), skill, 1, self.cfg)
        self.assertEqual(offer, 7000)

    def test_offer_never_below_skill_floor(self):
        skill = self.cfg["skills"]["code_script"]
        offer, _ = matching.make_offer(matching.Budget(amount=2000), skill, 1, self.cfg)
        self.assertGreaterEqual(offer, skill["min_price_rub"])

    def test_plural_forms(self):
        forms = ["страницу", "страницы", "страниц"]
        self.assertEqual(matching.plural(1, forms), "страницу")
        self.assertEqual(matching.plural(3, forms), "страницы")
        self.assertEqual(matching.plural(11, forms), "страниц")


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.now = datetime.now(MOSCOW)

    def assess(self, text, **kwargs):
        params = dict(
            title=text[:80],
            text=text,
            published_at=self.now,
            now=self.now,
            contacts=["@customer_name"],
            contact_mode="direct",
            responses=0,
            cfg=self.cfg,
        )
        params.update(kwargs)
        return matching.assess(**params)

    def test_good_task_passes(self):
        assessment, rejection = self.assess(GOOD_TASK)
        self.assertIsNone(rejection)
        self.assertEqual(assessment.skill_id, "translation")
        self.assertLess(assessment.offer_rub, 12000)

    def test_unreal_vacancy_never_sent(self):
        assessment, rejection = self.assess(UNREAL_VACANCY, contacts=["@korobkowave"])
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "вакансия")

    def test_arbitrage_vacancy_never_sent(self):
        assessment, _ = self.assess(ARBITRAGE_VACANCY, contacts=["@alinapolukey"])
        self.assertIsNone(assessment)

    def test_stale_posting_rejected(self):
        old = self.now - timedelta(minutes=self.cfg["filters"]["max_age_minutes"] + 5)
        assessment, rejection = self.assess(GOOD_TASK, published_at=old)
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "свежесть")

    def test_scam_rejected(self):
        assessment, rejection = self.assess(
            "Нужно срочно оставить положительный отзыв о работе магазина. Бюджет 700 руб."
        )
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "скам")

    def test_reply_quotes_the_task(self):
        assessment, _ = self.assess(GOOD_TASK)
        self.assertIn("перевести техническую документацию", assessment.reply.lower())
        self.assertIn("10 страниц", assessment.reply)
        self.assertNotIn("{", assessment.reply)

    def test_reply_never_mentions_ai(self):
        assessment, _ = self.assess(GOOD_TASK)
        lowered = assessment.reply.lower()
        for word in ("нейросет", "chatgpt", "gpt", "искусственный интеллект", "ии "):
            self.assertNotIn(word, lowered)

    def test_missing_contact_on_direct_source_rejected(self):
        assessment, rejection = self.assess(GOOD_TASK, contacts=[])
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "контакт")

    def test_platform_source_works_without_contact(self):
        assessment, _ = self.assess(GOOD_TASK, contacts=[], contact_mode="platform")
        self.assertIsNotNone(assessment)


class TextTests(unittest.TestCase):
    def test_channel_header_is_stripped(self):
        body = matching.strip_channel_chrome(
            "ЗАКАЗЫ НА ДИЗАЙН | ДИЗАЙНЕРЫ | ВАКАНСИИ Нужно написать текст 14 views 06:48",
            "ЗАКАЗЫ НА ДИЗАЙН | ДИЗАЙНЕРЫ | ВАКАНСИИ",
        )
        self.assertNotIn("ВАКАНСИИ", body)
        self.assertIn("Нужно написать текст", body)
        self.assertNotIn("views", body)

    def test_subject_drops_hashtags_and_boilerplate(self):
        subject = matching.clean_subject("#ищу #копирайтер Срочно требуется написать 5 текстов", "")
        self.assertNotIn("#", subject)
        self.assertTrue(subject.startswith("Написать"), subject)


if __name__ == "__main__":
    unittest.main(verbosity=2)
