"""Отправка сообщений в Telegram."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .util import chunk_text

log = logging.getLogger("radar.notifier")
API = "https://api.telegram.org"


class Telegram:
    def __init__(self, token: str, chat_id: str = "", parse_mode: str = "HTML") -> None:
        self.token = token
        self.chat_id = str(chat_id or "")
        self.parse_mode = parse_mode
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _call(self, method: str, payload: dict, retries: int = 3):
        if not self.token:
            log.error("Нет токена бота — сообщение не отправлено")
            return None
        assert self._session is not None, "Telegram используется вне async with"
        url = f"{API}/bot{self.token}/{method}"
        for attempt in range(retries):
            try:
                async with self._session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 429:
                        wait = int(data.get("parameters", {}).get("retry_after", 5))
                        await asyncio.sleep(wait + 1)
                        continue
                    if not data.get("ok"):
                        log.warning("Telegram %s: %s", method, data.get("description"))
                        return None
                    return data.get("result")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Telegram %s недоступен (%s), попытка %s", method, exc, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def send(self, text: str, chat_id: str | None = None,
                   disable_preview: bool = False, silent: bool = False) -> bool:
        target = str(chat_id or self.chat_id)
        if not target:
            log.error("Не задан chat_id — некому отправлять")
            return False
        ok = True
        for part in chunk_text(text):
            result = await self._call("sendMessage", {
                "chat_id": target,
                "text": part,
                "parse_mode": self.parse_mode,
                "disable_web_page_preview": disable_preview,
                "disable_notification": silent,
                "link_preview_options": {"is_disabled": disable_preview},
            })
            ok = ok and result is not None
            await asyncio.sleep(0.4)
        return ok

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        result = await self._call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }, retries=1)
        return result or []

    async def me(self) -> dict | None:
        return await self._call("getMe", {})

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self._call("setMyCommands", {
            "commands": [{"command": c, "description": d} for c, d in commands]
        })
