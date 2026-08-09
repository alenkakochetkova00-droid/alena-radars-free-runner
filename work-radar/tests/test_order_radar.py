import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("order_radar", ROOT / "order_radar.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RadarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = MODULE.load_config(ROOT / "config.json")

    def test_budget_parsing(self):
        self.assertEqual(MODULE.parse_budget_rub("Бюджет 15 000 руб."), 15000)
        self.assertEqual(MODULE.parse_budget_rub("Оплата 20к"), 20000)
        self.assertEqual(MODULE.parse_budget_rub("Budget $300", 80), 24000)

    def test_contact_extraction(self):
        contacts = MODULE.extract_contacts("Пишите @customer_name или test@example.com", [], "source")
        self.assertIn("@customer_name", contacts)
        self.assertIn("test@example.com", contacts)

    def test_good_marketplace_order(self):
        now = datetime.now(ZoneInfo("Europe/Moscow"))
        listing = MODULE.Listing(
            source="test",
            external_id="1",
            title="10 карточек для Wildberries",
            description="Нужно 10 карточек товара. Бюджет 15 000 руб. @customer_name",
            url="https://example.com/1",
            published_at=now,
            budget_rub=15000,
            contacts=["@customer_name"],
            responses=0,
        )
        result = MODULE.assess(listing, self.cfg, now=now)
        self.assertIsNotNone(result)
        self.assertEqual(result.offer_price_rub, 10000)
        self.assertEqual(result.quantity, 10)

    def test_scam_is_excluded(self):
        listing = MODULE.sample_listing()
        listing.description += " Нужно оставить отзыв и сделать выкуп товара"
        self.assertIsNone(MODULE.assess(listing, self.cfg))


if __name__ == "__main__":
    unittest.main()
