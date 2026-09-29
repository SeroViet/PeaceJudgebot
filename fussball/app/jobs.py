"""Hintergrund-Jobs im selben Prozess wie die Web-App.

- alle 3 Stunden: Daten laden, Wetten abrechnen, Prognosen neu berechnen;
  Telegram-Alarm, wenn Tipps neu dazukommen oder gestrichen werden
- täglich um DAILY_REPORT_TIME (Europe/Zurich, Standard 09:00): Tagesübersicht
- nach Abrechnung: Ergebnis der abgerechneten Wetten
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta

from fussball.app import state, telegram_bot

log = logging.getLogger(__name__)
_bot = None


async def start(engine) -> list:
    global _bot
    _bot = telegram_bot.build(engine)
    if _bot is not None:
        await _bot.initialize()
        await _bot.start()
        await _bot.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram-Bot gestartet")
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return []
    return [asyncio.create_task(_refresh_loop(engine)), asyncio.create_task(_daily_loop())]


async def stop() -> None:
    if _bot is not None:
        await _bot.updater.stop()
        await _bot.stop()
        await _bot.shutdown()


async def _refresh_loop(engine, every_hours: float = 3.0):
    await asyncio.sleep(20)
    while True:
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, lambda: state.refresh(engine))
            if info.get("changes"):
                await telegram_bot.notify(_bot, "🔔 <b>Tipps geändert</b>\n" + "\n".join(info["changes"]))
            if info.get("settled"):
                await telegram_bot.notify(_bot, f"🏁 {info['settled']} Wette(n) abgerechnet.\n\n"
                                          + telegram_bot.format_stats(engine))
        except Exception:  # noqa: BLE001
            log.exception("Refresh fehlgeschlagen")
        await asyncio.sleep(every_hours * 3600)


def _seconds_until(hhmm: str) -> float:
    now = datetime.now(state.TZ)
    h, m = map(int, hhmm.split(":"))
    target = datetime.combine(now.date(), time(h, m), tzinfo=state.TZ)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _daily_loop():
    when = os.getenv("DAILY_REPORT_TIME", "09:00")
    while True:
        await asyncio.sleep(_seconds_until(when))
        plan = state.load_plan()
        await telegram_bot.notify(_bot, "☀️ <b>Tipps des Tages</b>\n\n" + telegram_bot.format_singles(plan)
                                  + "\n\n" + telegram_bot.format_combos(plan))
        await asyncio.sleep(60)
