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


SHORT_CAT = {"national": "🌍 Länderspiele", "europa": "🏆 Europapokal", "liga_eu": "🇪🇺 Ligen Europa",
             "suedamerika": "🌎 Südamerika", "nordamerika": "🇺🇸 Nordamerika", "asien": "🌏 Asien & Australien",
             "andere": "⚽ Andere Ligen"}


def top_n() -> int:
    return int(os.getenv("TOP_TIPS", "6"))


def top_min_prob() -> float:
    """Nur harte, sichere Tipps: mindestens so wahrscheinlich (Standard 76 %)."""
    return float(os.getenv("TOP_MIN_PROB", "0.76"))


def fill_min_prob() -> float:
    """Untergrenze zum Auffüllen auf 6 Tipps (Standard 70 %), wenn es nicht genug sichere gibt."""
    return float(os.getenv("TOP_FILL_PROB", "0.70"))


def _tier(t: dict, agent: str | None) -> int:
    """0 = 🟢 sicher (≥ 76 %), 1 = 🟢 bestätigt (70–76 %), 2 = ⚪ ungeprüft, 3 = 🔴 Agent warnt."""
    if agent == "bestätigt":
        return 0 if t["prob"] >= top_min_prob() else 1
    return 3 if agent == "vorsicht" else 2


def top_tips(plan: dict, n: int | None = None, day: str | None = None) -> dict[str, list[dict]]:
    """Jeden Tag n Tipps (Standard 6), so gut wie möglich, nach Wettbewerbs-Art getrennt:
    zuerst vom Scout + Gegenprüfer bestätigte ab 76 %, dann bestätigte ab 70 %, dann ungeprüfte,
    zuletzt gewarnte (mit Grund). Gestrichene nie. Reichen die Spiele von heute nicht, kommen
    Spiele von morgen dazu (mit Datum)."""
    from datetime import datetime, timedelta

    from fussball.service import COMBO_MARKETS, EXCLUDED_TIPS, category

    now = datetime.now(state.TZ)
    day = day or now.date().isoformat()
    tomorrow = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    reports = {l["match_id"]: (l.get("agent") or {})
               for k in ("day_combos", "boost_combos", "torfest_combos", "risky_combos", "krass_combos") for c in plan.get(k, []) for l in c["legs"]}
    for t in plan.get("top") or []:  # eigenes Urteil des Scouts zu genau diesem Top-Tipp
        if t.get("agent"):
            reports[t["match_id"]] = {"assessment": t["agent"], "reason": t.get("reason", "")}
    agent = {mid: r.get("assessment") for mid, r in reports.items()}
    best: dict[int, dict] = {}
    for t in plan.get("top") or plan.get("safe", []):
        k = state.local(t["kickoff"])
        if (k.date().isoformat() not in (day, tomorrow) or k <= now or t["market"] not in COMBO_MARKETS
                or (t["market"], t["selection"]) in EXCLUDED_TIPS or agent.get(t["match_id"]) == "streichen"
                or ((t["market"], t["selection"]) == ("OU4.5", "U") and t.get("profile") != "zaeh")
                or t["prob"] < fill_min_prob()):
            continue
        a = agent.get(t["match_id"])
        cand = {**t, "agent": a, "reason": reports.get(t["match_id"], {}).get("reason", ""),
                "tier": _tier(t, a), "later": k.date().isoformat() != day}
        old = best.get(t["match_id"])
        if old is None or (cand["tier"], -cand["prob"]) < (old["tier"], -old["prob"]):
            best[t["match_id"]] = cand
    from fussball.service import _varied

    # Reihenfolge: gewarnte ganz am Schluss, heute vor morgen, bessere Stufe zuerst, dann die sicherste Chance;
    # höchstens 2× derselbe Tipp – sonst nächstbester Tipp desselben Spiels
    ranked = sorted(best.values(), key=lambda t: (t["tier"] == 3, t["later"], t["tier"], -t["prob"]))
    chosen = _varied(ranked, n or top_n())
    by_cat: dict[str, list[dict]] = {}
    for t in sorted(chosen, key=lambda t: t["kickoff"]):
        by_cat.setdefault(category(t["comp"], t.get("comp_name")), []).append(t)
    return by_cat


