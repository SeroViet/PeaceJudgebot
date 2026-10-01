"""Telegram-Bot: antwortet nur dem Besitzer (TELEGRAM_OWNER_ID).

Befehle: /heute, /kombi, /spiel <Team>, /bilanz, /gesetzt <Tipp-Nr> <Einsatz> <Quote>, /update, /id
"""

from __future__ import annotations

import asyncio
import html
import logging
import os

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from fussball import service
from fussball.app import state

log = logging.getLogger(__name__)


def owner_id() -> int | None:
    raw = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _fmt_day(iso: str) -> str:
    d = state.local(iso)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m.}"


def _fmt_time(iso: str) -> str:
    return f"{_fmt_day(iso)} {state.local(iso):%H:%M}"


def format_singles(plan: dict) -> str:
    cur = plan.get("currency", "CHF")
    if not plan["singles"]:
        return "Heute kein Einzeltipp mit genügend Value. Kein Tipp ist auch ein Tipp. 🧘"
    lines = ["<b>Einzeltipps</b>"]
    for t in plan["singles"]:
        lines.append(
            f"\n<b>#{t['id']} {_fmt_time(t['kickoff'])} {t['match']}</b> <i>({t['comp']})</i>\n"
            f"➡️ {tip(t['label'])}\n"
            f"Quote Ø {t['odds']:.2f} · <b>Mindestquote {t['min_odds']:.2f}</b>\n"
            f"Wahrsch. {t['prob']:.0%} · Edge {t['edge']:+.1%} · Einsatz {t['stake']:.2f} {cur}"
        )
    return "\n".join(lines)


def tip(label: str) -> str:
    """Tipp hervorheben: Telegram kennt keine Textfarben – Code-Schrift wird in der App farbig und
    in eigener Schrift angezeigt (und lässt sich antippen/kopieren)."""
    return f"<code>{html.escape(label)}</code>"


