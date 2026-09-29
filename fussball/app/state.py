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
    return {
        "generated_at": utcnow().isoformat(),
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


def refresh(engine: Engine, fetch: bool = True, days: int = 3) -> dict:
    """Daten laden, Wetten abrechnen, Prognosen und Plan neu berechnen."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "läuft bereits"}
    _status["running"] = True
    try:
        info = {}
        if fetch:
            info["update"] = service.update_data(engine)
            _status["last_update"] = utcnow().isoformat()
        info["settled"] = service.settle_bets(engine)
        old = load_plan()
        plan = service.daily_plan(engine, days=days)
        data = serialize_plan(plan)
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
