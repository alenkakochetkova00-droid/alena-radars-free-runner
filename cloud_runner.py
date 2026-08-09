#!/usr/bin/env python3
"""Run both Telegram radars in one small cloud container.

Each radar remains an independent child process. If one crashes, only that
process is restarted; the second radar keeps running.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("BOT_DATA_DIR", str(ROOT / ".runtime")))


async def health_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Minimal HTTP endpoint required by free container hosting."""
    try:
        await asyncio.wait_for(reader.read(4096), timeout=5)
        body = b'{"status":"ok","services":["work-radar","travel-radar"]}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required cloud secret: {name}")
    return value


def process_specs() -> list[tuple[str, list[str], Path, dict[str, str]]]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ)
    base["PYTHONUNBUFFERED"] = "1"

    work_env = dict(base)
    work_env.update(
        {
            "TELEGRAM_BOT_TOKEN": required_env("WORK_TELEGRAM_BOT_TOKEN"),
            "TELEGRAM_CHAT_ID": required_env("WORK_TELEGRAM_CHAT_ID"),
            "ORDER_RADAR_DB_PATH": str(DATA_ROOT / "work-orders.db"),
        }
    )

    travel_env = dict(base)
    travel_env.update(
        {
            "TELEGRAM_BOT_TOKEN": required_env("TRAVEL_TELEGRAM_BOT_TOKEN"),
            "TELEGRAM_CHAT_ID": required_env("TRAVEL_TELEGRAM_CHAT_ID"),
            "TRAVELPAYOUTS_TOKEN": required_env("TRAVELPAYOUTS_TOKEN"),
            "TRAVELPAYOUTS_MARKER": os.getenv("TRAVELPAYOUTS_MARKER", "").strip(),
            "DEAL_RADAR_DB_PATH": str(DATA_ROOT / "dealradar.sqlite3"),
            "DEAL_RADAR_LOG_DIR": str(DATA_ROOT / "logs"),
        }
    )

    return [
        (
            "work-radar",
            [sys.executable, "order_radar.py"],
            ROOT / "work-radar",
            work_env,
        ),
        (
            "travel-radar",
            [sys.executable, "-m", "radar.main"],
            ROOT / "travel-radar",
            travel_env,
        ),
    ]


async def supervise(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    stop: asyncio.Event,
) -> None:
    delay = 5
    process: asyncio.subprocess.Process | None = None
    try:
        while not stop.is_set():
            print(f"[{name}] starting", flush=True)
            process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env)
            code = await process.wait()
            process = None
            if stop.is_set():
                break
            print(f"[{name}] exited with code {code}; restart in {delay}s", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 60)
    finally:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except TimeoutError:
                process.kill()
                await process.wait()


async def amain() -> int:
    try:
        specs = process_specs()
    except RuntimeError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2

    port = int(os.getenv("PORT", "8080"))
    health_server = await asyncio.start_server(health_handler, "0.0.0.0", port)
    print(f"[health] listening on port {port}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(supervise(name, command, cwd, env, stop), name=name)
        for name, command, cwd, env in specs
    ]
    try:
        await stop.wait()
    finally:
        health_server.close()
        await health_server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
