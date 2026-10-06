"""Hintergrund-Jobs im selben Prozess wie die Web-App, mit Telegram-Nachrichten.

- Start: Telegram-Meldung; bei leerer Datenbank zuerst Historie laden (Erststart)
- alle REFRESH_HOURS (Standard 3): Daten laden, Wetten abrechnen, Tipps neu berechnen
  → Alarm bei neuen/gestrichenen Tipps, Ergebnis jeder abgerechneten Wette
- täglich um DAILY_REPORT_TIME (Europe/Zurich, Standard 09:00): Scout prüft die Kombis (einziger
  regulärer Agentenlauf), dann nur die Tageskombi (5–6 Spiele) + Risiko-Kombi senden
- REMINDER_MINUTES (Standard 60) vor Anpfiff: Erinnerung mit Mindestquote
- nach dem ersten Durchlauf nach dem Start: Tageskombi von heute (STARTUP_TIPS=0 schaltet ab)
- Render-Gratisplan: alle 10 Minuten /healthz über die öffentliche Adresse aufrufen, damit der
  Dienst nicht nach 15 Minuten ohne Besucher einschläft (KEEP_AWAKE=0 schaltet ab)
"""

from __future__ import annotations

import asyncio
import html
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
        try:
            await telegram_bot.register_commands(_bot)
        except Exception:  # noqa: BLE001
            log.exception("Befehlsmenü konnte nicht gesetzt werden")
        # Kurze Long-Polls: robuster hinter Proxys, die lange offene Verbindungen trennen.
        await _bot.updater.start_polling(drop_pending_updates=True, timeout=5, poll_interval=1.0,
                                         error_callback=lambda e: log.warning("Telegram-Polling: %s", e))
        log.info("Telegram-Bot gestartet")
        await telegram_bot.notify(_bot, f"✅ <b>PeaceJudge gestartet</b> · Version <code>{telegram_bot.version()}</code> · /status")
    if _bot is not None:
        await _restore_costs(engine)
    if os.getenv("DISABLE_SCHEDULER") == "1":
        return []
    tasks = [asyncio.create_task(_refresh_loop(engine)), asyncio.create_task(_daily_loop(engine)),
             asyncio.create_task(_reminder_loop(engine)), asyncio.create_task(_cost_pin_loop(engine))]
    url = os.getenv("RENDER_EXTERNAL_URL")
    if url and os.getenv("KEEP_AWAKE", "1") == "1":
        tasks.append(asyncio.create_task(_keep_awake(url)))
    return tasks


async def _keep_awake(base_url: str, every_s: int = 600):
    import requests

    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(every_s)
        try:
            await loop.run_in_executor(None, lambda: requests.get(base_url.rstrip("/") + "/healthz", timeout=20))
        except Exception as exc:  # noqa: BLE001
            log.warning("Keep-awake fehlgeschlagen: %s", exc)


def _track_top(engine, plan: dict) -> None:
    """Verschickte Top-Tipps merken: nach dem letzten Spiel kommt 🟢/🔴 pro Spiel."""
    if engine is None:
        return
    from datetime import datetime

    from fussball import tracking

    try:
        day = datetime.now(state.TZ).date().isoformat()
        tips = [t for ts in telegram_bot.top_tips(plan).values() for t in ts]  # alles, was du bekommen hast
        tracking.track_tips(engine, tips, day)
    except Exception:  # noqa: BLE001
        log.exception("Top-Tipps konnten nicht gemerkt werden")


def _track_boost(engine, plan: dict) -> None:
    """Boost-Kombi merken: nach dem letzten Spiel kommt 🟢/🔴 pro Spiel."""
    if engine is None:
        return
    from datetime import datetime

    from fussball import tracking

    try:
        day = datetime.now(state.TZ).date().isoformat()
        combo = next((c for c in plan.get("boost_combos", []) if c["day"] == day), None)
        if combo:
            tracking.track_tips(engine, combo["legs"], day, batch=f"boost-{day}",
                                title=f"📋 <b>Ergebnis Boost-Kombi {day[8:10]}.{day[5:7]}.</b>")
    except Exception:  # noqa: BLE001
        log.exception("Boost-Kombi konnte nicht gemerkt werden")


