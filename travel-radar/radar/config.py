"""Загрузка config.yaml + секретов из .env."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = Path(os.getenv("DEAL_RADAR_LOG_DIR", str(ROOT / "logs")))
DB_PATH = Path(os.getenv("DEAL_RADAR_DB_PATH", str(ROOT / "dealradar.sqlite3")))
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.yaml"


def load_env() -> dict[str, str]:
    """Простой парсер .env — без внешних зависимостей."""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    # Переменные окружения имеют приоритет над файлом
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TRAVELPAYOUTS_TOKEN", "TRAVELPAYOUTS_MARKER"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def save_env(values: dict[str, str]) -> None:
    existing = load_env()
    existing.update({k: v for k, v in values.items() if v is not None})
    lines = [
        "# Секреты Deal Radar. Файл не коммитить и никому не показывать.",
        "",
        *(f"{k}={v}" for k, v in existing.items()),
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    ENV_PATH.chmod(0o600)


class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        self.env = load_env()
        # Правки, сделанные командами бота. Живут в БД, переживают перезапуск.
        self.overrides: dict[str, Any] = getattr(self, "overrides", {}) or {}
        self.cities: dict[str, dict] = self._load_json("cities.json")
        self.visas: dict[str, dict] = self._load_json("visa_rf.json")
        self.baselines: dict[str, Any] = self._load_json("baselines.json")
        self.hotel_nice: dict[str, Any] = self._load_json("hotel_nice.json")
        # Справочники Travelpayouts: код → [русское название, страна] и
        # код → [название авиакомпании, лоукостер?]. Нужны, чтобы в сообщении
        # не было «BOM» вместо «Мумбаи».
        self.city_names: dict[str, Any] = self._load_json("city_names_ru.json")
        self.airlines: dict[str, Any] = self._load_json("airlines_ru.json")
        self.airline_rules: dict[str, Any] = self._load_json("airline_rules.json")
        self.rail_routes: list[dict] = self._load_json("rail_routes.json").get("routes", [])

    def _load_json(self, name: str) -> dict:
        path = DATA_DIR / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    # --- удобные геттеры ---------------------------------------------------
    def get(self, path: str, default=None):
        if path in self.overrides:
            return self.overrides[path]
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        # Секции целиком тоже могут быть частично переопределены
        if isinstance(node, dict):
            patched = dict(node)
            prefix = path + "."
            for key, value in self.overrides.items():
                if key.startswith(prefix) and "." not in key[len(prefix):]:
                    patched[key[len(prefix):]] = value
            return patched
        return node

    def apply_overrides(self, values: dict[str, Any]) -> None:
        self.overrides.update(values or {})

    @property
    def bot_token(self) -> str:
        return self.env.get("TELEGRAM_BOT_TOKEN", "")

    @property
    def chat_id(self) -> str:
        return self.env.get("TELEGRAM_CHAT_ID", "")

    @property
    def tp_token(self) -> str:
        return self.env.get("TRAVELPAYOUTS_TOKEN", "")

    @property
    def tp_marker(self) -> str:
        return self.env.get("TRAVELPAYOUTS_MARKER", "")

    def city(self, iata: str) -> dict:
        return self.cities.get(iata.upper(), {})

    def country_of(self, iata: str) -> str:
        own = self.city(iata).get("country")
        if own:
            return own
        entry = self.city_names.get(iata.upper())
        return entry[1] if entry and len(entry) > 1 else ""

    def city_name(self, iata: str) -> str:
        """Русское название города. Сначала свой справочник (там же районы,
        визы и бюджеты), затем полный словарь Travelpayouts на 9 600 городов."""
        own = self.city(iata).get("ru")
        if own:
            return own
        entry = self.city_names.get(iata.upper())
        return entry[0] if entry else iata

    def airline_name(self, code: str) -> str:
        entry = self.airlines.get((code or "").upper())
        return entry[0] if entry else (code or "")

    def airline_is_lowcost(self, code: str) -> bool:
        entry = self.airlines.get((code or "").upper())
        return bool(entry[1]) if entry and len(entry) > 1 else False

    def airline_rule(self, code: str) -> dict:
        """Что известно про багаж и парк самолётов конкретной авиакомпании."""
        return self.airline_rules.get((code or "").upper(), {})

    def nice_hotel_price(self, iata: str) -> int:
        """Хорошее эстетичное жильё, руб/ночь. Если города нет в справочнике —
        оцениваем как удвоенную обычную цену."""
        value = self.hotel_nice.get(iata.upper())
        if value:
            return int(value)
        return int(float(self.city(iata).get("hotel", 4000)) * 2)

    def reload(self) -> None:
        saved = dict(self.overrides)
        self.__init__()  # noqa: PLC2801 — простая перезагрузка конфига по команде бота
        self.overrides = saved


_config: Config | None = None


def get_config(force_reload: bool = False) -> Config:
    global _config
    if _config is None or force_reload:
        _config = Config()
    return _config
