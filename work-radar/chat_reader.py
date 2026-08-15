#!/usr/bin/env python3
"""Чтение публичных Telegram-чатов через официальный API (MTProto).

Каналы читаются по открытым страницам t.me/s/<канал>, но у групп такой страницы
нет: историю чата отдаёт только клиентский API Telegram. Поэтому здесь Telethon.

Читаем только те чаты, в которых состоит владелец аккаунта, и только сообщения —
ничего не пишем и никуда не вступаем автоматически. Автор сообщения в публичной
группе и есть контакт заказчика.

Модуль необязательный: если Telethon не установлен или сессия не задана, радар
работает на остальных источниках без ошибок.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

LOG = logging.getLogger("order-radar.chats")

SESSION_ENV = "TELEGRAM_SESSION"
API_ID_ENV = "TELEGRAM_API_ID"
API_HASH_ENV = "TELEGRAM_API_HASH"


class ChatReaderUnavailable(RuntimeError):
    """Чтение чатов не настроено — это не ошибка, а отключённая возможность."""


def is_configured() -> bool:
    return all(os.getenv(name) for name in (SESSION_ENV, API_ID_ENV, API_HASH_ENV))


def _client():
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ChatReaderUnavailable("не установлен telethon") from exc
    if not is_configured():
        raise ChatReaderUnavailable(
            f"не заданы {SESSION_ENV}, {API_ID_ENV} и {API_HASH_ENV}"
        )
    return TelegramClient(
        StringSession(os.environ[SESSION_ENV]),
        int(os.environ[API_ID_ENV]),
        os.environ[API_HASH_ENV],
    )


async def _collect(chats: list[str], max_age_minutes: int, per_chat: int) -> list[dict[str, Any]]:
    from telethon.errors import (  # локальный импорт: модуль необязательный
        ChannelPrivateError,
        FloodWaitError,
        UsernameNotOccupiedError,
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    collected: list[dict[str, Any]] = []
    client = _client()
    await client.start()
    try:
        for chat in chats:
            try:
                async for message in client.iter_messages(chat, limit=per_chat):
                    if not message.text or message.date < since:
                        break
                    sender = await message.get_sender()
                    handle = getattr(sender, "username", None)
                    collected.append(
                        {
                            "chat": chat,
                            "id": message.id,
                            "text": message.text,
                            "date": message.date,
                            "contact": f"@{handle}" if handle else "",
                            "url": f"https://t.me/{chat.lstrip('@')}/{message.id}",
                        }
                    )
            except FloodWaitError as exc:
                LOG.warning("Telegram просит подождать %s с — прекращаем обход чатов", exc.seconds)
                break
            except (ChannelPrivateError, UsernameNotOccupiedError, ValueError) as exc:
                LOG.warning("Чат %s недоступен: %s", chat, exc)
            except Exception as exc:  # чтобы один плохой чат не ронял весь цикл
                LOG.warning("Чат %s пропущен: %s", chat, exc)
    finally:
        await client.disconnect()
    return collected


def read_chats(chats: list[str], max_age_minutes: int = 60, per_chat: int = 40) -> list[dict[str, Any]]:
    """Свежие сообщения из перечисленных чатов.

    Возвращает список словарей; превращение в Listing остаётся на стороне радара,
    чтобы этот модуль не зависел от его структур.
    """
    if not chats:
        return []
    return asyncio.run(_collect(chats, max_age_minutes, per_chat))