async def _restore_costs(engine) -> None:
    """Tageskosten aus der angehefteten Nachricht übernehmen (überlebt Neustarts auf dem Gratisplan)."""
    from fussball.agents import runner

    owner = telegram_bot.owner_id()
    try:
        chat = await _bot.bot.get_chat(owner)
        pinned = chat.pinned_message
        usd = runner.parse_pin(pinned.text if pinned else None)
        if runner.parse_pin_daily(pinned.text if pinned else None):
            runner.mark_daily_done(engine)  # Tipps kamen heute schon – nach Neustart nicht doppelt prüfen
        if usd:
            runner.restore_carryover(engine, usd)
            log.info("Tageskosten nach Neustart übernommen: %.2f $", usd)
    except Exception:  # noqa: BLE001
        log.exception("Angeheftete Kosten-Nachricht nicht lesbar")


async def _cost_pin_loop(engine, every_s: int = 300):
    """Alle 5 Minuten: angeheftete Nachricht mit den heutigen Claude-Kosten aktualisieren."""
    from fussball.agents import runner

    owner, last = telegram_bot.owner_id(), None
    while True:
        try:
            text = runner.pin_text(engine)
            if _bot is not None and owner and text != last:
                chat = await _bot.bot.get_chat(owner)
                pinned = chat.pinned_message
                if pinned and (pinned.text or "").startswith(runner.PIN_PREFIX):
                    if pinned.text != text:
                        await _bot.bot.edit_message_text(text, chat_id=owner, message_id=pinned.message_id)
                else:
                    msg = await _bot.bot.send_message(owner, text, disable_notification=True)
                    await _bot.bot.pin_chat_message(owner, msg.message_id, disable_notification=True)
                last = text
        except Exception:  # noqa: BLE001
            log.exception("Kosten-Nachricht konnte nicht aktualisiert werden")
        await asyncio.sleep(every_s)


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
    # Beim Start keine ungeprüfte Liste mehr: die geprüften Tipps kommen vom Tageslauf (oder seinem Nachholen)
    first = os.getenv("STARTUP_TIPS", "0") == "1"
    while True:
        try:
            info = await loop.run_in_executor(None, lambda: state.refresh(engine, days=int(os.getenv("TIP_DAYS", "3"))))
            plan = state.load_plan()
            for c in info.get("combo_results", []) if os.getenv("DAILY_FULL", "0") == "1" else []:
                await telegram_bot.notify(_bot, telegram_bot.format_combo_result(c))  # nur wenn Kombis verschickt
            for title, items in info.get("tracked_results", []):
                await telegram_bot.notify(_bot, telegram_bot.format_results(items, title))
            # Nur die Spiele: Hat der Scout Spiele der heutigen Kombi ersetzt, neue Kombi senden
            if not first and any(a.get("removed") and not a.get("cached") for a in info.get("agent", [])):
                await telegram_bot.notify(_bot, "🔁 <b>Tageskombi angepasst</b> (Scout hat Spiele ersetzt)\n\n"
                                          + telegram_bot.format_today(plan))
            if info.get("settled_details"):
                await telegram_bot.notify(_bot, format_settled(info["settled_details"], plan.get("currency", "CHF"))
                                          + "\n\n" + telegram_bot.format_stats(engine))
            odds_err = (info.get("update") or {}).get("odds_api", {}).get("error")
            if odds_err:
                log.warning("Odds API: %s", odds_err)
            if first:
                first = False
                await telegram_bot.notify(_bot, telegram_bot.format_top5(plan))
                _track_top(engine, plan)
        except Exception as exc:  # noqa: BLE001
            log.exception("Refresh fehlgeschlagen")
            await telegram_bot.notify(_bot, f"⚠️ Aktualisierung fehlgeschlagen: {exc!r}"[:500])
        await asyncio.sleep(every * 3600)


def needs_catch_up(engine, when: str, now: datetime | None = None) -> bool:
    """Lief die Tagesprüfung heute noch nicht, obwohl es schon nach `when` ist (und vor CATCHUP_UNTIL)?"""
    from fussball.agents import runner

    now = now or datetime.now(state.TZ)
    h, m = map(int, when.split(":"))
    until = int(os.getenv("CATCHUP_UNTIL_HOUR", "20"))
    return time(h, m) <= now.time() and now.hour < until and runner.daily_done(engine) is None


