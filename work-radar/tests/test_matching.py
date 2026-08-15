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


class SmallTaskTests(unittest.TestCase):
    """Ставка на поток: много мелких заказов лучше двух дорогих и долгих."""

    def setUp(self):
        self.cfg = config()
        self.now = datetime.now(MOSCOW)

    def build(self, text):
        return matching.assess(
            title=text[:80], text=text, published_at=self.now, now=self.now,
            contacts=["@customer_name"], contact_mode="direct", responses=0, cfg=self.cfg,
        )

    def test_cheap_task_from_500_is_accepted(self):
        assessment, rejection = self.build(
            "Нужно срочно написать текст на сайт про доставку. Бюджет 900 руб."
        )
        self.assertIsNone(rejection, rejection.reason if rejection else "")
        self.assertLessEqual(assessment.offer_rub, 900)
        self.assertGreaterEqual(assessment.offer_rub, self.cfg["filters"]["minimum_offer_rub"])

    def test_minimum_price_floor_is_500(self):
        self.assertEqual(self.cfg["filters"]["minimum_offer_rub"], 500)

    def test_no_price_is_invented_without_a_budget(self):
        """«Бюджет по договорённости» — цену спрашиваем, а не выдумываем.

        Именно так радар предложил 1 000 ₽ там, где рынок ставил 150 ₽.
        Заявку не теряем: писать заказчику бесплатно, значит цену можно уточнить.
        """
        assessment, rejection = self.build(
            "Срочно нужно изменить презентацию в пдф: убрать название, изменить цены."
        )
        self.assertIsNone(rejection, rejection.reason if rejection else "")
        self.assertIsNone(assessment.offer_rub)
        self.assertIn("сориентируйте по бюджету", assessment.reply.lower())
        self.assertNotIn("₽", assessment.reply)

    def test_missing_budget_is_dropped_when_asking_costs_money(self):
        """Платный отклик без названной суммы — риск потратить деньги впустую."""
        assessment, rejection = matching.assess(
            title="Изменить презентацию", text="Нужно изменить презентацию в пдф. Убрать название.",
            published_at=self.now, now=self.now, contacts=[], contact_mode="platform",
            responses=0, cfg=self.cfg, posting_type="tasks", response_cost="paid",
        )
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "бюджет")

    def test_missing_budget_is_kept_when_the_response_is_free(self):
        """Заказ «для всех»: отклик ничего не стоит, значит цену можно спросить."""
        assessment, rejection = matching.assess(
            title="Изменить презентацию", text="Срочно нужно изменить презентацию в пдф. Убрать название.",
            published_at=self.now, now=self.now, contacts=[], contact_mode="platform",
            responses=0, cfg=self.cfg, posting_type="tasks", response_cost="free",
        )
        self.assertIsNone(rejection, rejection.reason if rejection else "")
        self.assertIsNone(assessment.offer_rub)

    def test_offer_never_exceeds_what_the_customer_named(self):
        assessment, _ = self.build("Срочно нужно поправить текст на сайте. Бюджет 800 руб.")
        self.assertIsNotNone(assessment)
        self.assertLessEqual(assessment.offer_rub, 800)

    def test_short_task_outranks_long_one(self):
        quick, _ = self.build("Срочно нужно написать текст на сайт. Бюджет 2 000 руб.")
        slow, _ = self.build("Срочно нужно написать 6 текстов на сайт. Бюджет 6 000 руб.")
        self.assertIsNotNone(quick)
        self.assertIsNotNone(slow)
        self.assertLess(quick.minutes, slow.minutes)
        self.assertGreater(quick.score, slow.score)

    def test_task_longer_than_the_limit_is_rejected(self):
        assessment, rejection = self.build(
            "Нужно написать 40 текстов на сайт про доставку. Бюджет 20 000 руб."
        )
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "объём")


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

    def test_board_without_a_timestamp_is_not_dropped(self):
        """Биржа может не публиковать время. Отсекать по неизвестному возрасту нельзя.

        Из-за этого FreelanceJob и Weblancer целиком уходили в «несвежие».
        """
        assessment, rejection = self.assess(GOOD_TASK, published_at=None)
        self.assertIsNone(rejection, rejection.reason if rejection else "")
        self.assertEqual(assessment.age_minutes, 0)

    def test_implausible_sum_rejected(self):
        """280 000 ₽ за час работы — это разобранная зарплата, а не бюджет задачи."""
        assessment, rejection = self.assess(
            "Срочно нужно написать текст на сайт про доставку. Бюджет 400 000 руб."
        )
        self.assertIsNone(assessment)
        self.assertEqual(rejection.stage, "сумма неправдоподобна")

    def test_sales_role_is_a_vacancy(self):
        assessment, _ = self.assess(
            "Ищем партнера по продажам / менеджера по работе с клиентами, IT-продукт, 40%"
        )
        self.assertIsNone(assessment)

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


