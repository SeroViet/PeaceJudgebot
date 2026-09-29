"""Telegram-Bot: antwortet nur dem Besitzer (TELEGRAM_OWNER_ID).

Befehle: /heute, /kombi, /spiel <Team>, /bilanz, /gesetzt <Tipp-Nr> <Einsatz> <Quote>, /update, /id
"""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from fussball import service
from fussball.app import state

log = logging.getLogger(__name__)


def owner_id() -> int | None:
    raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def _fmt_time(iso: str) -> str:
    return state.local(iso).strftime("%a %d.%m. %H:%M")


def format_singles(plan: dict) -> str:
    cur = plan.get("currency", "CHF")
    if not plan["singles"]:
        return "Heute kein Einzeltipp mit genügend Value. Kein Tipp ist auch ein Tipp. 🧘"
    lines = ["<b>Einzeltipps</b>"]
    for t in plan["singles"]:
        lines.append(
            f"\n<b>#{t['id']}</b> {t['match']} ({t['comp']}, {_fmt_time(t['kickoff'])})\n"
            f"➡️ {t['label']}\n"
            f"Quote Ø {t['odds']:.2f} · <b>Mindestquote {t['min_odds']:.2f}</b>\n"
            f"Wahrsch. {t['prob']:.0%} · Edge {t['edge']:+.1%} · Einsatz {t['stake']:.2f} {cur}"
        )
    return "\n".join(lines)


def format_safe(plan: dict, limit: int = 15) -> str:
    safe = plan.get("safe", [])
    if not safe:
        return "Aktuell keine Sicher-Tipps (es fehlen Pinnacle-Quoten für die nächsten Spiele)."
    lines = ["<b>🎯 Sicher-Tipps</b> (Trefferwahrscheinlichkeit 70–90 %)",
             "<i>Historisch: 78 % erwartet → 78 % getroffen (170 000 Tipps, 2020–2026)</i>"]
    for t in safe[:limit]:
        lines.append(f"\n<b>{t['id']}</b> {t['match']} ({t['comp']}, {_fmt_time(t['kickoff'])})\n"
                     f"➡️ {t['label']}\n"
                     f"Wahrscheinlichkeit <b>{t['prob']:.0%}</b> · faire Quote {t['fair_odds']:.2f} "
                     f"→ bei Sporttip nur spielen, wenn Quote ≥ {t['fair_odds']:.2f}")
    if len(safe) > limit:
        lines.append(f"\n… und {len(safe) - limit} weitere in der App.")
    return "\n".join(lines)


def _find_legs(plan: dict, tip_id: str) -> list[dict] | None:
    from fussball.service import split_market

    tid = tip_id.upper()
    if tid.startswith("K"):
        c = next((c for c in plan["combos"] if c["id"] == tid), None)
        return c["legs"] if c else None
    if tid.startswith("T"):
        c = next((c for c in plan.get("day_combos", []) if c["id"] == tid), None)
        if not c:
            return None
        return [{**l, "market": split_market(l["market"])[0], "line": split_market(l["market"])[1]} for l in c["legs"]]
    if tid.startswith("S"):
        t = next((t for t in plan.get("safe", []) if t["id"] == tid), None)
        if not t:
            return None
        market, line = split_market(t["market"])
        return [{**t, "market": market, "line": line}]
    t = next((t for t in plan["singles"] if t["id"] == tip_id), None)
    return [t] if t else None


DAY_BT = {"5": "46 % aufgegangen, Ø 4,3 von 5 richtig", "6": "41 % aufgegangen, Ø 5,2 von 6 richtig"}


