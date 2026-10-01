"""Steuert die Agenten: wählt Spiele, liefert Kontext, hält das Tagesbudget ein,
speichert Berichte und streicht Tipps, die der Scout als zu riskant einstuft."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import Engine, or_, select

from fussball.agents import scout
from fussball.data.db import session_scope
from fussball.data.schema import AgentReport, AppSetting, Match, Team, utcnow

log = logging.getLogger(__name__)


def max_cost_per_match() -> float:
    """Reserve vor jedem neu recherchierten Spiel (höchstens so viel kostet eines): Opus ~0.50 $, Sonnet ~0.30 $."""
    default = "0.3" if "sonnet" in scout.MODEL else "0.5"
    return float(os.getenv("AGENT_MAX_COST_PER_MATCH", default))


def daily_budget() -> float:
    return float(os.getenv("AGENT_DAILY_BUDGET_USD", "1.5"))


def spent_today(engine: Engine) -> float:
    """Alle Claude-Kosten von heute (UTC): Scout-Berichte + gelesene Screenshots."""
    today = utcnow().date()
    with session_scope(engine) as s:
        rows = s.scalars(select(AgentReport.cost_usd).where(
            AgentReport.created_at >= datetime.combine(today, datetime.min.time()))).all()
    return float(sum(rows)) + other_costs_today(engine)["usd"]


def other_costs_today(engine: Engine) -> dict:
    with session_scope(engine) as s:
        row = s.get(AppSetting, "claude_other_costs")
        v = dict(row.value) if row and isinstance(row.value, dict) else {}
    return v if v.get("day") == utcnow().date().isoformat() else {"day": utcnow().date().isoformat(), "usd": 0.0, "n": 0,
                                                                  "carry": 0.0}


def restore_carryover(engine: Engine, usd: float) -> None:
    """Nach einem Neustart: heute bereits ausgegebene Kosten (aus der angehefteten Telegram-Nachricht)
    wieder einrechnen, damit das Tageslimit über Neustarts hinweg gilt."""
    v = other_costs_today(engine)
    v.update(usd=v["usd"] - v.get("carry", 0.0) + usd, carry=usd)
    with session_scope(engine) as s:
        row = s.get(AppSetting, "claude_other_costs")
        if row:
            row.value = v
        else:
            s.add(AppSetting(key="claude_other_costs", value=v))


def add_other_cost(engine: Engine, usd: float) -> None:
    """Kosten ausserhalb des Scouts (z. B. Screenshot lesen) dem Tageszähler hinzufügen."""
    v = other_costs_today(engine)
    v.update(usd=v["usd"] + usd, n=v["n"] + 1)
    with session_scope(engine) as s:
        row = s.get(AppSetting, "claude_other_costs")
        if row:
            row.value = v
        else:
            s.add(AppSetting(key="claude_other_costs", value=v))


PIN_PREFIX = "📌 Claude-Kosten"


def pin_text(engine: Engine) -> str:
    return (f"{PIN_PREFIX} (UTC {utcnow().date().isoformat()}): {spent_today(engine):.2f} $ "
            f"von {daily_budget():.2f} $ Tageslimit")


def parse_pin(text: str | None) -> float | None:
    """Heute bereits ausgegebene Kosten aus der angehefteten Nachricht (None, wenn von einem anderen Tag)."""
    import re

    m = re.search(r"\(UTC (\d{4}-\d{2}-\d{2})\): ([\d.]+) \$", text or "")
    if not m or m.group(1) != utcnow().date().isoformat():
        return None
    return float(m.group(2))


def cost_summary(engine: Engine) -> str:
    today = utcnow().date()
    with session_scope(engine) as s:
        scout = s.scalars(select(AgentReport.cost_usd).where(
            AgentReport.created_at >= datetime.combine(today, datetime.min.time()))).all()
    other = other_costs_today(engine)
    total = float(sum(scout)) + other["usd"]
    carry = other.get("carry", 0.0)
    return (f"🤖 <b>Claude-Kosten heute</b>\n"
            f"Scout: {len(scout)} Spiele · {sum(scout):.2f} $\n"
            f"Screenshots: {other['n']} · {other['usd'] - carry:.2f} $\n"
            + (f"Vor dem letzten Neustart: {carry:.2f} $\n" if carry else "")
            + f"<b>Total {total:.2f} $ von {daily_budget():.2f} $ Tageslimit</b>\n"
            f"<i>Das Tageslimit gilt auch über Neustarts (📌 angeheftete Nachricht). Zusätzliche Sicherung: "
            f"Monatslimit bei Anthropic.</i>")


def fatigue_context(engine: Engine, match_id: int) -> str:
    """Belastung aus unserer Datenbank (nur Ligaspiele): letztes gespieltes Spiel und
    angesetzte Spiele bis zum Anpfiff."""
    now = utcnow()
    with session_scope(engine) as s:
        m = s.get(Match, match_id)
        lines = [f"Stand: {now:%d.%m.%Y} (UTC). Anpfiff dieses Spiels: {m.kickoff_utc:%d.%m.%Y %H:%M} UTC."]
        for team_id in (m.home_team_id, m.away_team_id):
            name = s.get(Team, team_id).name
            q = select(Match.kickoff_utc, Match.status).where(
                or_(Match.home_team_id == team_id, Match.away_team_id == team_id),
                Match.kickoff_utc < m.kickoff_utc, Match.id != m.id)
            rows = s.execute(q.order_by(Match.kickoff_utc.desc())).all()
            played = [k for k, st in rows if st == "finished"]
            upcoming = sorted(k for k, st in rows if st == "scheduled" and k > now)
            last = played[0] if played else None
            parts = [f"letztes Ligaspiel {last:%d.%m.}" if last else "letztes Ligaspiel unbekannt"]
            if upcoming:
                parts.append("bis zum Anpfiff noch angesetzt: " + ", ".join(f"{k:%d.%m.}" for k in upcoming))
            before = upcoming[-1] if upcoming else last
            if before:
                parts.append(f"Ruhetage vor dem Spiel: {(m.kickoff_utc - before).days}")
            lines.append(f"- {name}: " + "; ".join(parts) + " (Europapokal/Pokal/Länderspiele nicht erfasst)")
        return "\n".join(lines)


def option_note(alt: dict) -> str:
    """Kurzinfo zu einem möglichen Tipp für den Scout: Chance, faire Quote, geschätzte Sporttip-Quote."""
    note = f"Chance {alt['prob']:.0%}, faire Quote {1 / alt['prob']:.2f}"
    est = alt.get("sporttip_est")
    if est:
        note += f", Sporttip ca. {est:.2f} ({est * alt['prob'] - 1:+.0%} gegenüber fair)"
    return note


def recent_report(engine: Engine, match_id: int, max_age_h: float) -> AgentReport | None:
    with session_scope(engine) as s:
        r = s.scalars(select(AgentReport).where(
            AgentReport.match_id == match_id, AgentReport.created_at >= utcnow() - timedelta(hours=max_age_h))
            .order_by(AgentReport.created_at.desc())).first()
        if r is not None:
            s.expunge(r)
        return r


def analyze_legs(engine: Engine, legs: list[dict], client=None, max_age_h: float = 10.0,
                 local_time=None, cached_only: bool = False, max_searches: int | None = None) -> list[dict]:
    """Scout für jede Leg (Spiel + Tipp). Gibt pro Leg {match_id, assessment, text, cached} zurück.
    `cached_only`: nur gespeicherte Berichte verwenden, keine neue (kostenpflichtige) Recherche."""
    client = client or (None if cached_only else scout.make_client())
    if client is None and not cached_only:
        return []
    searches = max_searches or int(os.getenv("AGENT_MAX_SEARCHES", "4"))
    out = []
    for leg in legs:
        cached = recent_report(engine, leg["match_id"], max_age_h)
        if cached is not None:
            intel = scout.MatchIntel.model_validate(cached.data)
            out.append({"match_id": leg["match_id"], "assessment": intel.tip_assessment,
                        "reason": intel.tip_reason, "best_tip": intel.best_tip,
                        "text": scout.format_intel(leg["match"], leg["label"], intel), "cached": True})
            continue
        if cached_only:
            continue
        # nie über das Limit: vorher prüfen, ob ein weiteres Spiel (höchstens ~0.50 $) noch hineinpasst
        if spent_today(engine) + max_cost_per_match() > daily_budget():
            log.warning("Agenten-Tagesbudget erreicht (%.2f USD)", daily_budget())
            break
        kickoff = local_time(leg["kickoff"]) if local_time else leg["kickoff"]
        try:
            res = scout.scout_match(client, leg["match"], str(kickoff), leg.get("comp_name") or leg.get("comp", ""),
                                    leg["label"], fatigue_context(engine, leg["match_id"]),
                                    alternatives=[a["label"] for a in leg.get("alternatives", [])] or None,
                                    max_searches=searches,
                                    notes=[option_note(a) for a in leg.get("alternatives", [])] or None)
        except Exception as exc:  # noqa: BLE001 – ein Fehler darf die übrigen Spiele nicht stoppen
            log.exception("Scout fehlgeschlagen für %s", leg["match"])
            out.append({"match_id": leg["match_id"], "assessment": "fehler", "reason": repr(exc)[:200],
                        "text": f"⚠️ Analyse für {leg['match']} fehlgeschlagen.", "cached": False})
            continue
        with session_scope(engine) as s:
            s.add(AgentReport(match_id=leg["match_id"], model=res.model, tip=leg["label"],
                              assessment=res.intel.tip_assessment,
                              lineup_confirmed=res.intel.home.lineup_confirmed and res.intel.away.lineup_confirmed,
                              data=res.intel.model_dump(), cost_usd=res.cost_usd))
        out.append({"match_id": leg["match_id"], "assessment": res.intel.tip_assessment,
                    "reason": res.intel.tip_reason, "best_tip": res.intel.best_tip, "cost": res.cost_usd,
                    "text": scout.format_intel(leg["match"], leg["label"], res.intel), "cached": False})
    return out


def check_tips(engine: Engine, items: list[dict], client=None, local_time=None, max_age_h: float = 10.0) -> list[dict]:
    """Für Tipps von einem Wettschein: Scout-Urteil pro Tipp.
    `items`: {match_id, match, kickoff, comp, label}. Ist das Spiel heute schon recherchiert, wird nur der Tipp
    anhand der gespeicherten Fakten bewertet (ca. 1–2 Rappen); sonst volle Recherche (ca. 0.25 $), solange das
    Tageslimit reicht. Ergebnis pro Tipp: {assessment, reason} (assessment None = nicht geprüft)."""
    client = client or scout.make_client()
    out = []
    for it in items:
        res = {"assessment": None, "reason": ""}
        if client is None:
            res["reason"] = "Agenten nicht aktiv (ANTHROPIC_API_KEY fehlt)"
            out.append(res)
            continue
        if it.get("match_id") is None:
            # Spiel nicht in unseren Daten: trotzdem über die Teamnamen recherchieren (ohne Speicher)
            if spent_today(engine) + max_cost_per_match() > daily_budget():
                res["reason"] = "Tageslimit erreicht – morgen wieder"
            else:
                try:
                    r = scout.scout_match(client, it["match"], it.get("kickoff") or "heute/demnächst",
                                          it.get("comp") or "", it["label"], "Keine Daten aus unserer Datenbank.",
                                          max_searches=int(os.getenv("AGENT_MAX_SEARCHES", "4")))
                    add_other_cost(engine, r.cost_usd)
                    res.update(assessment=r.intel.tip_assessment, reason=r.intel.tip_reason)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Recherche ohne Spiel-ID fehlgeschlagen")
                    res["reason"] = f"Prüfung fehlgeschlagen ({type(exc).__name__})"
            out.append(res)
            continue
        try:
            cached = recent_report(engine, it["match_id"], max_age_h)
            if cached is not None:
                if spent_today(engine) + 0.05 > daily_budget():
                    res["reason"] = "Tageslimit erreicht – morgen wieder"
                else:
                    intel = scout.MatchIntel.model_validate(cached.data)
                    j, cost = scout.judge_tip(client, intel, it["match"], it["label"])
                    add_other_cost(engine, cost)
                    res.update(assessment=j.tip_assessment, reason=j.tip_reason)
            else:
                r = analyze_legs(engine, [it], client=client, local_time=local_time, max_age_h=max_age_h)
                if r:
                    res.update(assessment=r[0]["assessment"], reason=r[0]["reason"])
                else:
                    res["reason"] = "Tageslimit erreicht – morgen wieder"
        except Exception as exc:  # noqa: BLE001
            log.exception("Tipp-Prüfung fehlgeschlagen")
            res["reason"] = f"Prüfung fehlgeschlagen ({type(exc).__name__})"
        out.append(res)
    return out