def format_safe(plan: dict, limit: int = 15) -> str:
    safe = plan.get("safe", [])
    if not safe:
        return "Aktuell keine Sicher-Tipps (es fehlen Pinnacle-Quoten für die nächsten Spiele)."
    lines = ["<b>🎯 Sicher-Tipps</b> (Trefferwahrscheinlichkeit 70–90 %)",
             "<i>Historisch: 78 % erwartet → 78 % getroffen (170 000 Tipps, 2020–2026)</i>"]
    for t in safe[:limit]:
        lines.append(f"\n<b>{t['id']} {_fmt_time(t['kickoff'])} {t['match']}</b> <i>({t['comp']})</i>\n"
                     f"➡️ {tip(t['label'])}\n"
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
    if tid.startswith(("T", "R", "X")):
        c = next((c for c in plan.get("day_combos", []) + plan.get("risky_combos", []) + plan.get("krass_combos", []) if c["id"] == tid), None)
        if not c:
            return None
        out = []
        for l in c["legs"]:
            if l["market"] == "BB":  # BetBuilder: jeder Teil-Tipp wird als eigenes Leg abgerechnet
                out += [{**l, "market": m, "selection": sel, "line": line} for m, sel, line, _half in l["parts"]]
            else:
                out.append({**l, "market": split_market(l["market"])[0], "line": split_market(l["market"])[1]})
        return out
    if tid.startswith("S"):
        t = next((t for t in plan.get("safe", []) if t["id"] == tid), None)
        if not t:
            return None
        market, line = split_market(t["market"])
        return [{**t, "market": market, "line": line}]
    t = next((t for t in plan["singles"] if t["id"] == tip_id), None)
    return [t] if t else None


DAY_BT = {"3": "geht an ca. 6 von 10 Tagen auf (hochgerechnet aus 83 % pro Tipp)",
          "5": "ging an 46 von 100 Tagen auf", "6": "ging an 41 von 100 Tagen auf"}
# Einsatz-Empfehlung in % der eigenen Wettkasse: je unsicherer, desto kleiner
STAKE_PCT = {"3": 2.0, "5": 1.0, "6": 1.0, "risky": 0.5, "krass": 0.25}
BUILDER_HOWTO = ("🧩 So setzen: bei Sporttip im Spiel auf „BetBuilder“ die Tipps eines Spiels zusammenstellen, "
                 "dann die BetBuilder auf einen Schein. Geht das nicht: jeden BetBuilder einzeln.")
KRASS_BT = ("Krass: 3 BetBuilder – Tipps, die oft gemeinsam eintreten, exakt aus allen Endständen gerechnet. "
            "Geht etwa an 1 von 10 Tagen auf. Nur Mini-Einsatz.\n" + BUILDER_HOWTO)
RISKY_BT = ("Risiko: 2 BetBuilder, exakt aus allen Endständen gerechnet. Geht etwa an 1 von 5 Tagen auf. "
            "Nur kleiner Einsatz.\n" + BUILDER_HOWTO)


def _leg_odds(l: dict) -> str:
    """Quoten-Zeile eines Legs: Sporttip-Mindestquote (= faire Quote) und Live-Quote der Buchmacher."""
    if l.get("market") == "BB":
        parts = [f"BetBuilder fair <b>{1 / l['prob']:.2f}</b>"]
        if l.get("lift", 0) > 1.05:
            parts.append(f"🔗 treten {l['lift']:.1f}× öfter gemeinsam ein")
    else:
        parts = [f"Sporttip mind. <b>{1 / l['prob']:.2f}</b>"]
    if l.get("sporttip_est"):
        ok = "✅" if l["sporttip_est"] >= 1 / l["prob"] else "❌"
        parts.append(f"Sporttip ca. {l['sporttip_est']:.2f} {ok}")
    if l.get("book_odds"):
        parts.append(f"live {l['book_odds']:.2f} ({l['book']})")
    if l.get("ps_odds"):
        parts.append(f"Pinnacle {l['ps_odds']:.2f}")
    return "💶 " + " · ".join(parts)


def _odds_time(legs: list[dict]) -> str:
    times = [l["odds_at"] for l in legs if l.get("odds_at")]
    return f" <i>(Stand {state.local(min(times)).strftime('%d.%m. %H:%M')})</i>" if times else ""


def check_sporttip(plan: dict, combo_id: str, quotes: list[float]) -> str:
    """Sporttip-Quoten mit den fairen Quoten der Tageskombi vergleichen.
    Eine Zahl = Gesamtquote der Kombi; sonst eine Quote pro Spiel (in der Reihenfolge der Nachricht)."""
    c = next((c for c in plan.get("day_combos", []) + plan.get("risky_combos", []) + plan.get("krass_combos", [])
              if c["id"].upper() == combo_id.upper()), None)
    if c is None:
        return f"Kombi {combo_id} nicht gefunden. Die aktuelle Nummer steht in /tageskombi (z. B. T1)."
    legs = c["legs"]
    lines = [f"🇨🇭 <b>Sporttip-Check {c['id']}</b> ({c['size']}er)"]
    if len(quotes) == 1:
        total = quotes[0]
    elif len(quotes) == len(legs):
        total = 1.0
        for i, (l, q) in enumerate(zip(legs, quotes), 1):
            fair = 1 / l["prob"]
            total *= q
            ok = "✅" if q >= fair else "❌"
            lines.append(f"{ok} {i}. <b>{l['match']}</b> · {tip(l['label'])}: Sporttip {q:.2f} / fair {fair:.2f} "
                         f"({q / fair - 1:+.0%})")
    else:
        return (f"Bitte entweder 1 Zahl (Gesamtquote) oder {len(legs)} Quoten (eine pro Spiel) eingeben.\n"
                f"Beispiel: /sporttip {c['id']} " + " ".join(["1.30"] * len(legs)))
    value = total / c["fair_odds"] - 1
    lines.append(f"\nSporttip gesamt <b>{total:.2f}</b> · fair {c['fair_odds']:.2f} → <b>{value:+.1%}</b>")
    if value >= 0:
        lines.append("✅ <b>Gute Quote</b>: Sporttip zahlt mindestens die faire Quote. Spielbar.")
    elif value >= -0.05:
        lines.append("⚠️ Knapp unter fair: auf lange Sicht leicht im Minus. Wenn, dann nur kleiner Einsatz.")
    else:
        lines.append("❌ <b>Zu tiefe Quote</b>: Sporttip zahlt deutlich zu wenig. Auf lange Sicht Verlust – besser "
                     "einzelne Spiele mit ❌ weglassen oder nicht spielen.")
    return "\n".join(lines)


def format_slip(rows: list[dict], total_odds: float | None, learned: int) -> str:
    """Antwort auf einen Sporttip-Screenshot: jede Wette (auch BetBuilder, Handicap, 1. HZ, Boost) gegen
    die faire Quote. 💎 = Sporttip zahlt mehr als fair (Value)."""
    lines = ["🇨🇭 <b>Sporttip-Schein geprüft</b>"]
    fair_total, st_total, complete, value = 1.0, 1.0, True, []
    for r in rows:
        leg = r["leg"]
        mark = ("🚀 " if leg.boosted else "") + ("🧩 " if len(leg.parts) > 1 else "")
        if r["fair"] is None:
            complete = False
            lines.append(f"❔ {mark}<b>{r['match']}</b> · {tip(leg.market_text)} @ {leg.odds:.2f} – nicht berechenbar")
            continue
        fair_total *= r["fair"]
        st_total *= leg.odds
        edge = leg.odds / r["fair"] - 1
        icon = "💎" if edge >= 0.02 else "✅" if edge >= 0 else "⚠️" if edge >= -0.05 else "❌"
        if edge >= 0.02:
            value.append((edge, r))
        lines.append(f"{icon} {mark}<b>{r['match']}</b> · {tip(leg.market_text)}: Sporttip <b>{leg.odds:.2f}</b> / "
                     f"fair {r['fair']:.2f} (<b>{edge:+.0%}</b>) · Chance {r['prob']:.0%}")
    priced = [r for r in rows if r["fair"]]
    if value:
        lines.append("\n💎 <b>VALUE gefunden</b> – hier zahlt Sporttip mehr als fair:")
        for edge, r in sorted(value, key=lambda x: -x[0])[:5]:
            lines.append(f"  • <b>{r['match']}</b> · {tip(r['leg'].market_text)} @ {r['leg'].odds:.2f} ({edge:+.0%})")
        lines.append("  <i>Am besten einzeln spielen, kleiner Einsatz (1–2 % der Wettkasse).</i>")
    if len(priced) > 1 and complete and total_odds:
        v = total_odds / fair_total - 1
        verdict = ("✅ <b>Gute Quote</b> – spielbar." if v >= 0 else
                   "⚠️ Knapp unter fair – wenn, dann nur kleiner Einsatz." if v >= -0.05 else
                   "❌ <b>Zu tief</b> – Sporttip zahlt zu wenig; Wetten mit ❌ weglassen.")
        lines.append(f"\nWettschein: Sporttip <b>{total_odds:.2f}</b> · fair {fair_total:.2f} → <b>{v:+.1%}</b>\n"
                     f"{verdict}\nTrefferchance gesamt {1 / fair_total:.0%}")
    elif not value and priced:
        lines.append("\nKein Value auf diesem Bild – Sporttip zahlt überall weniger als fair.")
    if learned:
        lines.append(f"\n📚 {learned} Quote(n) gelernt – damit schätze ich Sporttip-Quoten in den Tipps.")
    return "\n".join(lines)


def sporttip_estimate(c: dict) -> float | None:
    """Geschätzte Sporttip-Gesamtquote einer Kombi (aus gelernten Screenshots), falls für alle Legs bekannt."""
    if not c["legs"] or not all(l.get("sporttip_est") for l in c["legs"]):
        return None
    est = 1.0
    for l in c["legs"]:
        est *= l["sporttip_est"]
    return est


def format_day_combos(plan: dict, max_days: int = 2) -> str:
    combos = plan.get("day_combos", [])
    if not combos:
        return "Keine Tageskombi möglich (zu wenige Spiele mit Pinnacle-Quoten an einem Tag)."
    from fussball.service import CATEGORIES

    days = sorted({c["day"] for c in combos})[:max_days]
    order = list(CATEGORIES)
    kind = lambda c: 2 if c.get("krass") else 1 if c.get("risky") else 0  # noqa: E731
    out = []
    for day in days:
        day_combos = sorted([c for c in combos if c["day"] == day],
                            key=lambda c: (order.index(c.get("cat", "liga")), kind(c), c["size"]))
        shown_cat = None
        for c in day_combos:
            cat = c.get("cat", "liga")
            if cat != shown_cat and len({x.get("cat", "liga") for x in combos}) > 1:
                out.append(f"━━━━━━━━━━━━━━━\n<b>{CATEGORIES[cat]}</b>")
                shown_cat = cat
            d = _fmt_day(c["legs"][0]["kickoff"])
            icon = {"bestätigt": " ✅", "vorsicht": " ⚠️", "streichen": " ❌"}
            legs = "\n".join(f"  {i}. <b>{state.local(l['kickoff']).strftime('%H:%M')} {l['match']}</b>"
                              f" <i>({l.get('comp_name') or l.get('comp', '')})</i>\n"
                              f"     {'🧩' if l.get('market') == 'BB' else '➡️'} {tip(l['label'])} <b>({l['prob']:.0%})</b>"
                              f"{icon.get((l.get('agent') or {}).get('assessment'), '')}"
                              + (f"\n     <i>🔄 vom Scout gewählt statt „{l['switched_from']}“</i>"
                                 if l.get("switched_from") else "")
                              + f"\n     {_leg_odds(l)}"
                              + (f"\n     <i>⚠️ {l['agent']['reason']}</i>"
                                 if (l.get("agent") or {}).get("assessment") == "vorsicht" else "")
                              for i, l in enumerate(c["legs"], 1))
            live = (f"Live-Gesamtquote (beste Buchmacher): <b>{c['book_odds']:.2f}</b>"
                    f"{_odds_time(c['legs'])}\n" if c.get("book_odds") else "")
            est = sporttip_estimate(c)
            if est is not None:
                live += (f"Sporttip geschätzt (aus deinen Screenshots): <b>{est:.2f}</b> "
                         f"{'✅' if est >= c['fair_odds'] else '❌ unter fair'}\n")
            wide = ("\n<i>Heute gibt es nicht genug Spiele im Bereich 75–88 %, darum etwas "
                    "breiter gewählt.</i>" if c.get("widened") else "")
            if c.get("krass"):
                head = f"🔥 <b>{c['id']} · Krass-Kombi {d}</b> ({c['size']} BetBuilder, Quote ca. {c['fair_odds']:.0f})"
                bt, pct = KRASS_BT, STAKE_PCT["krass"]
            elif c.get("risky"):
                head = (f"🎲 <b>{c['id']} · Risiko-Kombi {d}</b> ({c['size']} BetBuilder, Quote ca. "
                        f"{c['fair_odds']:.1f})" if c.get("builder") else
                        f"🎲 <b>{c['id']} · Risiko-Kombi {d}</b> ({c['size']} Spiele, höhere Quote)")
                bt, pct = RISKY_BT, STAKE_PCT["risky"]
            else:
                head = f"🎯 <b>{c['id']} · {c['size']}er-Tageskombi {d}</b>"
                bt = f"Backtest {c['size']}er: {DAY_BT.get(str(c['size']), '')}"
                pct = STAKE_PCT.get(str(c["size"]), 1.0)
            bt += f"\n💰 Einsatz: höchstens {pct:g} % deiner Wettkasse (bei 200 CHF = {2 * pct:.2f} CHF)"
            out.append(f"{head}\n{legs}\n"
                       f"Trefferchance gesamt <b>{c['prob']:.0%}</b> · faire Gesamtquote <b>{c['fair_odds']:.2f}</b>\n"
                       f"{live}"
                       f"Nur spielen, wenn Sporttip ≥ <b>{c['fair_odds']:.2f}</b> zahlt. "
                       f"Prüfen: /sporttip {c['id']} Quote1 Quote2 …\n"
                       f"<i>{bt}</i>{wide}")
    return "\n\n".join(out)


def format_today(plan: dict) -> str:
    """Tagesreport: Kombi für heute; sonst ehrlich melden und die nächste zeigen."""
    from datetime import datetime

    today = datetime.now(state.TZ).date().isoformat()
    every = [c for k in ("day_combos", "risky_combos", "krass_combos") for c in plan.get(k, []) if c["day"] == today]
    dropped = 0
    if os.getenv("VALUE_ONLY") == "1":  # Nur-Value-Modus: Kombis weglassen, die bei Sporttip vermutlich Verlust sind
        keep = [c for c in every if sporttip_estimate(c) is None or sporttip_estimate(c) >= c["fair_odds"]]
        dropped, every = len(every) - len(keep), keep
        plan = {**plan, **{k: [c for c in plan.get(k, []) if c in keep or c["day"] != today]
                           for k in ("day_combos", "risky_combos", "krass_combos")}}
    note = (f"\n\n🧮 {dropped} Kombi(s) weggelassen – Sporttip zahlt dort vermutlich weniger als fair."
            if dropped else "")
    if any(c["day"] == today for c in plan.get("day_combos", [])):
        # Alle Kombis von heute, nach Wettbewerbs-Art getrennt (Frauen, Nationalteams, Europapokal, Ligen)
        return format_day_combos({**plan, "day_combos": every}, max_days=1) + note
    text = _format_today_safe(plan, today)
    extra = [c for c in every if c.get("risky") or c.get("krass")]
    if extra:
        text += "\n\n" + format_day_combos({**plan, "day_combos": extra}, max_days=1)
    return text + note


def _format_today_safe(plan: dict, today: str) -> str:
    from datetime import datetime

    combos = plan.get("day_combos", [])
    if any(c["day"] == today for c in combos):
        return format_day_combos({**plan, "day_combos": [c for c in combos if c["day"] == today]}, max_days=1)
    upcoming = sorted({c["day"] for c in combos if c["day"] > today})
    now = datetime.now(state.TZ)
    rest: dict[int, dict] = {}
    for t in plan.get("safe", []):  # pro Spiel der sicherste Tipp, nur Spiele, die heute noch kommen
        k = state.local(t["kickoff"])
        if k.date().isoformat() == today and k > now and t["prob"] > rest.get(t["match_id"], {}).get("prob", 0):
            rest[t["match_id"]] = t
    msg = "📭 Heute kommen weltweit keine 3 Spiele mehr mit sicheren Pinnacle-Tipps – darum keine Kombi."
    if rest:
        msg += "\n\n⚽ <b>Heute noch als Einzeltipps:</b>\n" + "\n".join(
            f"  <b>{state.local(t['kickoff']).strftime('%H:%M')} {t['match']}</b>\n     ➡️ {tip(t['label'])}"
            f" <b>({t['prob']:.0%})</b>"
            f" · Sporttip mind. <b>{1 / t['prob']:.2f}</b>"
            for t in sorted(rest.values(), key=lambda t: t["kickoff"]))
    if upcoming:
        return msg + "\n\n📅 <b>Nächste Tageskombi:</b>\n\n" + format_day_combos(
            {**plan, "day_combos": [c for c in combos if c["day"] == upcoming[0]]}, max_days=1)
    return msg + " Sobald neue Spiele Quoten haben, melde ich mich."


def format_combo_result(c: dict) -> str:
    won = all(r["won"] for r in c["results"] if r["won"] is not None)
    head = "🏆 <b>Tageskombi GEWONNEN</b>" if won else "📉 <b>Tageskombi verloren</b>"
    lines = [f"{head} ({c['size']}er, {c['day']}): <b>{c['correct']} von {c['size']} richtig</b>"]
    for r in c["results"]:
        icon = "✅" if r["won"] else "➖" if r["won"] is None else "❌"
        lines.append(f"{icon} {r['match']} {r['score']} · {r['label']}")
    return "\n".join(lines)


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


COMMANDS = [
    ("tageskombi", "3er- und 5er-Kombi, alle Spiele am selben Tag"),
    ("risiko", "Risiko-Kombi: 2 BetBuilder, Quote ca. 4–8"),
    ("krass", "Krass-Kombi: 3 BetBuilder, Quote ca. 8–25"),
    ("sporttip", "Sporttip-Quoten prüfen: /sporttip T1 1.30 1.25 …"),
    ("kosten", "Claude-Kosten heute"),
    ("sicher", "Tipps mit hoher Trefferquote"),
    ("heute", "Value-Tipps und Sicher-Tipps"),
    ("analyse", "Agenten-Analyse: /analyse Team"),
    ("spiel", "Prognose zu einem Spiel: /spiel Team"),
    ("kombi", "Value-Kombis"),
    ("bilanz", "Gewinn, Verlust, Trefferquote"),
    ("gesetzt", "Wette erfassen: /gesetzt T1 10 2.10"),
    ("update", "Daten und Tipps neu berechnen"),
    ("start", "Hilfe und alle Befehle"),
]


async def register_commands(app: Application) -> None:
    """Befehlsmenü, das Telegram beim Tippen von "/" anzeigt."""
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


def build(engine) -> Application | None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        log.info("TELEGRAM_TOKEN fehlt – Bot deaktiviert")
        return None
    app = ApplicationBuilder().token(token).build()
    owner = owner_id()
    only_owner = filters.User(user_id=owner) if owner else filters.User(user_id=[])

    async def reply(update: Update, text: str):
        for chunk in chunks(text) or [""]:
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
                            "/tageskombi – 3er- und 5er-Kombi, alle Spiele am selben Tag\n"
                            "/risiko – Risiko-Kombi: 2 BetBuilder, Quote ca. 4–8\n"
                            "/krass – Krass-Kombi: 3 BetBuilder, Quote ca. 8–25\n"
                            "/sporttip T1 Quoten – Sporttip-Quoten prüfen\n"
                            "📸 Screenshot vom Sporttip-Schein schicken – ich prüfe die Quoten\n"
                            "/sicher – Tipps mit hoher Trefferquote (Über/Unter, 1X …)\n"
                            "/kosten – Claude-Kosten heute\n/bilanz – Bilanz\n/gesetzt Nr Einsatz Quote – Wette erfassen (z. B. /gesetzt S3 10 1.45)\n"
                            "/update – neu berechnen")

    async def cmd_today(update: Update, _ctx):
        plan = state.load_plan()
        await reply(update, format_singles(plan))
        await reply(update, format_safe(plan))

    async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from fussball.agents import runner, scout

        if not ctx.args:
            await reply(update, "Beispiel: /analyse Bayern")
            return
        if scout.make_client() is None:
            await reply(update, "Die Agenten sind noch nicht aktiv: ANTHROPIC_API_KEY fehlt beim Server.")
            return
        q = " ".join(ctx.args).lower()
        plan = state.load_plan()
        leg = next((l for c in plan.get("day_combos", []) for l in c["legs"] if q in l["match"].lower()), None) \
            or next((t for t in plan.get("safe", []) if q in t["match"].lower()), None)
        if leg is None:
            f = next((f for f in plan["forecasts"] if q in f["home"].lower() or q in f["away"].lower()), None)
            if f is None:
                await reply(update, f"Kein Spiel mit „{q}“ in den nächsten Tagen.")
                return
            leg = {"match_id": f["match_id"], "match": f"{f['home']} – {f['away']}", "kickoff": f["kickoff_utc"],
                   "comp": f["comp"], "label": "allgemeine Analyse"}
        await reply(update, f"🕵️ Scout recherchiert {leg['match']} … (ca. 1 Minute)")
        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: runner.analyze_legs(engine, [leg], max_age_h=1.0, local_time=state.local))
        await reply(update, res[0]["text"] if res else "Tagesbudget der Agenten erreicht – morgen wieder.")

    async def cmd_day(update: Update, _ctx):
        plan = state.load_plan()
        await reply(update, format_today(plan))
        later = [c for c in plan.get("day_combos", []) if c["day"] > __import__("datetime").datetime.now(state.TZ)
                 .date().isoformat()]
        if later and any(c["day"] == __import__("datetime").datetime.now(state.TZ).date().isoformat()
                         for c in plan.get("day_combos", [])):
            await reply(update, "Weitere Tage:\n\n" + format_day_combos({**plan, "day_combos": later}, max_days=2))

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

    async def cmd_risky(update: Update, _ctx):
        plan = state.load_plan()
        combos = plan.get("risky_combos", [])
        await reply(update, format_day_combos({**plan, "day_combos": combos}, max_days=2) if combos
                    else "Keine Risiko-Kombi möglich (zu wenige Spiele mit 60–72 % an einem Tag).")

    async def cmd_krass(update: Update, _ctx):
        plan = state.load_plan()
        combos = plan.get("krass_combos", [])
        await reply(update, format_day_combos({**plan, "day_combos": combos}, max_days=2) if combos
                    else "Keine Krass-Kombi möglich (zu wenige Spiele mit 55–70 % an einem Tag).")

    async def cmd_costs(update: Update, _ctx):
        from fussball.agents import runner

        await reply(update, runner.cost_summary(engine))

    async def cmd_sporttip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            combo_id, quotes = ctx.args[0], [float(a.replace(",", ".")) for a in ctx.args[1:]]
            assert quotes and all(q > 1.0 for q in quotes)
        except (IndexError, ValueError, AssertionError):
            await reply(update, "Format: /sporttip T1 1.30 1.25 1.40 1.22 1.35 (eine Quote pro Spiel)\n"
                                "oder /sporttip T1 3.10 (Gesamtquote vom Sporttip-Schein)")
            return
        await reply(update, check_sporttip(state.load_plan(), combo_id, quotes))

    async def on_photo(update: Update, _ctx):
        from fussball.agents import scout, slip

        client = scout.make_client()
        if client is None:
            await reply(update, "Zum Lesen von Screenshots fehlt ANTHROPIC_API_KEY beim Server.")
            return
        msg = update.effective_message
        if msg.photo:
            tg_file, media = await msg.photo[-1].get_file(), "image/jpeg"
        else:
            tg_file, media = await msg.document.get_file(), msg.document.mime_type or "image/jpeg"
        image = bytes(await tg_file.download_as_bytearray())
        await reply(update, "🔍 Lese den Sporttip-Schein … (ca. 20 Sekunden)")

        def work():
            forecasts = service.market_forecasts(engine, hours=96)
            listing = [f"{f.match_id}: {f.home} – {f.away} ({state.local(f.kickoff_utc):%d.%m. %H:%M})"
                       for f in forecasts]
            from fussball.agents import runner

            if runner.spent_today(engine) >= runner.daily_budget():
                raise RuntimeError("Tageslimit für Claude erreicht – morgen wieder (siehe /kosten)")
            data, cost = slip.read_slip(client, image, media, listing)
            runner.add_other_cost(engine, cost)
            rows = slip.evaluate(data, forecasts)
            return data, rows, slip.remember(engine, rows)

        try:
            data, rows, learned = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as exc:  # noqa: BLE001
            log.exception("Screenshot fehlgeschlagen")
            if "credit balance" in str(exc):
                await reply(update, "💳 Dein Anthropic-Guthaben ist leer. Bitte auf console.anthropic.com unter "
                                    "Settings → Billing Guthaben aufladen, dann das Bild nochmal schicken.")
            else:
                await reply(update, f"⚠️ Konnte das Bild nicht lesen: {exc}"[:300])
            return
        if not data.is_betting_slip or not data.legs:
            await reply(update, "Auf dem Bild habe ich keine Wetten mit Quoten gefunden. Bitte den Sporttip-Schein "
                                "oder die Spielliste mit Quoten fotografieren.")
            return
        await reply(update, format_slip(rows, data.total_odds, learned))

    async def cmd_update(update: Update, _ctx):
        await reply(update, "⟳ Aktualisiere Daten und Prognosen …")
        info = await asyncio.get_running_loop().run_in_executor(None, lambda: state.refresh(engine))
        changes = info.get("changes") or ["keine Änderungen bei den Tipps"]
        await reply(update, "Fertig.\n" + "\n".join(changes))

    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    for name, fn in (("heute", cmd_today), ("sicher", cmd_safe), ("tageskombi", cmd_day), ("analyse", cmd_analyse), ("kombi", cmd_combo), ("spiel", cmd_match),
                     ("bilanz", cmd_stats), ("gesetzt", cmd_placed), ("update", cmd_update),
                     ("sporttip", cmd_sporttip), ("risiko", cmd_risky), ("krass", cmd_krass), ("kosten", cmd_costs)):
        app.add_handler(CommandHandler(name, fn, filters=only_owner))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & only_owner, on_photo))
    return app


def chunks(text: str, limit: int = 3900) -> list[str]:
    """Lange Nachrichten an Absätzen (dann Zeilen) teilen – nie mitten in einem HTML-Tag."""
    out, cur = [], ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else block.split("\n")
        for piece in pieces:
            sep = "\n\n" if piece is block else "\n"
            if cur and len(cur) + len(sep) + len(piece) > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}{sep}{piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


async def notify(app: Application | None, text: str) -> None:
    owner = owner_id()
    if app is None or owner is None or not text:
        return
    try:
        for chunk in chunks(text):
            await app.bot.send_message(owner, chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("Telegram-Nachricht fehlgeschlagen")
