"""Verfolgte Tipps: alles, was per Screenshot geschickt wurde, wird gemerkt und nach dem Spiel
mit 🟢 gewonnen / 🔴 verloren gemeldet (nur Spiele, keine Quoten)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import Engine

from fussball.data.db import session_scope
from fussball.data.schema import AppSetting, Match, utcnow
from fussball.models.builder import Part, _mask

KEY = "tracked_tips"
MAX_ITEMS = 300


def _load(s) -> list[dict]:
    row = s.get(AppSetting, KEY)
    return list(row.value) if row and isinstance(row.value, list) else []


def _save(s, items: list[dict]) -> None:
    row = s.get(AppSetting, KEY)
    items = items[-MAX_ITEMS:]
    if row:
        row.value = items
    else:
        s.add(AppSetting(key=KEY, value=items))


def _store(engine: Engine, new: list[dict]) -> int:
    with session_scope(engine) as s:
        items = _load(s)
        seen = {(i["match_id"], i["label"]) for i in items}
        added = [n for n in new if (n["match_id"], n["label"]) not in seen]
        _save(s, items + added)
    return len(added)


def _item(match_id, match, kickoff, label, parts: list[Part], batch: str, title: str) -> dict:
    return {"match_id": match_id, "match": match, "kickoff": kickoff, "label": label,
            "parts": [[p.market, p.selection, p.line, p.half] for p in parts], "batch": batch, "title": title,
            "added": utcnow().isoformat(), "won": None, "done": False, "reported": False}


def track(engine: Engine, rows: list[dict], batch: str | None = None) -> int:
    """Tipps aus einem geprüften Schein merken (nur Spiele, die wir kennen und deren Tipp berechenbar ist)."""
    import uuid

    from fussball.agents.slip import to_parts

    batch = batch or f"slip-{uuid.uuid4().hex[:8]}"
    new = []
    for r in rows:
        parts = to_parts(r["leg"])
        if r.get("match_id") is not None and parts:
            new.append(_item(r["match_id"], r["match"], r.get("kickoff"), r["leg"].market_text, parts, batch,
                             "📋 <b>Ergebnis deines Scheins</b>"))
    return _store(engine, new)


def market_parts(market: str, selection: str) -> list[Part] | None:
    """Tipp-Code der Bot-Tipps ('OU1.5', 'HOME1.5', 'H1_DC', 'BB' …) → Teile für die Auswertung."""
    half = "ft"
    if market.startswith("H1_"):
        half, market = "1h", market[3:]
    for prefix in ("OU", "HOME", "AWAY"):
        if market.startswith(prefix) and market != prefix:
            return [Part(prefix, selection, float(market[len(prefix):]), half)]
    if market in ("1X2", "DC", "BTTS"):
        return [Part(market, selection, 0.0, half)]
    return None


def track_tips(engine: Engine, tips: list[dict], day: str) -> int:
    """Die Tipps, die der Bot verschickt hat (Top-Tipps), merken – Ergebnis kommt nach dem letzten Spiel."""
    new = []
    for t in tips:
        parts = ([Part(m, sel, float(line), half) for m, sel, line, half in t["parts"]] if t.get("parts")
                 else market_parts(t["market"], t["selection"]))
        if parts:
            new.append(_item(t["match_id"], t["match"], t["kickoff"], t["label"], parts, f"top-{day}",
                             f"📋 <b>Ergebnis Top-Tipps {day[8:10]}.{day[5:7]}.</b>"))
    return _store(engine, new)


def part_won(p: Part, fh: int, fa: int, hh: int | None, ha: int | None) -> bool | None:
    if p.half == "1h":
        if hh is None or ha is None:
            return None
        return bool(_mask(p, np.array(hh), np.array(ha)))
    return bool(_mask(p, np.array(fh), np.array(fa)))


def fill_halftime(engine: Engine, client=None, limit: int = 6) -> int:
    """Für beendete Spiele mit Halbzeit-Tipp, aber ohne Halbzeitstand: den Agenten nachschlagen lassen
    (ca. 2–5 Rappen pro Spiel, zählt zum Tageslimit). Ergebnis wird beim Spiel gespeichert."""
    from fussball.agents import runner, scout

    with session_scope(engine) as s:
        need = []
        for it in _load(s):
            if it["done"] or not any(p[3] == "1h" for p in it["parts"]):
                continue
            m = s.get(Match, it["match_id"])
            if m is not None and m.status == "finished" and m.ft_home is not None and m.ht_home is None:
                need.append((m.id, it["match"], m.kickoff_utc.strftime("%d.%m.%Y"), f"{m.ft_home}:{m.ft_away}"))
    need = list({n[0]: n for n in need}.values())[:limit]
    if not need:
        return 0
    client = client or scout.make_client()
    if client is None:
        return 0
    filled = 0
    for mid, match, date, final in need:
        if runner.spent_today(engine) + 0.1 > runner.daily_budget():
            break
        try:
            ht, cost = scout.halftime_score(client, match, date, final)
        except Exception:  # noqa: BLE001
            continue
        runner.add_other_cost(engine, cost)
        fh, fa = (int(x) for x in final.split(":"))
        if ht.found and ht.ht_home is not None and ht.ht_away is not None and ht.ht_home <= fh and ht.ht_away <= fa:
            with session_scope(engine) as s:
                m = s.get(Match, mid)
                m.ht_home, m.ht_away = ht.ht_home, ht.ht_away
            filled += 1
    return filled


def settle(engine: Engine) -> list[dict]:
    """Beendete Spiele abrechnen; gibt die neu entschiedenen Tipps zurück (won True/False/None)."""
    out = []
    with session_scope(engine) as s:
        items = _load(s)
        for it in items:
            if it["done"]:
                continue
            m = s.get(Match, it["match_id"])
            if m is None or m.status != "finished" or m.ft_home is None:
                continue
            if m.ht_home is None and any(p[3] == "1h" for p in it["parts"]) and \
                    m.kickoff_utc > utcnow() - timedelta(days=2):
                continue  # Halbzeitstand fehlt noch – der Agent schlägt ihn nach
            results = [part_won(Part(mk, sel, float(line), half), m.ft_home, m.ft_away, m.ht_home, m.ht_away)
                       for mk, sel, line, half in it["parts"]]
            it["won"] = None if None in results else all(results)
            it["score"] = f"{m.ft_home}:{m.ft_away}"
            it["done"] = True
            out.append(dict(it))
        _save(s, items)
    return out


def finished_batches(engine: Engine) -> list[tuple[str, list[dict]]]:
    """Scheine/Tipp-Nachrichten, bei denen ALLE Spiele fertig sind und die noch nicht gemeldet wurden.
    Spiele ohne Ergebnis nach 2 Tagen zählen als fertig (⚪), damit die Meldung nicht ewig wartet."""
    from datetime import datetime, timedelta

    out = []
    with session_scope(engine) as s:
        items = _load(s)
        by: dict[str, list[dict]] = {}
        for it in items:
            by.setdefault(it.get("batch", "alt"), []).append(it)
        for batch, its in by.items():
            if any(it.get("reported") for it in its):
                continue
            stale = lambda it: it.get("kickoff") and datetime.fromisoformat(it["kickoff"]) < utcnow() - timedelta(days=2)  # noqa: E731
            if all(it["done"] or stale(it) for it in its):
                for it in its:
                    it["reported"] = True
                    if not it["done"]:
                        it["done"], it["won"] = True, None
                out.append((its[0].get("title", "📋 <b>Ergebnisse</b>"), [dict(i) for i in its]))
        _save(s, items)
    return out


def recent(engine: Engine, n: int = 20) -> list[dict]:
    with session_scope(engine) as s:
        return _load(s)[-n:]