def format_day_combos(plan: dict, max_days: int = 2) -> str:
    combos = plan.get("day_combos", [])
    if not combos:
        return "Keine Tageskombi möglich (zu wenige Spiele mit Pinnacle-Quoten an einem Tag)."
    days = sorted({c["day"] for c in combos})[:max_days]
    out = []
    for day in days:
        for c in [c for c in combos if c["day"] == day]:
            d = state.local(c["legs"][0]["kickoff"]).strftime("%a %d.%m.")
            legs = "\n".join(f"  {i}. {state.local(l['kickoff']).strftime('%H:%M')} {l['match']}\n"
                              f"     ➡️ <b>{l['label']}</b> ({l['prob']:.0%})" for i, l in enumerate(c["legs"], 1))
            out.append(f"🎯 <b>{c['id']} · {c['size']}er-Tageskombi {d}</b>\n{legs}\n"
                       f"Trefferchance gesamt <b>{c['prob']:.0%}</b> · faire Gesamtquote <b>{c['fair_odds']:.2f}</b>\n"
                       f"Nur spielen, wenn Sporttip ≥ {c['fair_odds']:.2f} zahlt.\n"
                       f"<i>Backtest {c['size']}er: {DAY_BT.get(str(c['size']), '')}</i>")
    return "\n\n".join(out)


def format_combos(plan: dict) -> str:
    cur = plan.get("currency", "CHF")
    if not plan["combos"]:
        return "Keine Kombi erfüllt heute die Regeln (nur Tipps mit eigenem Value)."
    out = []
    for c in plan["combos"]:
        legs = "\n".join(f"  • {l['match']}: {l['label']} (min. {l['min_odds']:.2f})" for l in c["legs"])
        out.append(
            f"<b>{c['id']} · {c['variant']}</b> – Quote {c['odds']:.2f}\n{legs}\n"
            f"Trefferchance {c['prob']:.1%} · EV {c['ev']:+.1%} · Einsatz {c['stake']:.2f} {cur}\n"
            f"⚠️ 5 Kombis in Folge verloren: {c['streak5']:.0%}"
        )
    return "\n\n".join(out)


def format_match(plan: dict, query: str) -> str:
    q = query.lower()
    hits = [f for f in plan["forecasts"] if q in f["home"].lower() or q in f["away"].lower()]
    if not hits:
        return f"Kein Spiel mit „{query}“ in den nächsten Tagen."
    out = []
    for f in hits[:3]:
        p, o = f["probs_1x2"], f["probs_ou"]
        avg = f["odds"].get("avg", {})
        out.append(
            f"<b>{f['home']} – {f['away']}</b> ({f['comp_name']}, {_fmt_time(f['kickoff_utc'])})\n"
            f"Erwartete Tore {f['lam']:.2f} : {f['mu']:.2f}\n"
            f"1 {p['H']:.0%} (fair {1/p['H']:.2f}, Ø {avg.get('H', 0):.2f})\n"
            f"X {p['D']:.0%} (fair {1/p['D']:.2f}, Ø {avg.get('D', 0):.2f})\n"
            f"2 {p['A']:.0%} (fair {1/p['A']:.2f}, Ø {avg.get('A', 0):.2f})\n"
            f"Über 2.5 {o['O']:.0%} · Unter 2.5 {o['U']:.0%}\n"
            + "\n".join(f"• {x}" for x in f["factors"][1:])
        )
    return "\n\n".join(out)


def format_stats(engine) -> str:
    s = service.bet_stats(engine)
    clv = f"{s['clv']:+.1%}" if s["clv"] is not None else "–"
    lines = [f"<b>Bilanz</b>\nBankroll {s['bankroll']:.2f} · P/L {s['pnl']:+.2f}",
             f"ROI {s['roi']:+.1%} · Treffer {s['hit']:.0%} · CLV {clv}", f"Abgerechnet {s['n']} · offen {s['open']}"]
    for t, v in s["by_type"].items():
        lines.append(f"{'Einzel' if t == 'single' else 'Kombi'}: {v['n']} Wetten, P/L {v['pnl']:+.2f}, ROI {v['roi']:+.1%}")
    return "\n".join(lines)