def why_no_tips(plan: dict) -> str:
    """Kurz erklären, warum keine Tipps kamen (statt einfach nichts zu schicken)."""
    from datetime import datetime

    now = datetime.now(state.TZ)
    today = [t for t in plan.get("top") or []
             if state.local(t["kickoff"]).date() == now.date() and state.local(t["kickoff"]) > now
             and t["prob"] >= fill_min_prob()]
    if not today:
        return ("Heute und morgen gibt es kein europäisches Spiel mit mindestens "
                f"{fill_min_prob():.0%} Chance (z. B. Länderspielpause oder wenig Spiele). Lieber kein Tipp als ein unsicherer.")
    warned = sum(t.get("agent") in ("vorsicht", "streichen") for t in today)
    unchecked = sum(t.get("agent") in (None, "ungeprüft") for t in today)
    failed = sum(t.get("agent") == "fehler" for t in today)
    parts = [f"{len(today)} Spiele kamen in Frage"]
    if warned:
        parts.append(f"{warned} haben die Agenten wegen Risiko gestrichen")
    if unchecked:
        parts.append(f"{unchecked} konnten nicht geprüft werden (Tageslimit – in Render AGENT_DAILY_BUDGET_USD erhöhen)")
    if failed:
        parts.append(f"⚠️ {failed} mit Agenten-Fehler – Details unter /status")
    return " · ".join(parts) + "."


def version() -> str:
    """Kurze Commit-Kennung des laufenden Stands (Render setzt RENDER_GIT_COMMIT)."""
    return os.getenv("RENDER_GIT_COMMIT", "")[:7] or "lokal"


def format_status(plan: dict, spent: float, budget: float, daily: str | None, last_error: str | None) -> str:
    """Läuft alles? Tagesprüfung, Agenten-Urteile, Fehler und Kosten auf einen Blick."""
    from datetime import datetime

    now = datetime.now(state.TZ)
    today = [t for t in plan.get("top") or [] if state.local(t["kickoff"]).date() == now.date()]
    count = lambda *a: sum(t.get("agent") in a for t in today)  # noqa: E731
    lines = [f"🩺 <b>Status</b> · Version <code>{version()}</code>",
             f"{'✅' if daily else '⏳'} Tagesprüfung heute: {'gelaufen' if daily else 'noch nicht gelaufen'}",
             f"🔎 Spiele heute in Frage: {len(today)}",
             f"🟢 Von Scout + Gegenprüfer bestätigt: {count('bestätigt')}",
             f"🔴 Wegen Risiko gestrichen: {count('vorsicht', 'streichen')}",
             f"⚪ Nicht geprüft (Limit/noch nicht dran): {count(None, 'ungeprüft')}",
             f"{'⚠️' if count('fehler') else '✅'} Agenten-Fehler: {count('fehler')}",
             f"💰 Claude heute: {spent:.2f} $ von {budget:.2f} $"]
    errs = [t for t in today if t.get("agent") == "fehler"][:3]
    for t in errs:
        lines.append(f"   · {html.escape(t['match'])}: {html.escape(t.get('reason', ''))[:120]}")
    if last_error:
        lines.append(f"⚠️ Letzter Systemfehler: {html.escape(last_error)[:200]}")
    for t in today:
        if t.get("agent") in ("vorsicht", "streichen") and t.get("reason"):
            lines.append(f"🔴 <b>{html.escape(t['match'])}</b>: <i>{html.escape(t['reason'])}</i>")
    return "\n".join(lines)