def _seconds_until(hhmm: str) -> float:
    now = datetime.now(state.TZ)
    h, m = map(int, hhmm.split(":"))
    target = datetime.combine(now.date(), time(h, m), tzinfo=state.TZ)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_daily_lock = asyncio.Lock()


def daily_running() -> bool:
    return _daily_lock.locked()


async def run_daily_now(engine) -> None:
    """Auf Befehl (/jetzt): Prüfung sofort, auch wenn heute schon gelaufen (Tageslimit gilt weiter)."""
    try:
        await _run_daily(engine, asyncio.get_running_loop())
    except Exception as exc:  # noqa: BLE001
        log.exception("Prüfung auf Befehl fehlgeschlagen")
        await telegram_bot.notify(_bot, f"⚠️ <b>Prüfung fehlgeschlagen</b>: {html.escape(repr(exc))[:300]}")


async def _run_daily(engine, loop) -> None:
    """Quoten holen, Agenten prüfen die sicheren Tipps, Tipps verschicken (mit Start- und Fehlermeldung)."""
    async with _daily_lock:
        await _run_daily_locked(engine, loop)


async def _run_daily_locked(engine, loop) -> None:
    await telegram_bot.notify(_bot, "🔎 <b>Agenten prüfen jetzt die heutigen Spiele</b> (mehrere gleichzeitig) – "
                              "die sicheren Tipps kommen in ca. 10–20 Minuten.")
    info: dict = {}
    for _ in range(40 if engine is not None else 0):  # läuft gerade ein Refresh: kurz warten (max. 20 Min.)
        info = await loop.run_in_executor(None, lambda: state.refresh(
            engine, days=int(os.getenv("TIP_DAYS", "3")), agents=True))
        if not info.get("skipped"):
            break
        await asyncio.sleep(30)
    plan = state.load_plan()
    await telegram_bot.notify(_bot, telegram_bot.format_top5(plan))
    boost = telegram_bot.format_boost(plan) if os.getenv("DAILY_BOOST", "1") == "1" else ""
    if boost:  # 2. Nachricht: die mutige Boost-Kombi
        await telegram_bot.notify(_bot, boost)
        _track_boost(engine, plan)
    errs = [t for t in plan.get("top") or [] if t.get("agent") == "fehler"]
    if errs:  # Fehler nie verschweigen
        await telegram_bot.notify(_bot, f"⚠️ <b>Agenten-Fehler bei {len(errs)} Spiel(en)</b>\n"
                                  f"{html.escape(errs[0].get('reason', ''))[:200]}\nDetails: /status")
    if engine is not None and telegram_bot.top_tips(plan):
        # nur als erledigt merken, wenn wirklich Tipps kamen – sonst holt ein Neustart die Prüfung nach
        from fussball.agents import runner

        runner.mark_daily_done(engine)
    _track_top(engine, plan)


async def _daily_loop(engine=None):
    """Einmal täglich: Quoten holen, Scout prüft die Kombis des Tages (einziger kostenpflichtiger
    Agentenlauf – Neustarts lösen keine Agenten aus), dann die Tageskombi senden."""
    when = os.getenv("DAILY_REPORT_TIME", "09:00")
    loop = asyncio.get_running_loop()
    await asyncio.sleep(90)  # Kosten aus der angehefteten Nachricht übernehmen, erster Refresh läuft
    catch_up = engine is not None and needs_catch_up(engine, when)
    retries = 0
    while True:
        if catch_up:
            catch_up = False  # Neustart nach 09:00 (z. B. Update): heutige Prüfung jetzt nachholen
            log.info("Tagesprüfung wird nachgeholt")
        else:
            await asyncio.sleep(_seconds_until(when))
        try:
            await _run_daily(engine, loop)
        except Exception as exc:  # noqa: BLE001 – nie still abbrechen
            log.exception("Tageslauf fehlgeschlagen")
            await telegram_bot.notify(_bot, f"⚠️ <b>Tagesprüfung fehlgeschlagen</b>: {html.escape(repr(exc))[:300]}\n"
                                      "Details: /status")
            retries += 1
            if retries <= 2:  # nach 10 Minuten nochmals versuchen, damit heute doch noch Tipps kommen
                catch_up = True
                await asyncio.sleep(600)
            continue
        retries = 0
        plan = state.load_plan()
        if os.getenv("DAILY_EXTRA", "0") == "1":  # Torfest nur auf Wunsch (sonst /torfest)
            await telegram_bot.notify(_bot, telegram_bot.format_boost(
                plan, "torfest_combos", "⚡ <b>Torfest-Kombi heute</b> (2 Tore vor der Pause, torreichste Spiele)"))
        if os.getenv("DAILY_FULL", "0") == "1":  # ausführliche Kombis nur auf Wunsch, sonst /tageskombi
            await telegram_bot.notify(_bot, "☀️ <b>Tageskombi heute</b>\n\n" + telegram_bot.format_today(plan))
        # Kosten stehen in der angehefteten Nachricht 📌 und unter /kosten – keine Extra-Nachricht

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


