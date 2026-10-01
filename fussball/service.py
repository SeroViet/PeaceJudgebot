"""Anwendungslogik für App, Telegram-Bot und Scheduler.

- update_data():      laufende Saison + kommende Spiele laden
- predict_upcoming(): Wahrscheinlichkeiten, faire Quoten, Mindestquoten, Tipps
- settle_bets():      offene Wetten nach Spielende abrechnen (P/L, CLV)
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, delete, select

from fussball.betting.combos import Combo, build_combos
from fussball.betting.value import Tip, load_betting_config, select_singles
from fussball.cli import current_season_code
from fussball.config import CONFIG_DIR, get_settings, load_leagues
from fussball.data import football_data
from fussball.data.db import session_scope
from fussball.data.point_in_time import latest_odds_as_of
from fussball.data.schema import AppSetting, Bet, Match, Prediction, Team, utcnow
from fussball.models import dixon_coles as dc
from fussball.models.backtest import ModelConfig, blend_1x2, blend_ou, load_frame, walk_forward
from fussball.models.devig import fair_probs
from fussball.models.markets import all_markets, correct_scores

log = logging.getLogger(__name__)

MODEL_NAME = "dc-elo-market"
MODEL_VERSION = "1.0"
LABELS_1X2 = {"H": "Heimsieg", "D": "Unentschieden", "A": "Auswärtssieg"}
LABELS_OU = {"O": "Über 2.5 Tore", "U": "Unter 2.5 Tore"}


# ----------------------------------------------------------------------------- Einstellungen


def get_config(engine: Engine) -> dict:
    """betting.yaml + in der App gespeicherte Änderungen."""
    cfg = load_betting_config()
    with session_scope(engine) as s:
        row = s.get(AppSetting, "betting")
        if row and isinstance(row.value, dict):
            _deep_update(cfg, row.value)
    return cfg


def save_config(engine: Engine, overrides: dict) -> None:
    with session_scope(engine) as s:
        row = s.get(AppSetting, "betting")
        merged = copy.deepcopy(row.value) if row and isinstance(row.value, dict) else {}
        _deep_update(merged, overrides)
        if row:
            row.value = merged
        else:
            s.add(AppSetting(key="betting", value=merged))


def _deep_update(base: dict, upd: dict) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def model_summary() -> dict:
    """Ergebnis des letzten Backtests (Gewichte, Qualität je Liga)."""
    path = get_settings().storage_dir / "backtest_summary.json"
    if not path.exists():  # mitgelieferter Stand (Backtest 2020–2026), bis ein eigener Lauf existiert
        path = CONFIG_DIR / "backtest_summary.json"
        if not path.exists():
            return {}
    return json.loads(path.read_text(encoding="utf-8"))


def enabled_leagues(engine: Engine) -> list[str]:
    cfg = get_config(engine)
    leagues = load_leagues()
    codes = [c for c, v in leagues.items() if v.get("enabled")]
    codes = [c for c in codes if c not in cfg.get("force_disabled_leagues", [])]
    return codes + [c for c in cfg.get("force_enabled_leagues", []) if c not in codes and c in leagues]


# ----------------------------------------------------------------------------- Daten


def live_sports(engine: Engine, plan: dict, hours: float = 30.0) -> list[tuple[str, str]]:
    """(sport_key, Titel) der Wettbewerbe, deren Spiele in der Tageskombi der nächsten Stunden stehen."""
    from fussball.data.odds_api import SPORT_KEYS
    from fussball.data.schema import Competition

    now, out = utcnow(), {}
    codes = {l["comp"] for c in plan.get("day_combos", []) for l in c["legs"]
             if now < datetime.fromisoformat(l["kickoff"]) <= now + timedelta(hours=hours)}
    if not codes:
        return []
    with session_scope(engine) as s:
        names = dict(s.execute(select(Competition.code, Competition.name).where(Competition.code.in_(codes))).all())
    for code in sorted(codes):
        out[SPORT_KEYS.get(code, code)] = names.get(code, code)
    return list(out.items())


def lite_mode() -> bool:
    """Sparmodus (Standard): nur Pinnacle-Quoten weltweit, ohne Liga-Historie und eigenes Modell.
    Braucht ~150 MB statt >1 GB RAM und reicht für Tageskombis und sichere Tipps, weil das eigene
    Modell im Backtest ohnehin in keiner Liga freigegeben ist. FULL_MODEL=1 schaltet alles ein."""
    return os.getenv("FULL_MODEL", "0") != "1"


def update_data(engine: Engine, live: list[tuple[str, str]] | None = None) -> dict:
    """Laufende Saison (inkl. 2. Ligen für Aufsteiger) und kommende Spiele laden."""
    from fussball.models.backtest import RELATED_LEAGUES

    if lite_mode():
        return {"odds_api": world_scan(engine, live=live), "at": utcnow().isoformat()}

    settings = get_settings()
    leagues = load_leagues()
    main = enabled_leagues(engine)
    codes = sorted({c for m in main for c in [m, *RELATED_LEAGUES.get(m, [])] if c in leagues})
    season = current_season_code()
    results = football_data.run_import(engine, [leagues[c] for c in codes], [season],
                                       settings.storage_dir / "raw" / "football-data", refresh=True)
    fixtures = {}
    try:
        content = football_data.download_fixtures()
        with session_scope(engine) as s:
            fixtures = football_data.import_fixtures(s, {c: leagues[c] for c in main}, season, content)
    except Exception as exc:  # noqa: BLE001
        log.exception("fixtures.csv fehlgeschlagen")
        fixtures = {"error": repr(exc)}
    odds_api = world_scan(engine, live=live)
    return {"seasons": results, "fixtures": fixtures, "odds_api": odds_api, "at": utcnow().isoformat()}


# ----------------------------------------------------------------------------- Prognosen


@dataclass
class MatchForecast:
    match_id: int
    comp: str
    comp_name: str
    kickoff_utc: datetime
    home: str
    away: str
    lam: float
    mu: float
    probs_1x2: dict[str, float]
    probs_ou: dict[str, float]
    model_1x2: dict[str, float]
    market_1x2: dict[str, float] | None
    market_ou: dict[str, float] | None
    elo_diff: float
    odds: dict[str, dict[str, float]]  # {"avg": {"H": 2.1, ...}, "max": {...}}
    other_markets: dict[str, dict[str, float]] = field(default_factory=dict)
    top_scores: list[tuple[str, float]] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)
    league_ok: bool = True
    mode: str = "markt"  # "modell" = Modell+Markt (Liga freigegeben), "markt" = faire Marktquote
    reference: str | None = None  # PS | BFE | Avg (1X2)
    reference_ou: str | None = None  # Referenz für Über/Unter 2.5
    ou_ok: bool = False  # Tipps auf Über/Unter nur mit eigener Referenz
    implied: dict[str, dict[str, float]] = field(default_factory=dict)  # alle Märkte aus Pinnacle
    implied_rates: tuple[float, float] | None = None

    def fair(self, sel: str) -> float:
        p = self.probs_1x2.get(sel) or self.probs_ou.get(sel)
        return 1 / p if p else float("nan")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kickoff_utc"] = self.kickoff_utc.isoformat()
        return d


def _factors(r: pd.Series, params: dc.DixonColesParams | None, market: dict | None, model: dict) -> list[str]:
    out = [f"Erwartete Tore: {r['home']} {r['lam']:.2f} – {r['mu']:.2f} {r['away']}"]
    if params is not None:
        idx = params.index()
        for team in (r["home"], r["away"]):
            if team in idx:
                a, d = params.attack[idx[team]], params.defence[idx[team]]
                out.append(f"{team}: Angriff {a:+.2f}, Abwehr {d:+.2f} (0 = Ligaschnitt)")
            else:
                out.append(f"{team}: kaum Daten, Aufsteiger-Prior verwendet")
    out.append(f"ELO-Differenz inkl. Heimvorteil: {r['elo_diff']:+.0f}")
    if market:
        diff = {k: model[k] - market[k] for k in model}
        k = max(diff, key=lambda x: abs(diff[x]))
        out.append(f"Grösste Abweichung Modell vs. Markt: {LABELS_1X2[k]} {diff[k]:+.1%}")
    return out


def predict_upcoming(engine: Engine, days: int = 4, now: datetime | None = None) -> list[MatchForecast]:
    now = now or utcnow()
    summary = model_summary()
    leagues = load_leagues()
    frame = load_frame(engine, allowed_books=get_config(engine).get("bookmakers") or None)
    upcoming = frame[(frame["status"] == "scheduled") & (frame["kickoff_utc"] > now)
                     & (frame["kickoff_utc"] <= now + timedelta(days=days))]
    forecasts: list[MatchForecast] = []
    for comp in sorted(set(upcoming["comp"]) & set(enabled_leagues(engine))):
        comp_sum = summary.get("leagues", {}).get(comp, {})
        cfg = ModelConfig(**summary.get("model_config", {}))
        start = upcoming.loc[upcoming["comp"] == comp, "slot"].min()
        start = (start if pd.notna(start) else now) - timedelta(hours=1)
        wf = walk_forward(frame, comp, cfg, start=pd.Timestamp(start))
        wf = wf[(wf["status"] == "scheduled") & (wf["kickoff_utc"] > now)
                & (wf["kickoff_utc"] <= now + timedelta(days=days))]
        if wf.empty:
            continue
        model_ok = bool(comp_sum.get("beats_market", False))
        mv_ok = bool(summary.get("market_value", {}).get("enabled", False))
        if model_ok:
            w_dc, w_elo, w_ou = comp_sum.get("w_dc", 0.0), comp_sum.get("w_elo", 0.0), comp_sum.get("w_ou", 0.0)
        else:  # Modell ohne nachgewiesenen Vorteil: nur der faire Marktpreis zählt
            w_dc = w_elo = w_ou = 0.0
        p1 = blend_1x2(wf, w_dc, w_elo)
        p2 = blend_ou(wf, w_ou)
        for idx, r in wf.iterrows():
            m = dc.score_matrix(r["lam"], r["mu"], 0.0)
            model = {k: r[f"dc_{k}"] for k in "HDA"}
            market = {k: r[f"mkt_{k}"] for k in "HDA"} if pd.notna(r.get("mkt_H")) else None
            market_ou = {k: r[f"mkt_{k}"] for k in "OU"} if pd.notna(r.get("mkt_O")) else None
            odds = {src: {k: float(r[f"{src}_{k}"]) for k in "HDAOU" if pd.notna(r.get(f"{src}_{k}"))}
                    for src in ("avg", "max", "best")}
            odds["books"] = {k: str(r[f"bestbook_{k}"]) for k in "HDAOU" if pd.notna(r.get(f"bestbook_{k}"))}
            probs_1x2 = {k: float(p1.loc[idx, f"p_{k}"]) for k in "HDA"}
            forecasts.append(MatchForecast(
                match_id=int(r["match_id"]), comp=comp, comp_name=leagues[comp]["name"],
                kickoff_utc=r["kickoff_utc"].to_pydatetime(), home=r["home"], away=r["away"],
                lam=float(r["lam"]), mu=float(r["mu"]), probs_1x2=probs_1x2,
                probs_ou={k: float(p2.loc[idx, f"p_{k}"]) for k in "OU"}, model_1x2=model,
                market_1x2=market, market_ou=market_ou, elo_diff=float(r["elo_diff"]), odds=odds,
                other_markets={k: v for k, v in all_markets(m).items() if k not in ("1X2", "OU2.5")},
                top_scores=correct_scores(m, 6), factors=_factors(r, None, market, model),
                league_ok=model_ok or (mv_ok and r.get("market_book") == "PS"),
                mode="modell" if model_ok else "markt", reference=r.get("market_book"),
                reference_ou=r.get("ou_book"), ou_ok=model_ok or (mv_ok and r.get("ou_book") == "PS"),
            ))  # fmt: skip
    forecasts.sort(key=lambda f: f.kickoff_utc)
    _add_implied(engine, forecasts)
    _store_predictions(engine, forecasts)
    return forecasts


def _odds_state(engine: Engine) -> dict:
    today = utcnow().date().isoformat()
    with session_scope(engine) as s:
        row = s.get(AppSetting, "odds_api_state")
        st = dict(row.value) if row and isinstance(row.value, dict) else {}
    if st.get("day") != today:
        st = {"day": today, "spent": 0, "last": st.get("last", {})}
    return st


def _save_odds_state(engine: Engine, st: dict) -> None:
    with session_scope(engine) as s:
        row = s.get(AppSetting, "odds_api_state")
        if row:
            row.value = st
        else:
            s.add(AppSetting(key="odds_api_state", value=st))


def world_scan(engine: Engine, hours: float = 30.0, live: list[tuple[str, str]] | None = None) -> dict:
    """Alle Fussball-Wettbewerbe weltweit (inkl. Nations League, WM-Quali) nach Spielen in den
    nächsten `hours` Stunden durchsuchen (gratis) und für die Wettbewerbe mit den meisten
    Spielen Quoten holen (2 Credits je Wettbewerb), innerhalb des Tagesbudgets.
    Danach Ergebnisse für Wettbewerbe mit offenen, vergangenen Spielen (2 Credits).
    `live`: (sport_key, Titel) der Wettbewerbe der aktuellen Tageskombi – deren Quoten werden
    alle ODDS_API_LIVE_HOURS neu geholt; dafür bleiben ODDS_API_LIVE_CREDITS im Budget reserviert."""
    from fussball.data.odds_api import OddsApiClient, import_generic, import_scores, upcoming_counts

    client = OddsApiClient()
    if not client.configured:
        return {"error": "ODDS_API_KEY fehlt – ohne Pinnacle-Quoten keine Tipps"}
    budget = int(os.getenv("ODDS_API_DAILY_CREDITS", "16"))
    interval = float(os.getenv("ODDS_API_INTERVAL_HOURS", "24"))
    reserve = int(os.getenv("ODDS_API_LIVE_CREDITS", "4")) if live else 0
    live_every = float(os.getenv("ODDS_API_LIVE_HOURS", "3"))
    st = _odds_state(engine)
    out: dict = {"fetched": {}, "scores": {}}
    try:
        # Ergebnisse zuerst: offene Spiele (Quelle odds-api), deren Anpfiff > 2.5 h her ist
        from fussball.data.schema import Competition

        with session_scope(engine) as s:
            due = s.execute(select(Competition.code).join(Match, Match.competition_id == Competition.id).where(
                Match.status == "scheduled", Match.source == "odds-api",
                Match.kickoff_utc < utcnow() - timedelta(hours=2.5),
                Match.kickoff_utc > utcnow() - timedelta(days=3)).distinct()).scalars().all()
        from fussball.data.odds_api import SPORT_KEYS

        for code in due:
            if st["spent"] + 2 > budget:
                break
            sport = SPORT_KEYS.get(code, code)
            with session_scope(engine) as s:
                out["scores"][sport] = import_scores(s, client, sport)
            st["spent"] += 2
        for sport, title, n in upcoming_counts(client, hours):
            last = st["last"].get(sport)
            if last and utcnow() - datetime.fromisoformat(last) < timedelta(hours=interval):
                continue
            if st["spent"] + 2 > budget - reserve:
                out.setdefault("skipped", []).append(f"{title} ({n})")
                continue
            with session_scope(engine) as s:
                out["fetched"][title] = import_generic(s, client, sport, title)
            st["spent"] += 2
            st["last"][sport] = utcnow().isoformat()
        # Live-Quoten für die Spiele der Tageskombi
        for sport, title in live or []:
            last = st["last"].get(sport)
            if last and utcnow() - datetime.fromisoformat(last) < timedelta(hours=live_every):
                continue
            if st["spent"] + 2 > budget:
                break
            with session_scope(engine) as s:
                out.setdefault("live", {})[title] = import_generic(s, client, sport, title)
            st["spent"] += 2
            st["last"][sport] = utcnow().isoformat()
    except Exception as exc:  # noqa: BLE001
        log.exception("Weltweiter Scan fehlgeschlagen")
        out["error"] = repr(exc)
    finally:
        _save_odds_state(engine, st)
    out.update(credits_today=st["spent"], credits_left=client.remaining)
    return out


def market_forecasts(engine: Engine, hours: float = 72.0, now: datetime | None = None) -> list[MatchForecast]:
    """Prognosen allein aus Pinnacle-Quoten – für jeden Wettbewerb weltweit, ohne Historie."""
    from fussball.data.schema import Competition, Odds
    from fussball.models.implied import fit_rates, implied_markets

    now = now or utcnow()
    out = []
    with session_scope(engine) as s:
        rows = s.execute(select(Match, Competition.code, Competition.name).join(
            Competition, Competition.id == Match.competition_id).where(
            Match.status == "scheduled", Match.kickoff_utc > now, Match.kickoff_utc <= now + timedelta(hours=hours))
        ).all()
        for m, code, cname in rows:
            prices: dict[str, float] = {}
            for sel, price in s.execute(select(Odds.selection, Odds.price).where(
                    Odds.match_id == m.id, Odds.bookmaker == "PS", Odds.market == "1X2",
                    Odds.is_closing.is_(False)).order_by(Odds.known_at)).all():
                prices[sel] = price
            if set(prices) != {"H", "D", "A"}:
                continue
            p1 = dict(zip("HDA", fair_probs([prices["H"], prices["D"], prices["A"]])))
            tot = pinnacle_total(s, m.id)
            lam, mu = fit_rates(p1, *(tot if tot else (None, None)))
            home, away = s.get(Team, m.home_team_id).name, s.get(Team, m.away_team_id).name
            implied = implied_markets(lam, mu)
            out.append(MatchForecast(m.id, code, cname, m.kickoff_utc, home, away, lam, mu, p1,
                                     implied["OU2.5"], p1, p1, None, 0.0, {}, league_ok=True, mode="markt",
                                     reference="PS", implied=implied, implied_rates=(lam, mu)))
    return sorted(out, key=lambda f: f.kickoff_utc)


def pinnacle_total(session, match_id: int) -> tuple[float, float] | None:
    """(Linie, faire P(Über)) der jüngsten Pinnacle-Tore-Linie, bevorzugt nahe 2.5."""
    from fussball.data.schema import Odds

    rows = session.execute(
        select(Odds.line, Odds.selection, Odds.price, Odds.known_at)
        .where(Odds.match_id == match_id, Odds.bookmaker == "PS", Odds.market == "OU", Odds.is_closing.is_(False))
    ).all()
    by_line: dict[float, dict] = {}
    for line, sel, price, known in sorted(rows, key=lambda r: r[3]):
        by_line.setdefault(line, {})[sel] = price
    full = {ln: v for ln, v in by_line.items() if "O" in v and "U" in v}
    if not full:
        return None
    line = min(full, key=lambda ln: abs(ln - 2.5))
    return line, fair_probs([full[line]["O"], full[line]["U"]])[0]


def _add_implied(engine: Engine, forecasts: list[MatchForecast]) -> None:
    """Alle Märkte aus Pinnacle ableiten (nur wenn Pinnacle-1X2 vorliegt)."""
    from fussball.models.implied import fit_rates, implied_markets

    with session_scope(engine) as s:
        for f in forecasts:
            if f.reference != "PS" or not f.market_1x2:
                continue
            tot = pinnacle_total(s, f.match_id)
            lam, mu = fit_rates(f.market_1x2, *(tot if tot else (None, None)))
            f.implied_rates = (lam, mu)
            f.implied = implied_markets(lam, mu)


SAFE_FAMILIES = {"1X2": "sieg", "DC": "sieg", "DNB": "sieg", "BTTS": "btts"}


def safe_tips(forecasts: list[MatchForecast], min_prob: float = 0.70, max_prob: float = 0.90,
              per_match: int = 2) -> list[dict]:
    """Tipps mit hoher Trefferwahrscheinlichkeit (aus Pinnacle abgeleitet), wie auf dem
    Sporttip-Schein. Pro Spiel höchstens `per_match` Tipps aus verschiedenen Markt-Familien;
    innerhalb des Bereichs wird die höhere Quote (niedrigere Wahrscheinlichkeit) bevorzugt."""
    from fussball.models.implied import label

    out = []
    for f in forecasts:
        if not f.implied:
            continue
        cands = []
        for market, sels in f.implied.items():
            for sel, p in sels.items():
                if min_prob <= p <= max_prob and (market, sel) not in EXCLUDED_TIPS:
                    fam = SAFE_FAMILIES.get(market, "tore" if market.startswith("OU") else market[:4])
                    cands.append((p, market, sel, fam))
        cands.sort(key=lambda c: c[0])  # knapp über der Schwelle = höhere faire Quote
        used: set[str] = set()
        for p, market, sel, fam in cands:
            if fam in used or len(used) >= per_match:
                continue
            used.add(fam)
            out.append({"match_id": f.match_id, "match": f"{f.home} – {f.away}", "kickoff": f.kickoff_utc.isoformat(),
                        "comp": f.comp, "market": market, "selection": sel, "label": label(market, sel, f.home, f.away),
                        "prob": p, "fair_odds": 1 / p})
    out.sort(key=lambda t: (t["kickoff"], -t["prob"]))
    return out


# Teamtore ("Bayern trifft", "Bayern über 1.5 Tore") sind im Backtest im Bereich 70–88 % genauso gut
# kalibriert wie Über/Unter gesamt und bringen Abwechslung in die Kombi.
COMBO_MARKETS = ("1X2", "DC", "OU1.5", "OU2.5", "OU3.5", "BTTS", "HOME0.5", "AWAY0.5", "HOME1.5", "AWAY1.5")
# Keine Tipps wie "12 (kein Unentschieden)", "X" oder "Team unter …": wenig aussagekräftig
EXCLUDED_TIPS = {("DC", "12"), ("1X2", "D"), ("HOME0.5", "U"), ("AWAY0.5", "U"), ("HOME1.5", "U"), ("AWAY1.5", "U")}
MAX_SAME_TIP = 2  # höchstens 2× derselbe Tipp (z. B. "Über 1.5 Tore") pro Kombi
# Tore-Tipps (Über/Unter, beide treffen) werden bevorzugt, solange sie nur wenig unsicherer sind
GOAL_BONUS = 0.04


def _is_goal_market(market: str) -> bool:
    return market.startswith(("OU", "HOME", "AWAY")) or market == "BTTS"


def best_tip_per_match(forecasts: list[MatchForecast], min_prob: float, max_prob: float,
                       markets: tuple[str, ...] = COMBO_MARKETS, n_alternatives: int = 4,
                       exclude: set = EXCLUDED_TIPS) -> list[dict]:
    """Pro Spiel genau ein Tipp aus den erlaubten Märkten im Bereich: der sicherste, wobei Tore-Tipps
    einen kleinen Vorzug bekommen. Triviale Märkte (Über 0.5 Tore) und "12"/"X" sind ausgeschlossen.
    `alternatives`: weitere Tipps im Bereich, aus denen der Scout nach seiner Recherche wählen darf."""
    from fussball.models.implied import label

    out = []
    for f in forecasts:
        cands = [(p + (GOAL_BONUS if _is_goal_market(m) else 0.0), p, m, sel)
                 for m, sels in (f.implied or {}).items() if m in markets for sel, p in sels.items()
                 if min_prob <= p <= max_prob and (m, sel) not in exclude]
        if not cands:
            continue
        cands.sort(reverse=True)
        tip = lambda p, m, sel: {"market": m, "selection": sel, "label": label(m, sel, f.home, f.away),  # noqa: E731
                                 "prob": p, "fair_odds": 1 / p}
        _, p, market, sel = cands[0]
        out.append({"match_id": f.match_id, "match": f"{f.home} – {f.away}", "kickoff": f.kickoff_utc.isoformat(),
                    "comp": f.comp, "comp_name": f.comp_name, **tip(p, market, sel),
                    "alternatives": [tip(p, m, s) for _, p, m, s in cands[:n_alternatives]]})
    return out


def _varied(legs: list[dict], n: int, max_same: int = MAX_SAME_TIP) -> list[dict]:
    """Die n sichersten Legs, aber höchstens `max_same`-mal derselbe Tipp; reicht das nicht, auffüllen."""
    chosen, count, rest = [], {}, []
    for t in legs:
        # Tipp schon zu oft drin: nächstbesten Tipp desselben Spiels nehmen (falls im Bereich)
        options = [t] + [{**t, **a} for a in t.get("alternatives", []) if a["label"] != t["label"]]
        pick = next((o for o in options if count.get((o["market"], o["selection"]), 0) < max_same), None)
        if pick is None:
            rest.append(t)
            continue
        chosen.append(pick)
        count[(pick["market"], pick["selection"])] = count.get((pick["market"], pick["selection"]), 0) + 1
        if len(chosen) == n:
            return chosen
    return chosen + rest[: n - len(chosen)]


# Reicht der Bereich 75–88 % an einem Tag nicht für 5 Spiele, schrittweise erweitern,
# damit trotzdem jeden Tag eine Kombi kommt (im Text markiert).
WIDER_BANDS = ((0.70, 0.90), (0.65, 0.92))


def day_combos(forecasts: list[MatchForecast], sizes=(3, 5), min_prob: float = 0.75, max_prob: float = 0.88,
               tz: str = "Europe/Zurich", widen: bool = True) -> list[dict]:
    """Tageskombis: alle Spiele am selben Kalendertag, ein Tipp pro Spiel, die sichersten zuerst."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    bands = [(min_prob, max_prob), *(WIDER_BANDS if widen else ())]
    per_band: list[dict[str, list[dict]]] = []
    for lo, hi in bands:
        by_day: dict[str, list[dict]] = {}
        for t in best_tip_per_match(forecasts, lo, hi):
            day = datetime.fromisoformat(t["kickoff"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).date().isoformat()
            by_day.setdefault(day, []).append(t)
        per_band.append(by_day)
    out = []
    for day in sorted({d for b in per_band for d in b}):
        band = next((i for i, b in enumerate(per_band) if len(b.get(day, [])) >= min(sizes)), None)
        if band is None:
            continue
        legs = sorted(per_band[band][day], key=lambda t: -t["prob"])
        for n in sizes:
            if len(legs) < n:
                continue
            chosen = sorted(_varied(legs, n), key=lambda t: t["kickoff"])
            prob = float(np.prod([t["prob"] for t in chosen]))
            out.append({"day": day, "size": n, "legs": chosen, "prob": prob, "fair_odds": 1 / prob,
                        "leg_min": min(t["prob"] for t in chosen), "widened": band > 0})
    return out


EXCHANGES = {"PS", "BFE", "MBK"}  # Pinnacle nur als Referenz, Börsen nicht als Buchmacher


def attach_book_odds(engine: Engine, combos: list[dict], books: list[str] | None = None) -> None:
    """Aktuelle Buchmacher-Quoten (letzter Abruf) an die Legs der Tageskombis hängen:
    beste Quote + Buchmacher + Pinnacle-Quote + Zeitpunkt. Für 1X2 und Über/Unter;
    doppelte Chance und beide treffen gibt es bei der Quellen-API nicht → nur faire Quote."""
    from fussball.data.schema import Odds

    with session_scope(engine) as s:
        for c in combos:
            for leg in c["legs"]:
                if leg["market"] == "1X2":
                    market, line = "1X2", 0.0
                elif leg["market"].startswith("OU"):
                    market, line = "OU", float(leg["market"][2:])
                else:
                    continue
                rows = s.execute(select(Odds.bookmaker, Odds.price, Odds.known_at).where(
                    Odds.match_id == leg["match_id"], Odds.market == market, Odds.line == line,
                    Odds.selection == leg["selection"], Odds.is_closing.is_(False), Odds.source == "odds-api")).all()
                ps = next((p for b, p, _ in rows if b == "PS"), None)
                offers = [(p, b, t) for b, p, t in rows if b not in EXCHANGES and (not books or b in books)]
                if ps:
                    leg["ps_odds"] = ps
                if offers:
                    price, book, at = max(offers)
                    leg.update(book_odds=price, book=BOOK_NAMES.get(book, book), odds_at=at.isoformat())
            if all(l.get("book_odds") for l in c["legs"]):
                c["book_odds"] = float(np.prod([l["book_odds"] for l in c["legs"]]))


# Risiko-Kombi: Tipps mit höherer Quote (60–72 %). Nur Märkte, die im Backtest in diesem Bereich
# gut kalibriert sind: 1/2, Über/Unter 2.5, beide treffen "Ja" ("Nein" traf 5 Punkte zu selten).
RISKY_MARKETS = ("1X2", "OU2.5", "BTTS")
RISKY_EXCLUDED = EXCLUDED_TIPS | {("BTTS", "N")}


# Krass-Kombi: 5 Spiele mit 55–70 % pro Tipp → Gesamtquote ca. 8–12, geht etwa an 1 von 10 Tagen auf.
# Märkte, die im Backtest in diesem Bereich kalibriert sind.
KRASS_MARKETS = ("1X2", "OU2.5", "BTTS", "HOME1.5", "AWAY1.5")


def risky_combos(forecasts: list[MatchForecast], safe_combos: list[dict], size: int = 3, min_prob: float = 0.60,
                 max_prob: float = 0.72, tz: str = "Europe/Zurich", skip: set[int] | None = None,
                 markets: tuple[str, ...] = RISKY_MARKETS) -> list[dict]:
    """Pro Tag eine Risiko-Kombi aus `size` Spielen, möglichst andere als in der sicheren Tageskombi."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    used: dict[str, set[int]] = {}
    for c in safe_combos:
        used.setdefault(c["day"], set()).update(l["match_id"] for l in c["legs"])
    by_day: dict[str, list[dict]] = {}
    for t in best_tip_per_match([f for f in forecasts if f.match_id not in (skip or set())], min_prob, max_prob,
                                markets, exclude=RISKY_EXCLUDED):
        day = datetime.fromisoformat(t["kickoff"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).date().isoformat()
        by_day.setdefault(day, []).append(t)
    out = []
    for day in sorted(by_day):
        # Spiele, die nicht in der sicheren Kombi stehen, zuerst; reicht das nicht, auch diese
        legs = sorted(by_day[day], key=lambda t: (t["match_id"] in used.get(day, set()),
                                                  -(t["prob"] + (GOAL_BONUS if _is_goal_market(t["market"]) else 0))))
        if len(legs) < size:
            continue
        chosen = sorted(_varied(legs, size), key=lambda t: t["kickoff"])
        prob = float(np.prod([t["prob"] for t in chosen]))
        out.append({"day": day, "size": size, "legs": chosen, "prob": prob, "fair_odds": 1 / prob,
                    "leg_min": min(t["prob"] for t in chosen), "risky": True})
    return out


def build_extra_combos(plan: "DailyPlan", kind: str, skip: set[int] | None = None) -> list[dict]:
    """Risiko- (kind='risky') oder Krass-Kombi (kind='krass') aus den Prognosen des Plans."""
    pool = plan.all_forecasts or plan.forecasts
    if kind == "risky":
        rc = plan.config.get("risky_combo", {})
        return risky_combos(pool, plan.day_combos, rc.get("size", 3), rc.get("min_prob", 0.60),
                            rc.get("max_prob", 0.72), skip=skip)
    kc = plan.config.get("krass_combo", {})
    return [{**c, "krass": True} for c in risky_combos(
        pool, plan.day_combos + plan.risky_combos, kc.get("size", 5), kc.get("min_prob", 0.55),
        kc.get("max_prob", 0.70), skip=skip, markets=KRASS_MARKETS)]


def apply_agents_risky(engine: Engine, plan: "DailyPlan", client=None, horizon_h: float = 36.0,
                       cached_only: bool = False, kind: str = "risky") -> list[dict]:
    """Scout prüft die Risiko- bzw. Krass-Kombi des nächsten Tages. Gestrichene Spiele werden ersetzt,
    Spiele mit „vorsicht“ ebenfalls, solange danach noch eine Kombi für den Tag zustande kommt (eine Runde)."""
    from fussball.agents import runner
    from fussball.app.state import local

    attr = "risky_combos" if kind == "risky" else "krass_combos"
    now = utcnow()
    soon = [c for c in getattr(plan, attr)
            if now < datetime.fromisoformat(c["legs"][0]["kickoff"]) <= now + timedelta(hours=horizon_h)]
    if not soon:
        return []
    day = soon[0]["day"]
    res = {r["match_id"]: r for r in runner.analyze_legs(engine, soon[0]["legs"], client=client, local_time=local,
                                                         cached_only=cached_only)}
    struck = {mid for mid, r in res.items() if r["assessment"] == "streichen"}
    risky = {mid for mid, r in res.items() if r["assessment"] == "vorsicht"}
    if struck or risky:
        combos = build_extra_combos(plan, kind, skip=struck | risky)
        if not any(c["day"] == day for c in combos):
            combos = build_extra_combos(plan, kind, skip=struck)
        setattr(plan, attr, combos)
    for c in getattr(plan, attr):
        for leg in c["legs"]:
            if leg["match_id"] in res:
                leg["agent"] = {k: res[leg["match_id"]][k] for k in ("assessment", "reason")}
    return list(res.values())


def apply_agents(engine: Engine, plan: "DailyPlan", client=None, horizon_h: float = 36.0,
                 max_rounds: int = 3, cached_only: bool = False) -> list[dict]:
    """Scout-Agent prüft die Legs der nächsten Tageskombi (innerhalb `horizon_h`).
    Gestrichene Spiele werden ausgeschlossen und die Kombi neu gebaut (nachrücken);
    Spiele mit „vorsicht“ ebenso, solange danach noch eine Kombi für den Tag zustande kommt."""
    from fussball.agents import runner
    from fussball.app.state import local

    now = utcnow()
    soon = [c for c in plan.day_combos
            if now < datetime.fromisoformat(c["legs"][0]["kickoff"]) <= now + timedelta(hours=horizon_h)]
    if not soon:
        return []
    day = soon[0]["day"]
    dc = plan.config.get("day_combo", {})
    excluded: set[int] = set()
    results: dict[int, dict] = {}
    pool = plan.all_forecasts or plan.forecasts

    def rebuild(skip: set[int]) -> list[dict]:
        return day_combos([f for f in pool if f.match_id not in skip], tuple(dc.get("sizes", [3, 5])),
                          dc.get("min_prob", 0.75), dc.get("max_prob", 0.88))

    for _ in range(max_rounds):
        legs = {l["match_id"]: l for c in sorted(plan.day_combos, key=lambda c: c["size"]) if c["day"] == day
                for l in c["legs"]}  # kleinste Kombi zuerst: bei knappem Budget die wichtigsten Spiele
        todo = [l for mid, l in legs.items() if mid not in results]
        if not todo:
            break
        for r in runner.analyze_legs(engine, todo, client=client, local_time=local, cached_only=cached_only):
            results[r["match_id"]] = r
        struck = {mid for mid, r in results.items() if r["assessment"] == "streichen"}
        risky = {mid for mid, r in results.items() if r["assessment"] == "vorsicht"}
        # ⚠️-Spiele nur ersetzen, wenn es danach trotzdem eine Kombi für den Tag gibt
        target = struck | risky
        combos = rebuild(target)
        if not any(c["day"] == day for c in combos):
            target, combos = struck, rebuild(struck)
        if target == excluded:
            break
        excluded = target
        plan.day_combos = combos
    for c in plan.day_combos:
        for leg in c["legs"]:
            r = results.get(leg["match_id"])
            if r is None:
                continue
            # Der Scout darf nach seiner Recherche einen anderen Tipp aus dem sicheren Bereich wählen
            alt = next((a for a in leg.get("alternatives", []) if a["label"] == r.get("best_tip")), None)
            if alt and alt["label"] != leg["label"]:
                leg.update(alt, switched_from=leg["label"])
            leg["agent"] = {k: r[k] for k in ("assessment", "reason")}
        c["prob"] = float(np.prod([l["prob"] for l in c["legs"]]))
        c["fair_odds"], c["leg_min"] = 1 / c["prob"], min(l["prob"] for l in c["legs"])
    return [{**r, "removed": r["match_id"] in excluded} for r in results.values()]


def record_served(engine: Engine, combos: list[dict]) -> None:
    """Gesendete Tageskombis merken (erste Version pro Tag und Grösse), um sie auszuwerten."""
    with session_scope(engine) as s:
        row = s.get(AppSetting, "served_combos")
        hist = dict(row.value) if row and isinstance(row.value, dict) else {}
        for c in combos:
            key = f"{c['day']}_{c['size']}"
            if key not in hist:
                hist[key] = {"day": c["day"], "size": c["size"], "fair_odds": c["fair_odds"], "prob": c["prob"],
                             "legs": [{k: l[k] for k in ("match_id", "match", "market", "selection", "label")}
                                      for l in c["legs"]], "done": False}
        if row:
            row.value = hist
        else:
            s.add(AppSetting(key="served_combos", value=hist))


def evaluate_served(engine: Engine) -> list[dict]:
    """Tageskombis auswerten, deren Spiele alle beendet sind (einmalig)."""
    from fussball.models.implied import outcome

    done = []
    with session_scope(engine) as s:
        row = s.get(AppSetting, "served_combos")
        if not row or not isinstance(row.value, dict):
            return []
        hist = dict(row.value)
        for key, c in hist.items():
            if c.get("done"):
                continue
            results = []
            for leg in c["legs"]:
                m = s.get(Match, leg["match_id"])
                if m is None or m.status != "finished" or m.ft_home is None:
                    break
                results.append({**leg, "score": f"{m.ft_home}:{m.ft_away}",
                                "won": outcome(leg["market"], leg["selection"], m.ft_home, m.ft_away)})
            else:
                c = {**c, "done": True, "results": results, "correct": sum(bool(r["won"]) for r in results)}
                hist[key] = c
                done.append(c)
        row.value = hist
    return done


def split_market(market: str) -> tuple[str, float]:
    """'OU1.5' → ('OU', 1.5), 'HOME0.5' → ('HOME', 0.5), 'DC' → ('DC', 0.0)."""
    for prefix in ("OU", "HOME", "AWAY"):
        if market.startswith(prefix) and market != prefix:
            return prefix, float(market[len(prefix):])
    return market, 0.0


def _store_predictions(engine: Engine, forecasts: list[MatchForecast]) -> None:
    with session_scope(engine) as s:
        ids = [f.match_id for f in forecasts]
        if ids:
            s.execute(delete(Prediction).where(Prediction.match_id.in_(ids), Prediction.run_type == "live"))
        now = utcnow()
        for f in forecasts:
            for market, line, probs in (("1X2", 0.0, f.probs_1x2), ("OU", 2.5, f.probs_ou)):
                for sel, p in probs.items():
                    s.add(Prediction(match_id=f.match_id, model_name=MODEL_NAME, model_version=MODEL_VERSION,
                                     run_type="live", market=market, line=line, selection=sel, probability=p,
                                     fair_odds=1 / p if p > 0 else None, expected_goals_home=f.lam,
                                     expected_goals_away=f.mu, features={"elo_diff": f.elo_diff},
                                     explanation={"factors": f.factors}, known_at=now))


BOOK_NAMES = {"UNIBET_SE": "Unibet (SE)", "UNIBET_NL": "Unibet (NL)", "UNIBET_FR": "Unibet (FR)",
              "LEOVEGAS_SE": "LeoVegas (SE)", "BOL": "BetOnline", "MYB": "MyBookie", "EVG": "Everygame",
              "B365": "Bet365", "BW": "bwin", "WH": "William Hill", "1XB": "1xBet", "BFD": "Betfred",
              "BV": "BetVictor", "PP": "Paddy Power", "SKB": "Sky Bet", "IW": "Interwetten", "VC": "VC Bet",
              "UNI": "Unibet", "MAR": "Marathonbet", "888": "888sport", "BETC": "Betclic", "BTSS": "Betsson",
              "TIP": "Tipico", "LEO": "LeoVegas", "NORD": "NordicBet", "COOL": "Coolbet", "BF": "Betfair Sportsbook"}


def tips_from_forecasts(forecasts: list[MatchForecast], price: str = "best") -> list[Tip]:
    tips = []
    for f in forecasts:
        for market, line, probs, labels in (("1X2", 0.0, f.probs_1x2, LABELS_1X2), ("OU", 2.5, f.probs_ou, LABELS_OU)):
            if market == "OU" and not f.ou_ok:
                continue  # keine eigene Referenz (z. B. Pinnacle ohne 2.5-Linie) → kein Tipp
            mk = f.market_1x2 if market == "1X2" else f.market_ou
            for sel, p in probs.items():
                odds = f.odds.get(price, {}).get(sel)
                if not odds:
                    continue
                book = f.odds.get("books", {}).get(sel, "") if price == "best" else ""
                book = BOOK_NAMES.get(book, book) or ("Ø Markt" if price == "avg" else "Bestquote")
                team = f" {f.home}" if sel == "H" else f" {f.away}" if sel == "A" else ""
                tips.append(Tip(f.match_id, f"{f.home} – {f.away}", f.kickoff_utc.isoformat(), f.comp, market,
                                line, sel, labels[sel] + team, p, odds, book,
                                fair_odds=1 / p, market_prob=(mk or {}).get(sel)))
    return tips


@dataclass
class DailyPlan:
    forecasts: list[MatchForecast]
    singles: list[tuple[Tip, float]]
    combos: list[Combo]
    config: dict
    blocked_leagues: list[str]
    safe: list[dict] = field(default_factory=list)
    day_combos: list[dict] = field(default_factory=list)
    all_forecasts: list = field(default_factory=list)
    risky_combos: list[dict] = field(default_factory=list)
    krass_combos: list[dict] = field(default_factory=list)


def daily_plan(engine: Engine, days: int = 2, forecasts: list[MatchForecast] | None = None) -> DailyPlan:
    cfg = get_config(engine)
    if forecasts is None:
        forecasts = [] if lite_mode() else predict_upcoming(engine, days=days)
    forced = cfg.get("force_enabled_leagues", [])
    ok = [f for f in forecasts if f.league_ok or f.comp in forced or "*" in forced]
    blocked = sorted({f.comp for f in forecasts} - {f.comp for f in ok})
    tips = tips_from_forecasts(ok, price=cfg["singles"].get("price", "best"))
    staked_today, staked_week = _staked(engine)
    singles = select_singles(tips, cfg["singles"], cfg["bankroll"], staked_today, staked_week)
    combos = build_combos(tips, cfg["combos"])
    world = {f.match_id: f for f in market_forecasts(engine, hours=24 * days)}
    world.update({f.match_id: f for f in forecasts if f.implied})
    all_fc = sorted(world.values(), key=lambda f: f.kickoff_utc)
    sc = cfg.get("safe", {})
    safe = safe_tips(all_fc, sc.get("min_prob", 0.70), sc.get("max_prob", 0.90), sc.get("per_match", 2))
    dc = cfg.get("day_combo", {})
    days_ = day_combos(all_fc, tuple(dc.get("sizes", [3, 5])), dc.get("min_prob", 0.75), dc.get("max_prob", 0.88))
    plan = DailyPlan(forecasts, singles, combos, cfg, blocked, safe, days_)
    plan.all_forecasts = all_fc
    return plan


def _staked(engine: Engine) -> tuple[float, float]:
    now = utcnow()
    with session_scope(engine) as s:
        rows = s.execute(select(Bet.placed_at, Bet.stake)).all()
    today = sum(st for t, st in rows if t.date() == now.date())
    week = sum(st for t, st in rows if t >= now - timedelta(days=7))
    return today, week


# ----------------------------------------------------------------------------- Wetten


def record_bet(engine: Engine, legs: list[dict], stake: float, odds: float, bookmaker: str = "Sporttip") -> str | int:
    """Speichert eine Einzelwette (1 Leg) oder Kombi (mehrere Legs).

    Kombis: jedes Leg ist eine Zeile mit gleicher combo_group; Einsatz, Gesamtquote
    und P/L stehen nur auf dem ersten Leg, damit Summen korrekt bleiben."""
    import uuid

    group = uuid.uuid4().hex[:8] if len(legs) > 1 else None
    with session_scope(engine) as s:
        first_id = None
        for i, leg in enumerate(legs):
            b = Bet(match_id=leg["match_id"], bet_type="combo" if group else "single", combo_group=group,
                    market=leg["market"], line=leg.get("line", 0.0), selection=leg["selection"],
                    odds_taken=float(odds) if i == 0 else float(leg.get("odds") or 0.0),
                    stake=float(stake) if i == 0 else 0.0, bookmaker=bookmaker)
            s.add(b)
            s.flush()
            first_id = first_id or b.id
    return group or first_id


def _leg_won(m: Match, market: str, line: float, selection: str) -> bool | None:
    if m is None or m.status != "finished" or m.ft_home is None:
        return None
    if market in ("DC", "DNB", "BTTS", "HOME", "AWAY") or (market == "OU" and line != 2.5):
        from fussball.models.implied import outcome

        code = f"{market}{line}" if market in ("OU", "HOME", "AWAY") else market
        return outcome(code, selection, m.ft_home, m.ft_away)
    if market == "1X2":
        result = "H" if m.ft_home > m.ft_away else "D" if m.ft_home == m.ft_away else "A"
        return result == selection
    if market == "OU":
        total = m.ft_home + m.ft_away
        return total > line if selection == "O" else total < line
    return None


def settle_bets(engine: Engine, details: list | None = None) -> int:
    """Rechnet offene Wetten auf beendete Spiele ab und berechnet den CLV.
    `details` (optional) wird mit einer Kurzbeschreibung je abgerechneter Wette gefüllt."""
    settled = 0

    def note(b: Bet, m: Match | None, text: str) -> None:
        if details is not None:
            details.append({"bet_id": b.id, "status": b.status, "pnl": b.pnl, "stake": b.stake,
                            "odds": b.odds_taken, "clv": b.clv, "text": text})
    with session_scope(engine) as s:
        open_bets = s.scalars(select(Bet).where(Bet.status == "open").order_by(Bet.id)).all()
        groups: dict[str, list[Bet]] = {}
        for b in open_bets:
            if b.combo_group:
                groups.setdefault(b.combo_group, []).append(b)
                continue
            won = _leg_won(s.get(Match, b.match_id), b.market, b.line, b.selection)
            if won is None:
                continue
            b.status = "won" if won else "lost"
            b.pnl = round(b.stake * (b.odds_taken - 1), 2) if won else -b.stake
            b.clv = closing_value(s, b)
            settled += 1
            m = s.get(Match, b.match_id)
            note(b, m, f"{_match_name(s, m)} {m.ft_home}:{m.ft_away} · {LABELS_ALL.get(b.selection, b.selection)}")
        for legs in groups.values():
            legs = sorted(legs, key=lambda b: b.id)
            results = [_leg_won(s.get(Match, b.match_id), b.market, b.line, b.selection) for b in legs]
            if any(r is False for r in results) or all(r is not None for r in results):
                won = all(results)
                for b, r in zip(legs, results):
                    b.status = "won" if r else "lost" if r is False else "void"
                    b.pnl = 0.0
                head = legs[0]
                head.pnl = round(head.stake * (head.odds_taken - 1), 2) if won else -head.stake
                head.status = "won" if won else "lost"
                settled += 1
                note(head, None, f"Kombi mit {len(legs)} Tipps")
    return settled


LABELS_ALL = {"H": "Heimsieg", "D": "Unentschieden", "A": "Auswärtssieg", "O": "Über 2.5", "U": "Unter 2.5"}


def _match_name(session, m: Match) -> str:
    home, away = session.get(Team, m.home_team_id), session.get(Team, m.away_team_id)
    return f"{home.name} – {away.name}"


def closing_value(session, bet: Bet) -> float | None:
    """CLV = genommene Quote × faire Schlusswahrscheinlichkeit − 1."""
    m = session.get(Match, bet.match_id)
    odds = [o for o in latest_odds_as_of(session, bet.match_id, m.kickoff_utc)
            if o.is_closing and o.market == bet.market and o.line == bet.line]
    sels = ("H", "D", "A") if bet.market == "1X2" else ("O", "U")
    for book in ("PS", "BFE", "Avg"):
        prices = {o.selection: o.price for o in odds if o.bookmaker == book}
        if all(k in prices for k in sels):
            fair = dict(zip(sels, fair_probs([prices[k] for k in sels])))
            bet.closing_odds = prices[bet.selection]
            return round(bet.odds_taken * fair[bet.selection] - 1, 4)
    return None


def bet_stats(engine: Engine) -> dict:
    with session_scope(engine) as s:
        rows = s.execute(
            select(Bet, Match.kickoff_utc, Team.name).join(Match, Match.id == Bet.match_id)
            .join(Team, Team.id == Match.home_team_id)
        ).all()
        data = [{"id": b.id, "type": b.bet_type, "market": b.market, "status": b.status, "stake": b.stake,
                 "odds": b.odds_taken, "pnl": b.pnl or 0.0, "clv": b.clv, "placed_at": b.placed_at,
                 "kickoff": k, "combo": b.combo_group} for b, k, _ in rows]
    df = pd.DataFrame(data)
    cfg = get_config(engine)
    if df.empty:
        return {"n": 0, "bankroll": cfg["bankroll"], "pnl": 0.0, "roi": 0.0, "hit": 0.0, "clv": None,
                "by_type": {}, "monthly": [], "open": 0}
    df = df[(df["type"] != "combo") | (df["stake"] > 0)]  # Kombi nur über das erste Leg zählen
    done = df[df["status"] != "open"]
    by_type = {}
    for t, g in done.groupby("type"):
        by_type[t] = {"n": len(g), "pnl": float(g["pnl"].sum()), "roi": float(g["pnl"].sum() / g["stake"].sum())}
    monthly = (done.assign(month=done["placed_at"].dt.strftime("%Y-%m")).groupby("month")["pnl"].sum()
               .cumsum().round(2).reset_index().values.tolist())
    return {
        "n": len(done), "open": int((df["status"] == "open").sum()),
        "bankroll": cfg["bankroll"] + float(done["pnl"].sum()),
        "pnl": float(done["pnl"].sum()),
        "roi": float(done["pnl"].sum() / done["stake"].sum()) if len(done) else 0.0,
        "hit": float((done["status"] == "won").mean()) if len(done) else 0.0,
        "clv": float(done["clv"].dropna().mean()) if done["clv"].notna().any() else None,
        "by_type": by_type, "monthly": monthly,
    }  # fmt: skip


def np_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (datetime, pd.Timestamp)):
        return o.isoformat()
    raise TypeError(type(o))