def format_top5(plan: dict, n: int | None = None) -> str:
    """Kurz und klar: die Tipps von heute, je Wettbewerbs-Art, eine Zeile pro Spiel."""
    groups = top_tips(plan, n)
    if not groups:
        return "📭 <b>Heute keine Tipps</b>\n" + why_no_tips(plan)
    icon = {0: " 🟢", 1: " 🟢", 2: " ⚪", 3: " 🔴"}
    out = ["🛡️ <b>Tipps heute</b>"]
    shown = [t for ts in groups.values() for t in ts]
    for cat in SHORT_CAT:
        ts = groups.get(cat)
        if not ts:
            continue
        out.append(f"\n<b>{SHORT_CAT[cat]}</b>")
        prob = 1.0
        for t in ts:
            prob *= t["prob"]
            from fussball.service import PROFILE_TEXT

            hint = f" <i>{PROFILE_TEXT[t['profile']]}</i>" if t.get("profile") in PROFILE_TEXT else ""
            k = state.local(t["kickoff"])
            when = k.strftime("%H:%M") if not t.get("later") else "morgen " + k.strftime("%H:%M")
            out.append(f"<b>{when} {t['match']}</b>{hint}\n"
                       f"➡️ {tip(t['label'])} · {t['prob']:.0%}{icon.get(t.get('tier', 2), '')}"
                       + (f"\n<i>🔴 {html.escape(t['reason'])}</i>" if t.get("tier") == 3 and t.get("reason")
                          else ""))
        if len(ts) > 1:
            out.append(f"<i>Alle {len(ts)} als Kombi: Chance {prob:.0%}</i>")
    legend = ["🟢 = von Scout + Gegenprüfer bestätigt"]
    if any(t.get("tier") == 2 for t in shown):
        legend.append("⚪ = nicht geprüft")
    if any(t.get("tier") == 3 for t in shown):
        legend.append("🔴 = Agent warnt – nur klein setzen")
    out.append("\n<i>" + " · ".join(legend) + "</i>")
    return "\n".join(out)


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
    if tid.startswith(("T", "R", "X", "B", "F")):
        c = next((c for c in plan.get("day_combos", []) + plan.get("boost_combos", []) + plan.get("torfest_combos", []) + plan.get("risky_combos", []) + plan.get("krass_combos", []) if c["id"] == tid), None)
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
BUILDER_HOWTO = "🧩 Pro Spiel im „BetBuilder“ zusammenstellen, dann auf einen Schein."
KRASS_BT = "Geht etwa an 1 von 10 Tagen auf. Nur Mini-Einsatz.\n" + BUILDER_HOWTO
RISKY_BT = "Geht etwa an 1 von 5 Tagen auf. Nur kleiner Einsatz.\n" + BUILDER_HOWTO


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
    c = next((c for c in plan.get("day_combos", []) + plan.get("boost_combos", []) + plan.get("torfest_combos", []) + plan.get("risky_combos", []) + plan.get("krass_combos", [])
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


def verdict(row: dict, check: dict) -> tuple[bool | None, str]:
    """🟢/🔴 für einen Tipp: rot, wenn der Scout ihn streicht oder warnt, oder die Chance unter 50 % liegt."""
    a, reason, prob = check.get("assessment"), check.get("reason", ""), row.get("prob")
    if len(reason) > 220:
        reason = reason[:217].rsplit(" ", 1)[0] + " …"
    if prob is not None and prob < 0.5:
        return False, f"Nur ca. {prob:.0%} Chance – geht öfter nicht auf als auf." + (f" {reason}" if reason else "")
    if a in ("streichen", "vorsicht"):
        return False, reason
    if a == "bestätigt":
        return True, reason
    return None, reason or "nicht geprüft"


def format_results(items: list[dict], title: str = "📋 <b>Ergebnisse</b>") -> str:
    """Nur die Spiele mit Namen: 🟢 Tipp gewonnen, 🔴 verloren, ⚪ nicht auswertbar, ⏳ läuft noch."""
    if not items:
        return "Noch keine verfolgten Tipps. Schick einen Screenshot von deinem Wettschein."
    lines = [title]
    for it in sorted(items, key=lambda i: i.get("kickoff") or ""):
        icon = "⏳" if not it["done"] else "🟢" if it["won"] else "🔴" if it["won"] is False else "⚪"
        lines.append(f"{icon} <b>{html.escape(it['match'])}</b>")
    won = sum(1 for i in items if i["done"] and i["won"])
    decided = sum(1 for i in items if i["done"] and i["won"] is not None)
    if decided:
        lines.append(f"<b>{won} von {decided} gewonnen</b>")
    return "\n".join(lines)


def format_slip(rows: list[dict], checks: list[dict], book: str = "Sporttip") -> str:
    """Antwort auf einen Screenshot: pro Tipp 🟢 geht auf / 🔴 geht nicht auf – mit Grund aus der Recherche."""
    name = book if book != "andere" else "Wett"
    lines = [f"🕵️ <b>{name}-Schein geprüft</b>"]
    reds, greens, open_ = 0, 0, 0
    for r, c in zip(rows, checks):
        leg = r["leg"]
        ok, reason = verdict(r, c)
        icon = "🟢" if ok else "🔴" if ok is False else "⚪"
        reds += ok is False
        greens += ok is True
        open_ += ok is None
        chance = f" · {r['prob']:.0%}" if r.get("prob") else ""
        lines.append(f"\n{icon} <b>{r['match']}</b>\n{tip(leg.market_text)}{chance}"
                     + (f"\n<i>{html.escape(reason)}</i>" if reason else ""))
    total = len(rows)
    if reds:
        lines.append(f"\n🔴 <b>{reds} von {total} Tipps rot</b> – diese weglassen.")
    elif open_:
        lines.append(f"\n⚪ {open_} Tipp(s) konnten nicht geprüft werden, der Rest ist 🟢.")
    else:
        lines.append(f"\n🟢 <b>Alle {total} Tipps grün</b> – laut Recherche passt der Schein.")
    return "\n".join(lines)


BOOST_BT = ("Für den Sporttip-KombiBoost: jeder Tipp ab Quote 1.50 → Sporttip legt Bonus drauf. "
            "Geht etwa an 1 von 10–15 Tagen auf. Nur kleiner Einsatz.")


def leg_sporttip(l: dict) -> float:
    """Ungefähre Sporttip-Quote eines Tipps: gelernt aus Screenshots, sonst faire Quote × 0.93."""
    from fussball.service import SPORTTIP_FACTOR

    return l.get("sporttip_est") or round(SPORTTIP_FACTOR / l["prob"], 2)


def sporttip_odds(c: dict) -> float:
    out = 1.0
    for l in c["legs"]:
        out *= leg_sporttip(l)
    return out


TORFEST_BT = ("Nur die torreichsten Spiele. Im Test mit 2003 torreichen Spielen: 51 % berechnet → 52 % getroffen. "
              "Geht etwa an 1 von 7–8 Tagen auf. Nur kleiner Einsatz.")


def format_boost(plan: dict, key: str = "boost_combos",
                 title: str = "🚀 <b>Boost-Kombi heute</b> (jeder Tipp ab Quote 1.50 → KombiBoost)") -> str:
    """Kurz: Boost- bzw. Torfest-Kombis von heute, eine Zeile pro Spiel."""
    from datetime import datetime

    today = datetime.now(state.TZ).date().isoformat()
    combos = [c for c in plan.get(key, []) if c["day"] == today]
    if key == "boost_combos":
        combos = combos[:1]  # genau eine Boost-Kombi pro Tag
    if not combos:
        return ""
    icon = {"bestätigt": " 🟢", "vorsicht": " 🔴", "streichen": " ❌"}
    out = [title]
    for c in combos:
        out.append(f"\n<b>{SHORT_CAT.get(c.get('cat'), '')}</b>")
        for l in c["legs"]:
            a = (l.get("agent") or {})
            mark = icon.get(a.get("assessment"), " ⚪" if key == "boost_combos" else "")
            out.append(f"<b>{state.local(l['kickoff']).strftime('%H:%M')} {l['match']}</b>\n"
                       f"➡️ {tip(l['label'])} · {l['prob']:.0%} · Quote ca. {leg_sporttip(l):.2f}{mark}"
                       + (f"\n<i>🔴 {html.escape(a['reason'])}</i>"
                          if a.get("assessment") in ("vorsicht", "streichen") and a.get("reason") else ""))
        out.append(f"<i>Gesamtquote ca. {sporttip_odds(c):.1f}{' + Boost' if c.get('boost') else ''} · "
                   f"Chance {c['prob']:.0%}</i>")
    if key == "boost_combos":
        out.append("\n<i>⚠️ Mutiger Schein: gewinnt selten, dafür viel. Nur kleiner Einsatz (z. B. 2–5 CHF).</i>")
    return "\n".join(out)


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
    kind = lambda c: (4 if c.get("krass") else 3 if c.get("risky") else 2 if c.get("torfest")  # noqa: E731
                      else 1 if c.get("boost") else 0)
    out = []
    for day in days:
        day_combos = sorted([c for c in combos if c["day"] == day],
                            key=lambda c: (order.index(c["cat"]) if c.get("cat") in order else 99, kind(c),
                                           c["size"]))
        shown_cat = None
        for c in day_combos:
            cat = c.get("cat", "andere")
            if cat != shown_cat and len({x.get("cat", "andere") for x in combos}) > 1:
                out.append(f"━━━━━━━━━━━━━━━\n<b>{CATEGORIES.get(cat, cat)}</b>")
                shown_cat = cat
            d = _fmt_day(c["legs"][0]["kickoff"])
            icon = {"bestätigt": " 🟢", "vorsicht": " 🔴", "streichen": " 🔴"}
            legs = "\n".join(f"  {i}. <b>{state.local(l['kickoff']).strftime('%H:%M')} {l['match']}</b>"
                              f" <i>({l.get('comp_name') or l.get('comp', '')})</i>\n"
                              f"     {'🧩' if l.get('market') == 'BB' else '➡️'} {tip(l['label'])} <b>({l['prob']:.0%})</b>"
                              f"{icon.get((l.get('agent') or {}).get('assessment'), '')}"
                              + (f" · Quote ca. {leg_sporttip(l):.2f}" if c.get("boost") or c.get("torfest") else "")
                              + (f"\n     <i>🔄 vom Scout gewählt statt „{l['switched_from']}“</i>"
                                 if l.get("switched_from") else "")
                              + (f"\n     <i>🔴 {html.escape(l['agent']['reason'])}</i>"
                                 if (l.get("agent") or {}).get("assessment") == "vorsicht" else "")
                              for i, l in enumerate(c["legs"], 1))
            wide = ("\n<i>Heute gibt es nicht genug Spiele im Bereich 75–88 %, darum etwas "
                    "breiter gewählt.</i>" if c.get("widened") else "")
            if c.get("torfest"):
                head = (f"⚡ <b>{c['id']} · Torfest-Kombi {d}</b> ({c['size']} Spiele, 2 Tore vor der Pause, "
                        f"Sporttip ca. {sporttip_odds(c):.1f})")
                bt, pct = TORFEST_BT, STAKE_PCT["risky"]
            elif c.get("boost"):
                odds = sporttip_odds(c)
                head = f"🚀 <b>{c['id']} · Boost-Kombi {d}</b> ({c['size']} Tipps, Sporttip ca. {odds:.1f} + KombiBoost)"
                bt, pct = BOOST_BT, STAKE_PCT["risky"]
            elif c.get("krass"):
                head = f"🔥 <b>{c['id']} · Krass-Kombi {d}</b> ({c['size']} BetBuilder)"
                bt, pct = KRASS_BT, STAKE_PCT["krass"]
            elif c.get("risky"):
                head = f"🎲 <b>{c['id']} · Risiko-Kombi {d}</b> ({c['size']} {'BetBuilder' if c.get('builder') else 'Spiele'})"
                bt, pct = RISKY_BT, STAKE_PCT["risky"]
            else:
                head = f"🎯 <b>{c['id']} · {c['size']}er-Tageskombi {d}</b>"
                bt = f"Backtest {c['size']}er: {DAY_BT.get(str(c['size']), '')}"
                pct = STAKE_PCT.get(str(c["size"]), 1.0)
            bt += f"\n💰 Einsatz: höchstens {pct:g} % deiner Wettkasse"
            out.append(f"{head}\n{legs}\n"
                       f"Chance gesamt <b>{c['prob']:.0%}</b> · Quote ca. <b>{c['fair_odds']:.2f}</b>\n"
                       f"<i>{bt}</i>{wide}")
    return "\n\n".join(out)


def format_today(plan: dict) -> str:
    """Tagesreport: Kombi für heute; sonst ehrlich melden und die nächste zeigen."""
    from datetime import datetime

    today = datetime.now(state.TZ).date().isoformat()
    every = [c for k in ("day_combos", "boost_combos", "torfest_combos", "risky_combos", "krass_combos") for c in plan.get(k, []) if c["day"] == today]
    dropped = 0
    if os.getenv("VALUE_ONLY") == "1":  # Nur-Value-Modus: Kombis weglassen, die bei Sporttip vermutlich Verlust sind
        keep = [c for c in every if sporttip_estimate(c) is None or sporttip_estimate(c) >= c["fair_odds"]]
        dropped, every = len(every) - len(keep), keep
        plan = {**plan, **{k: [c for c in plan.get(k, []) if c in keep or c["day"] != today]
                           for k in ("day_combos", "boost_combos", "torfest_combos", "risky_combos", "krass_combos")}}
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
    ("top5", "Die 5–6 sichersten Tipps von heute, vom Scout geprüft"),
    ("tageskombi", "Sichere 3er- und 5er-Kombi, alle Spiele am selben Tag"),
    ("ergebnisse", "Deine Tipps: 🟢 gewonnen / 🔴 verloren"),
    ("jetzt", "Agenten jetzt prüfen lassen und 6 Tipps schicken"),
    ("status", "Laufen die Agenten? Geprüft, gestrichen, Fehler"),
    ("kosten", "Claude-Kosten heute"),
    ("sicher", "Tipps mit hoher Trefferquote"),
    ("heute", "Top-Tipps von heute (wie /top5)"),
    ("analyse", "Agenten-Analyse: /analyse Team"),
    ("spiel", "Prognose zu einem Spiel: /spiel Team"),
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
        await reply(update, "👋 PeaceJudge ist bereit.\n/top5 – die sichersten Tipps von heute\n/kombi – Kombis\n/spiel Team – Prognose\n"
                            "/tageskombi – 3er- und 5er-Kombi, alle Spiele am selben Tag\n"
                            "/boost – Boost-Kombi: Tipps ab 1.50 + KombiBoost\n"
                            "/torfest – 1. Halbzeit Über 1.5 in den torreichsten Spielen\n"
                            "/risiko – Risiko-Kombi: 2 BetBuilder, Quote ca. 4–8\n"
                            "/krass – Krass-Kombi: 3 BetBuilder, Quote ca. 8–25\n"
                            "📸 Screenshot vom Wettschein schicken – die Agenten prüfen jeden Tipp: 🟢 geht auf / 🔴 nicht\n"
                            "/sicher – Tipps mit hoher Trefferquote (Über/Unter, 1X …)\n"
                            "/ergebnisse – deine Tipps: 🟢 gewonnen / 🔴 verloren\n/kosten – Claude-Kosten heute\n/bilanz – Bilanz\n/gesetzt Nr Einsatz Quote – Wette erfassen (z. B. /gesetzt S3 10 1.45)\n"
                            "/update – neu berechnen")

    async def cmd_today(update: Update, _ctx):
        await reply(update, format_top5(state.load_plan()))

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

    async def cmd_boost(update: Update, _ctx):
        plan = state.load_plan()
        combos = plan.get("boost_combos", [])
        await reply(update, format_day_combos({**plan, "day_combos": combos}, max_days=2) if combos
                    else "Keine Boost-Kombi möglich (zu wenige Spiele mit Tipps ab Quote 1.50).")

    async def cmd_torfest(update: Update, _ctx):
        plan = state.load_plan()
        combos = plan.get("torfest_combos", [])
        await reply(update, format_day_combos({**plan, "day_combos": combos}, max_days=2) if combos
                    else "Keine Torfest-Kombi möglich (zu wenige sehr torreiche Spiele an einem Tag).")

    async def cmd_krass(update: Update, _ctx):
        plan = state.load_plan()
        combos = plan.get("krass_combos", [])
        await reply(update, format_day_combos({**plan, "day_combos": combos}, max_days=2) if combos
                    else "Keine Krass-Kombi möglich (zu wenige Spiele mit 55–70 % an einem Tag).")

    async def cmd_results(update: Update, _ctx):
        from fussball import tracking

        await reply(update, format_results(tracking.recent(engine)))

    async def cmd_status(update: Update, _ctx):
        from fussball.agents import runner

        await reply(update, format_status(state.load_plan(), runner.spent_today(engine), runner.daily_budget(),
                                          runner.daily_done(engine), state.status().get("last_error")))

    async def cmd_now(update: Update, _ctx):
        """/jetzt: Agenten-Prüfung sofort starten und 6 Tipps schicken (Tageslimit gilt weiter)."""
        import asyncio

        from fussball.app import jobs

        if jobs.daily_running():
            await reply(update, "⏳ Die Agenten prüfen gerade schon – die Tipps kommen gleich.")
            return
        asyncio.get_running_loop().create_task(jobs.run_daily_now(engine))

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
        await reply(update, "🔍 Lese den Schein, dann recherchieren die Agenten jedes Spiel … (1–3 Minuten)")

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
            book = data.bookmaker if data.bookmaker != "andere" else "Sporttip"
            slip.remember(engine, rows, book)
            items = [{"match_id": r.get("match_id"), "match": r["match"], "kickoff": r.get("kickoff"),
                      "comp": r.get("comp", ""), "label": r["leg"].market_text} for r in rows]
            checks = runner.check_tips(engine, items, client=client, local_time=state.local)
            from fussball import tracking

            tracking.track(engine, rows)
            return data, rows, checks

        try:
            data, rows, checks = await asyncio.get_running_loop().run_in_executor(None, work)
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
        await reply(update, format_slip(rows, checks, data.bookmaker)
                    + "\n\n<i>📋 Ich melde mich nach den Spielen mit 🟢 gewonnen / 🔴 verloren (/ergebnisse).</i>")

    async def cmd_update(update: Update, _ctx):
        await reply(update, "⟳ Aktualisiere Daten und Prognosen …")
        info = await asyncio.get_running_loop().run_in_executor(None, lambda: state.refresh(engine))
        changes = info.get("changes") or ["keine Änderungen bei den Tipps"]
        await reply(update, "Fertig.\n" + "\n".join(changes))

    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    for name, fn in (("heute", cmd_today), ("top5", cmd_today), ("sicher", cmd_safe), ("tageskombi", cmd_day), ("analyse", cmd_analyse), ("kombi", cmd_combo), ("spiel", cmd_match),
                     ("bilanz", cmd_stats), ("gesetzt", cmd_placed), ("update", cmd_update),
                     ("sporttip", cmd_sporttip), ("risiko", cmd_risky), ("krass", cmd_krass), ("boost", cmd_boost), ("torfest", cmd_torfest), ("kosten", cmd_costs), ("ergebnisse", cmd_results), ("status", cmd_status), ("jetzt", cmd_now)):
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
