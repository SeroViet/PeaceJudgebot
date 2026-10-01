"""Zwischengespeicherter Tagesplan (Prognosen, Einzeltipps, Kombis) und Jobs."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from fussball import service
from fussball.config import get_settings
from fussball.data.schema import utcnow

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Zurich")
_lock = threading.Lock()
_status: dict = {"running": False, "last_run": None, "last_error": None, "last_update": None}


def plan_path():
    return get_settings().storage_dir / "plan.json"


def local(dt_iso: str | datetime) -> datetime:
    dt = datetime.fromisoformat(dt_iso) if isinstance(dt_iso, str) else dt_iso
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)


def serialize_plan(plan: service.DailyPlan) -> dict:
    singles = []
    for i, (t, stake) in enumerate(plan.singles, start=1):
        singles.append({"id": str(i), "match_id": t.match_id, "match": t.match, "kickoff": t.kickoff,
                        "comp": t.comp, "market": t.market, "line": t.line, "selection": t.selection,
                        "label": t.label, "prob": t.prob, "odds": t.odds, "fair_odds": t.fair_odds,
                        "min_odds": round(1 / t.prob * (1 + plan.config["singles"]["min_edge"]), 2),
                        "edge": t.edge, "stake": stake, "bookmaker": t.bookmaker})
    combos = []
    for i, c in enumerate(plan.combos, start=1):
        combos.append({
            "id": f"K{i}", "variant": c.variant, "prob": c.prob, "odds": c.odds, "ev": c.ev,
            "margin": c.bookmaker_margin, "streak5": c.losing_streak_prob(5), "streak10": c.losing_streak_prob(10),
            "stake": c.stake(plan.config["bankroll"], plan.config["combos"]["kelly_fraction"],
                             plan.config["combos"]["max_stake_pct"]),
            "legs": [{"match_id": t.match_id, "match": t.match, "kickoff": t.kickoff, "comp": t.comp,
                      "market": t.market, "line": t.line, "selection": t.selection, "label": t.label,
                      "prob": t.prob, "odds": t.odds, "edge": t.edge,
                      "min_odds": round(1 / t.prob * (1 + plan.config["combos"]["leg_min_edge"]), 2)}
                     for t in c.legs],
        })  # fmt: skip
    safe = [{**t, "id": f"S{i}"} for i, t in enumerate(plan.safe, start=1)]
    return {
        "generated_at": utcnow().isoformat(),
        "safe": safe,
        "day_combos": [{**c, "id": f"T{i}"} for i, c in enumerate(plan.day_combos, start=1)],
        "risky_combos": [{**c, "id": f"R{i}"} for i, c in enumerate(plan.risky_combos, start=1)],
        "krass_combos": [{**c, "id": f"X{i}"} for i, c in enumerate(plan.krass_combos, start=1)],
        "forecasts": [f.to_dict() for f in plan.forecasts],
        "singles": singles,
        "combos": combos,
        "blocked_leagues": plan.blocked_leagues,
        "currency": plan.config.get("currency", "CHF"),
    }


def load_plan() -> dict:
    path = plan_path()
    if not path.exists():
        return {"generated_at": None, "forecasts": [], "singles": [], "combos": [], "blocked_leagues": [],
                "currency": "CHF"}
    return json.loads(path.read_text(encoding="utf-8"))


def refresh(engine: Engine, fetch: bool = True, days: int = 3, agents: bool = False) -> dict:
    """Daten laden, Wetten abrechnen, Prognosen und Plan neu berechnen."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "läuft bereits"}
    _status["running"] = True
    try:
        info = {}
        old = load_plan()
        if fetch:
            info["update"] = service.update_data(engine, live=service.live_sports(engine, old))
            _status["last_update"] = utcnow().isoformat()
        details: list = []
        info["settled"] = service.settle_bets(engine, details)
        info["settled_details"] = details
        plan = service.daily_plan(engine, days=days)
        info["agent"] = []
        # Agenten kosten Geld: nur auf Anforderung (täglicher Lauf), nie bei jedem Neustart/Refresh.
        # Ohne Agentenlauf werden die letzten Bewertungen (Cache) übernommen, ohne neue Kosten.
        try:
            info["agent"] = service.apply_agents(engine, plan, cached_only=not agents)
        except Exception:  # noqa: BLE001 – ohne Agenten weiterarbeiten
            log.exception("Agenten fehlgeschlagen")
        rc = plan.config.get("risky_combo", {})
        plan.risky_combos = service.risky_combos(plan.all_forecasts or plan.forecasts, plan.day_combos,
                                                 rc.get("size", 3), rc.get("min_prob", 0.60), rc.get("max_prob", 0.72))
        try:
            info["agent"] += service.apply_agents_risky(engine, plan, cached_only=not agents)
        except Exception:  # noqa: BLE001
            log.exception("Agenten (Risiko-Kombi) fehlgeschlagen")
        kc = plan.config.get("krass_combo", {})
        plan.krass_combos = [{**c, "krass": True} for c in service.risky_combos(
            plan.all_forecasts or plan.forecasts, plan.day_combos + plan.risky_combos, kc.get("size", 5),
            kc.get("min_prob", 0.55), kc.get("max_prob", 0.70), markets=service.KRASS_MARKETS)]
        books = plan.config.get("bookmakers") or None
        for combos in (plan.day_combos, plan.risky_combos, plan.krass_combos):
            service.attach_book_odds(engine, combos, books)
        from fussball.agents import slip

        table = slip.ratios(engine)
        for c in plan.day_combos + plan.risky_combos + plan.krass_combos:
            for leg in c["legs"]:
                leg["sporttip_est"] = slip.estimate(table, leg["market"], 1 / leg["prob"])
        data = serialize_plan(plan)
        service.record_served(engine, plan.day_combos)
        info["combo_results"] = service.evaluate_served(engine)
        path = plan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, default=service.np_default, ensure_ascii=False), encoding="utf-8")
        info["changes"] = diff_plans(old, data)
        _status.update(last_run=utcnow().isoformat(), last_error=None)
        return info
    except Exception as exc:  # noqa: BLE001
        log.exception("Aktualisierung fehlgeschlagen")
        _status["last_error"] = f"{utcnow():%d.%m. %H:%M} {exc!r}"
        raise
    finally:
        _status["running"] = False
        _lock.release()


