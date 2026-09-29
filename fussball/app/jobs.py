"""Hintergrund-Jobs im selben Prozess wie die Web-App, mit Telegram-Nachrichten.

- Start: Telegram-Meldung; bei leerer Datenbank zuerst Historie laden (Erststart)
- alle REFRESH_HOURS (Standard 3): Daten laden, Wetten abrechnen, Tipps neu berechnen
  → Alarm bei neuen/gestrichenen Tipps, Ergebnis jeder abgerechneten Wette
- täglich um DAILY_REPORT_TIME (Europe/Zurich, Standard 09:00): Tipps des Tages
- REMINDER_MINUTES (Standard 60) vor Anpfiff: Erinnerung mit Mindestquote
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta

from fussball.app import state, telegram_bot
from fussball.data.schema import utcnow

log = logging.getLogger(__name__)
_bot = None
_reminded: set[str] = set()


async def start(engine) -> list:
    global _bot
    _bot = telegram_bot.build(engine)
    if _bot is not None:
        await _bot.initialize()
        await _bot.start()
        # Kurze Long-Polls: robuster hinter Proxys, die lange offene Verbindungen trennen.
        await _bot.updater.start_polling(drop_pending_updates=True, timeout=5, poll_interval=1.0,
                                         error_callback=lambda e: log.warning("Telegram-Polling: %s", e))
        log.info("Telegram-Bot gestartet")
        await telegram_bot.notify(_bot, "✅ <b>PeaceJudge gestartet</b>\nIch melde mich mit Tipps, Erinnerungen und "
                                        "Ergebnissen. /start zeigt alle Befehle.")
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return []
    return [asyncio.create_task(_refresh_loop(engine)), asyncio.create_task(_daily_loop()),
            asyncio.create_task(_reminder_loop())]


async def stop() -> None:
    if _bot is not None:
        await _bot.updater.stop()
        await _bot.stop()
        await _bot.shutdown()


def format_settled(details: list[dict], currency: str = "CHF") -> str:
    lines = []
    for d in details:
        icon = "✅" if d["status"] == "won" else "❌" if d["status"] == "lost" else "➖"
        clv = f" · CLV {d['clv']:+.1%}" if d.get("clv") is not None else ""
        lines.append(f"{icon} {d['text']} @ {d['odds']:.2f}: <b>{d['pnl']:+.2f} {currency}</b>{clv}")
    return "🏁 <b>Abgerechnet</b>\n" + "\n".join(lines)


async def _refresh_loop(engine):
    loop = asyncio.get_running_loop()
    await asyncio.sleep(10)
    if await loop.run_in_executor(None, state.needs_bootstrap, engine):
        await telegram_bot.notify(_bot, "⏳ Erststart: lade die Historie der Ligen (einige Minuten) …")
        try:
            res = await loop.run_in_executor(None, state.bootstrap, engine)
            ok = sum(r["status"] == "ok" for r in res)
            await telegram_bot.notify(_bot, f"📚 Historie geladen: {ok}/{len(res)} Dateien.")
        except Exception:  # noqa: BLE001
            log.exception("Erststart fehlgeschlagen")
    every = float(os.getenv("REFRESH_HOURS", "3"))
    while True:
        try:
            info = await loop.run_in_executor(None, lambda: state.refresh(engine, days=int(os.getenv("TIP_DAYS", "3"))))
            plan = state.load_plan()
            if info.get("changes"):
                await telegram_bot.notify(_bot, "🔔 <b>Tipps geändert</b>\n" + "\n".join(info["changes"])
                                          + "\n\nDetails: /heute")
            if info.get("settled_details"):
                await telegram_bot.notify(_bot, format_settled(info["settled_details"], plan.get("currency", "CHF"))
                                          + "\n\n" + telegram_bot.format_stats(engine))
            odds_err = (info.get("update") or {}).get("odds_api", {}).get("error")
            if odds_err:
                log.warning("Odds API: %s", odds_err)
        except Exception as exc:  # noqa: BLE001
            log.exception("Refresh fehlgeschlagen")
            await telegram_bot.notify(_bot, f"⚠️ Aktualisierung fehlgeschlagen: {exc!r}"[:500])
        await asyncio.sleep(every * 3600)


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
        await telegram_bot.notify(_bot, telegram_bot.format_day_combos(plan, max_days=1))
        await telegram_bot.notify(_bot, telegram_bot.format_safe(plan))
        await asyncio.sleep(60)


def due_reminders(plan: dict, now: datetime, minutes: int, already: set[str]) -> list[tuple[str, str]]:
    """(Schlüssel, Text) für Tipps, deren Anpfiff in `minutes` (±10) Minuten ist."""
    out = []
    items = [(f"S{t['match_id']}{t['market']}{t['selection']}", t) for t in plan.get("singles", [])]
    for key, t in items:
        kickoff = datetime.fromisoformat(t["kickoff"])
        if key in already or not (now + timedelta(minutes=minutes - 10) < kickoff <= now + timedelta(minutes=minutes + 10)):
            continue
        out.append((key, f"⏰ <b>Anpfiff in ~{minutes} Min.</b>\n#{t['id']} {t['match']}\n➡️ {t['label']}\n"
                         f"Nur setzen, wenn deine Quote ≥ <b>{t['min_odds']:.2f}</b> ist "
                         f"(zuletzt {t['odds']:.2f} bei {t['bookmaker']}). Einsatz {t['stake']:.2f}\n"
                         f"Gesetzt? /gesetzt {t['id']} {t['stake']:.0f} {t['odds']:.2f}"))
    return out


async def _reminder_loop():
    minutes = int(os.getenv("REMINDER_MINUTES", "60"))
    while True:
        try:
            for key, text in due_reminders(state.load_plan(), utcnow(), minutes, _reminded):
                _reminded.add(key)
                await telegram_bot.notify(_bot, text)
        except Exception:  # noqa: BLE001
            log.exception("Erinnerung fehlgeschlagen")
        await asyncio.sleep(300)