LINEUP_SOURCES: tuple[str, ...] = ()  # Kombis nur mit LINEUP_COMBOS=1 – Budget für die sicheren Tipps


def due_lineup_checks(plan: dict, now: datetime, minutes: int, already: set[str]) -> list[dict]:
    """Alle Spiele mit einem Bot-Tipp (Kombis + Top-Tipps), deren Anpfiff in ~`minutes` Minuten ist:
    offizielle Aufstellung prüfen. Ein Spiel nur einmal (Tipp der sichersten Kombi zuerst)."""
    legs: dict[int, dict] = {}
    for src in LINEUP_SOURCES + (("day_combos",) if os.getenv("LINEUP_COMBOS", "0") == "1" else ()):
        for c in plan.get(src, []):
            for l in c["legs"]:
                legs.setdefault(l["match_id"], l)
    for t in plan.get("top") or []:  # nur die von beiden Agenten bestätigten sicheren Tipps
        if t.get("agent") == "bestätigt":
            legs.setdefault(t["match_id"], t)
    out = []
    for mid, leg in legs.items():
        kickoff = datetime.fromisoformat(leg["kickoff"])
        key = f"L{mid}"
        if key not in already and now + timedelta(minutes=minutes - 10) < kickoff <= now + timedelta(minutes=minutes + 10):
            out.append(leg)
    return out


def lineup_alert(leg: dict, res: dict) -> str | None:
    """Kurze Warnung, wenn der Scout nach der Aufstellung „vorsicht“ oder „streichen“ meldet."""
    if res.get("assessment") not in ("vorsicht", "streichen"):
        return None
    head = "❌ <b>Nicht setzen</b>" if res["assessment"] == "streichen" else "🔴 <b>Vorsicht</b>"
    k = state.local(leg["kickoff"]).strftime("%H:%M")
    reason = html.escape(res.get("reason") or "")
    return (f"📋 <b>Aufstellungs-Check</b>\n<b>{k} {html.escape(leg['match'])}</b>\n"
            f"➡️ <code>{html.escape(leg['label'])}</code>\n{head}" + (f": <i>{reason}</i>" if reason else ""))


async def _reminder_loop(engine=None):
    minutes = int(os.getenv("REMINDER_MINUTES", "60"))
    lineup_min = int(os.getenv("LINEUP_CHECK_MINUTES", "75"))
    loop = asyncio.get_running_loop()
    while True:
        try:
            plan = state.load_plan()
            for key, text in due_reminders(plan, utcnow(), minutes, _reminded):
                _reminded.add(key)
                await telegram_bot.notify(_bot, text)
            if engine is not None:
                from fussball.agents import runner

                for leg in due_lineup_checks(plan, utcnow(), lineup_min, _reminded):
                    _reminded.add(f"L{leg['match_id']}")
                    res = await loop.run_in_executor(None, lambda leg=leg: runner.analyze_legs(
                        engine, [leg], max_age_h=0.3, local_time=state.local, max_searches=4))
                    text = lineup_alert(leg, res[0]) if res else None
                    if text:
                        await telegram_bot.notify(_bot, text)
        except Exception:  # noqa: BLE001
            log.exception("Erinnerung/Aufstellungs-Check fehlgeschlagen")
        await asyncio.sleep(300)