def build(engine) -> Application | None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        log.info("TELEGRAM_TOKEN fehlt – Bot deaktiviert")
        return None
    app = ApplicationBuilder().token(token).build()
    owner = owner_id()
    only_owner = filters.User(user_id=owner) if owner else filters.User(user_id=[])

    async def reply(update: Update, text: str):
        for chunk in [text[i : i + 3900] for i in range(0, len(text), 3900)] or [""]:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def cmd_id(update: Update, _ctx):
        await update.effective_message.reply_text(
            f"Deine Telegram-ID: {update.effective_user.id}\n"
            "Als TELEGRAM_OWNER_ID eintragen, damit der Bot nur dir antwortet.")

    async def cmd_start(update: Update, _ctx):
        if owner is None or update.effective_user.id != owner:
            await update.effective_message.reply_text(
                f"👋 Deine Telegram-ID: {update.effective_user.id}\n"
                "Trage sie beim Server als TELEGRAM_OWNER_ID ein und starte den Dienst neu. "
                "Danach antworte ich nur noch dir.")
            return
        await reply(update, "👋 PeaceJudge ist bereit.\n/heute – Tipps\n/kombi – Kombis\n/spiel Team – Prognose\n"
                            "/tageskombi – 5er/6er-Kombi, alle Spiele am selben Tag\n"
                            "/sicher – Tipps mit hoher Trefferquote (Über/Unter, 1X …)\n"
                            "/bilanz – Bilanz\n/gesetzt Nr Einsatz Quote – Wette erfassen (z. B. /gesetzt S3 10 1.45)\n"
                            "/update – neu berechnen")

    async def cmd_today(update: Update, _ctx):
        plan = state.load_plan()
        await reply(update, format_singles(plan))
        await reply(update, format_safe(plan))

    async def cmd_day(update: Update, _ctx):
        await reply(update, format_day_combos(state.load_plan()))

    async def cmd_safe(update: Update, _ctx):
        await reply(update, format_safe(state.load_plan(), limit=40))

    async def cmd_combo(update: Update, _ctx):
        await reply(update, format_combos(state.load_plan()))

    async def cmd_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await reply(update, "Beispiel: /spiel Bayern")
            return
        await reply(update, format_match(state.load_plan(), " ".join(ctx.args)))

    async def cmd_stats(update: Update, _ctx):
        await reply(update, format_stats(engine))

    async def cmd_placed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            tip_id, stake, odds = ctx.args[0], float(ctx.args[1].replace(",", ".")), float(ctx.args[2].replace(",", "."))
        except (IndexError, ValueError):
            await reply(update, "Format: /gesetzt 2 10 2.15  ·  /gesetzt S3 10 1.45  ·  /gesetzt K1 5 12.40")
            return
        legs = _find_legs(state.load_plan(), tip_id)
        if not legs:
            await reply(update, f"Tipp {tip_id} nicht gefunden.")
            return
        service.record_bet(engine, legs, stake, odds)
        await reply(update, f"✅ Erfasst: {tip_id}, Einsatz {stake:.2f} @ {odds:.2f}")

    async def cmd_update(update: Update, _ctx):
        await reply(update, "⟳ Aktualisiere Daten und Prognosen …")
        info = await asyncio.get_running_loop().run_in_executor(None, lambda: state.refresh(engine))
        changes = info.get("changes") or ["keine Änderungen bei den Tipps"]
        await reply(update, "Fertig.\n" + "\n".join(changes))

    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    for name, fn in (("heute", cmd_today), ("sicher", cmd_safe), ("tageskombi", cmd_day), ("kombi", cmd_combo), ("spiel", cmd_match),
                     ("bilanz", cmd_stats), ("gesetzt", cmd_placed), ("update", cmd_update)):
        app.add_handler(CommandHandler(name, fn, filters=only_owner))
    return app


async def notify(app: Application | None, text: str) -> None:
    owner = owner_id()
    if app is None or owner is None or not text:
        return
    try:
        for chunk in [text[i : i + 3900] for i in range(0, len(text), 3900)]:
            await app.bot.send_message(owner, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("Telegram-Nachricht fehlgeschlagen")