class ComputerOnlyTests(unittest.TestCase):
    """Берём только то, что делается за компьютером и удалённо."""

    def setUp(self):
        self.cfg = config()

    def test_acting_gig_rejected(self):
        text = "Ищем актёра / лицо в кадр для рекламного ролика, нужно сняться в видео. Бюджет 20 000 ₽"
        self.assertTrue(matching.blocked_by_skillset(text, self.cfg))

    def test_turnkey_project_rejected(self):
        text = ("Разработка платформы управления автопрокатом + 2 сайта бронирования на PHP. "
                "Компания заказывает разработку под ключ. Бюджет 300 000 ₽")
        self.assertTrue(matching.blocked_by_skillset(text, self.cfg))

    def test_offline_presence_rejected(self):
        text = "Нужно приехать и сделать опись имущества на территории заказчика. Бюджет 5 000 ₽"
        self.assertTrue(matching.blocked_by_skillset(text, self.cfg))

    def test_presentation_is_our_work(self):
        match = matching.match_skill(
            "нужно собрать презентацию на 12 слайдов для инвесторов", self.cfg)
        self.assertEqual(match.skill_id, "presentations")
        self.assertEqual(match.quantity, 12)

    def test_document_edit_is_our_work(self):
        match = matching.match_skill(
            "нужно изменить цифры в документе, шрифт и расстояние оставить", self.cfg)
        self.assertEqual(match.skill_id, "doc_edit")

    def test_vector_graphics_is_our_work(self):
        match = matching.match_skill(
            "нужна блок-схема процесса согласования и иконки для сайта", self.cfg)
        self.assertEqual(match.skill_id, "vector_graphics")

    def test_illustration_still_rejected(self):
        """Векторные схемы берём, а рисование иллюстраций — нет."""
        self.assertTrue(matching.blocked_by_skillset(
            "нужно нарисовать иллюстрацию персонажа для афиши", self.cfg))

    def test_one_c_is_not_ours(self):
        """1С требует доступа к копии заказчика — сдать результат нельзя."""
        match = matching.match_skill("подстановка цены мрц из марки в рмк 1с", self.cfg)
        self.assertIsNone(match)

    def test_plain_script_task_still_allowed(self):
        text = "Написать скрипт по удалению дубликатов фраз из двух списков в Excel. Бюджет 2 500 ₽"
        self.assertEqual(matching.blocked_by_skillset(text, self.cfg), [])

    def test_server_administration_rejected(self):
        """Развернуть игровой сервер через чат нельзя — нужен доступ к железу."""
        text = ("Скрипт для создания зеркал игрового сервера кс 1.6. Развертывание зеркал "
                "основного сервера, подключение к мастер-серверу Steam. Бюджет 5 000 ₽")
        self.assertTrue(matching.blocked_by_skillset(text, self.cfg))


class ScamTests(unittest.TestCase):
    """Схемы, на которых уже пытались развести."""

    def setUp(self):
        self.cfg = config()
        self.now = datetime.now(MOSCOW)

    def reject_stage(self, text):
        _, rejection = matching.assess(
            title=text[:80], text=text, published_at=self.now, now=self.now,
            contacts=["@customer_name"], contact_mode="direct", responses=0, cfg=self.cfg,
        )
        return rejection.stage if rejection else None

    def test_card_details_request(self):
        self.assertEqual(
            self.reject_stage("Нужно написать текст, 5 000 ₽. Пришлю ссылку, заполните данные карты"),
            "скам",
        )

    def test_payment_link_scheme(self):
        self.assertEqual(
            self.reject_stage("Нужен текст срочно, бюджет 5 000 ₽. Перейдите по ссылке для оплаты"),
            "скам",
        )

    def test_sms_code_request(self):
        self.assertEqual(
            self.reject_stage("Написать 5 текстов, 6 000 ₽. Пришлите код подтверждения для доступа"),
            "скам",
        )

    def test_safety_note_is_configured(self):
        self.assertTrue(self.cfg["safety_note"])


class CardTests(unittest.TestCase):
    """Карточка должна читаться с телефона за десять секунд."""

    def setUp(self):
        self.cfg = config()
        self.now = datetime.now(MOSCOW)

    def build(self, text):
        assessment, _ = matching.assess(
            title=text[:80], text=text, published_at=self.now, now=self.now,
            contacts=["@customer_name"], contact_mode="direct", responses=0, cfg=self.cfg,
        )
        return assessment

    def test_short_line_is_one_sentence(self):
        assessment = self.build(GOOD_TASK)
        self.assertTrue(assessment.short_line.endswith("."))
        self.assertLess(len(assessment.short_line), 130)

    def test_details_drop_boilerplate_and_emoji(self):
        text = (
            "📌 Нужно перевести документацию 📝 Необходимо перевести 10 страниц на русский "
            "с сохранением таблиц. Сделать надо сегодня. 〰️ 〰️ 💳 Бюджет: 12 000 ₽ 〰️ "
            "🌐 Оплата на любую карту мира #переводы"
        )
        points = matching.key_points(text)
        joined = " ".join(points)
        self.assertNotIn("〰", joined)
        self.assertNotIn("#переводы", joined)
        self.assertNotIn("Оплата на любую карту", joined)
        self.assertIn("сохранением таблиц", joined)

    def test_details_keep_the_deadline(self):
        points = matching.key_points(
            "Необходимо перевести 10 страниц с сохранением таблиц. Сделать надо сегодня."
        )
        self.assertTrue(any("сегодня" in point.lower() for point in points))

    def test_duration_has_a_safety_margin(self):
        """Обещаем с запасом: 88 минут работы — это «около 2 часов», а не «полтора»."""
        self.assertEqual(matching.human_duration(25), "около 45 минут")
        self.assertEqual(matching.human_duration(88), "около 2 часов")
        self.assertEqual(matching.human_duration(180), "около 5 часов")

    def test_price_is_a_single_total(self):
        """Никаких ставок в час и делений — одна сумма за всё задание."""
        assessment = self.build(GOOD_TASK)
        self.assertIsInstance(assessment.offer_rub, int)
        self.assertNotIn("час", assessment.short_line.lower())


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
