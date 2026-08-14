"""Проверки сбора данных: чей контакт в посте и во что обходится отклик."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import order_radar as R  # noqa: E402


class RecurringHandleTests(unittest.TestCase):
    """Один и тот же @username под всеми постами — это канал, а не заказчик."""

    def test_channel_admin_is_filtered_out(self):
        posts = [
            (f"Нужно сделать задачу номер {index}. Писать @Trasterr", [])
            for index in range(8)
        ]
        house = R.recurring_handles(posts, "it_zakazy", [])
        self.assertIn("@trasterr", house)

    def test_real_customers_are_kept(self):
        posts = [
            ("Нужен текст, писать @client_one", []),
            ("Нужна таблица, писать @client_two", []),
            ("Нужен перевод, писать @client_three", []),
            ("Нужен скрипт, писать @client_four", []),
        ]
        self.assertEqual(R.recurring_handles(posts, "somechannel", []), set())

    def test_mixed_channel_keeps_customers_drops_admin(self):
        posts = [
            ("Нужен текст, писать @client_one. Вопросы @admin_handle", []),
            ("Нужна таблица, писать @client_two. Вопросы @admin_handle", []),
            ("Нужен перевод, писать @client_three. Вопросы @admin_handle", []),
            ("Нужен скрипт, писать @client_four. Вопросы @admin_handle", []),
        ]
        house = R.recurring_handles(posts, "somechannel", [])
        self.assertEqual(house, {"@admin_handle"})


class FreeSourceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = R.load_config(ROOT / "config.json")

    def test_every_source_declares_response_cost(self):
        for source in self.cfg["sources"]:
            self.assertIn(
                source.get("response_cost"), {"free", "paid", "quota"},
                f"источник {source.get('name')} не указал стоимость отклика",
            )

    def test_no_paid_source_is_enabled(self):
        """Ни одна включённая площадка не должна требовать денег за отклик."""
        for source in self.cfg["sources"]:
            if source.get("response_cost") == "paid":
                self.assertFalse(
                    source.get("enabled", True),
                    f"{source.get('name')}: платный отклик, источник должен быть выключен",
                )

    def test_free_sources_are_direct_contact(self):
        """Бесплатный отклик возможен только там, где есть контакт заказчика."""
        for source in self.cfg["sources"]:
            if source.get("response_cost") == "free":
                self.assertEqual(
                    source.get("contact_mode"), "direct",
                    f"{source.get('name')}: бесплатный отклик без прямого контакта невозможен",
                )

    def test_quota_sources_declare_a_limit(self):
        for source in self.cfg["sources"]:
            if source.get("response_cost") == "quota":
                self.assertIsInstance(source.get("monthly_quota"), int)


class QuotaTests(unittest.TestCase):
    """Бесплатные отклики нужно растянуть на месяц, а не сжечь за первый день."""

    def setUp(self):
        import tempfile
        self.cfg = R.load_config(ROOT / "config.json")
        self.tmp = tempfile.TemporaryDirectory()
        self.store = R.Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unlimited_source_always_allowed(self):
        remaining, allowed = R.quota_state("Фриланс Таверна", self.cfg, self.store)
        self.assertIsNone(remaining)
        self.assertTrue(allowed)

    def test_fresh_month_allows_a_response(self):
        remaining, allowed = R.quota_state("FL.ru", self.cfg, self.store)
        self.assertEqual(remaining, 15)
        self.assertTrue(allowed)

    def test_exhausted_quota_blocks(self):
        for _ in range(15):
            self.store.record_response("FL.ru")
        remaining, allowed = R.quota_state("FL.ru", self.cfg, self.store)
        self.assertEqual(remaining, 0)
        self.assertFalse(allowed)

    def test_daily_pacing_stops_burning_quota(self):
        """Первого числа с квотой 15 на 30 дней нельзя потратить всё сразу."""
        from datetime import datetime
        now = datetime(2026, 9, 1, 12, 0, tzinfo=R.MOSCOW)
        for _ in range(3):
            self.store.record_response("FL.ru", when=now)
        remaining, allowed = R.quota_state("FL.ru", self.cfg, self.store, now=now)
        self.assertEqual(remaining, 12)
        self.assertFalse(allowed, "дневная норма исчерпана, но радар всё равно разрешает отклик")


if __name__ == "__main__":
    unittest.main(verbosity=2)
