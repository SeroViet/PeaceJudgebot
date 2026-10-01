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


def daily_budget() -> float:
    return float(os.getenv("AGENT_DAILY_BUDGET_USD", "1.5"))


def spent_today(engine: Engine) -> float:
    today = utcnow().date()
    with session_scope(engine) as s:
        rows = s.scalars(select(AgentReport.cost_usd).where(
            AgentReport.created_at >= datetime.combine(today, datetime.min.time()))).all()
    return float(sum(rows))


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
    searches = max_searches or int(os.getenv("AGENT_MAX_SEARCHES", "6"))
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
        if spent_today(engine) >= daily_budget():
            log.warning("Agenten-Tagesbudget erreicht (%.2f USD)", daily_budget())
            break
        kickoff = local_time(leg["kickoff"]) if local_time else leg["kickoff"]
        try:
            res = scout.scout_match(client, leg["match"], str(kickoff), leg.get("comp_name") or leg.get("comp", ""),
                                    leg["label"], fatigue_context(engine, leg["match_id"]),
                                    alternatives=[a["label"] for a in leg.get("alternatives", [])] or None,
                                    max_searches=searches)
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