def needs_bootstrap(engine: Engine, min_matches: int = 1000) -> bool:
    from sqlalchemy import func, select

    if service.lite_mode():
        return False
    from fussball.data.db import session_scope
    from fussball.data.schema import Match

    with session_scope(engine) as s:
        return s.execute(select(func.count(Match.id))).scalar_one() < min_matches


def bootstrap(engine: Engine, seasons_back: int = 3) -> list[dict]:
    """Erster Start auf einem neuen Server: Historie laden (laufende + 3 Vorsaisons,
    Hauptligen und 2. Ligen für Aufsteiger). Dauert einige Minuten."""
    from fussball.cli import current_season_code
    from fussball.config import load_leagues
    from fussball.data import football_data
    from fussball.models.backtest import RELATED_LEAGUES

    leagues = load_leagues()
    main = service.enabled_leagues(engine)
    codes = sorted({c for m in main for c in [m, *RELATED_LEAGUES.get(m, [])] if c in leagues})
    start = int(current_season_code()[:2])
    seasons = [f"{(start - i) % 100:02d}{(start - i + 1) % 100:02d}" for i in range(seasons_back, -1, -1)]
    log.info("Erststart: lade %s für %s", seasons, codes)
    return football_data.run_import(engine, [leagues[c] for c in codes], seasons,
                                    get_settings().storage_dir / "raw" / "football-data")


def diff_plans(old: dict, new: dict) -> list[str]:
    """Tipps, die neu dazukamen oder gestrichen wurden (für Telegram-Alarm)."""
    key = lambda s: (s["match_id"], s["market"], s["selection"])  # noqa: E731
    before = {key(s): s for s in old.get("singles", [])}
    after = {key(s): s for s in new.get("singles", [])}
    out = [f"➕ Neu: {s['match']} – {s['label']} @ {s['odds']:.2f}" for k, s in after.items() if k not in before]
    out += [f"❌ Gestrichen: {s['match']} – {s['label']}" for k, s in before.items() if k not in after]
    return out


def status() -> dict:
    return dict(_status)
