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


def update_data(engine: Engine) -> dict:
    """Laufende Saison (inkl. 2. Ligen für Aufsteiger) und kommende Spiele laden."""
    from fussball.models.backtest import RELATED_LEAGUES

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
    odds_api = {}
    from fussball.data.odds_api import OddsApiClient, import_odds

    client = OddsApiClient()
    interval_h = float(os.getenv("ODDS_API_INTERVAL_HOURS", "24"))
    with session_scope(engine) as s:
        last = s.get(AppSetting, "odds_api_last")
        last_at = datetime.fromisoformat(last.value) if last and isinstance(last.value, str) else None
    due = last_at is None or utcnow() - last_at >= timedelta(hours=interval_h)
    if client.configured and not due:
        odds_api = {"skipped": f"letzter Abruf {last_at:%d.%m. %H:%M} UTC, Intervall {interval_h:g} h (Credits sparen)"}
    elif client.configured:
        try:
            with session_scope(engine) as s:
                odds_api = import_odds(s, client, main)
                row = s.get(AppSetting, "odds_api_last")
                if row:
                    row.value = utcnow().isoformat()
                else:
                    s.add(AppSetting(key="odds_api_last", value=utcnow().isoformat()))
            odds_api["credits_left"] = client.remaining
        except Exception as exc:  # noqa: BLE001
            log.exception("Odds API fehlgeschlagen")
            odds_api = {"error": repr(exc)}
    else:
        odds_api = {"error": "ODDS_API_KEY fehlt – ohne Pinnacle-Quoten keine Tipps"}
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
    _store_predictions(engine, forecasts)
    return forecasts


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


def daily_plan(engine: Engine, days: int = 2, forecasts: list[MatchForecast] | None = None) -> DailyPlan:
    cfg = get_config(engine)
    forecasts = forecasts if forecasts is not None else predict_upcoming(engine, days=days)
    forced = cfg.get("force_enabled_leagues", [])
    ok = [f for f in forecasts if f.league_ok or f.comp in forced or "*" in forced]
    blocked = sorted({f.comp for f in forecasts} - {f.comp for f in ok})
    tips = tips_from_forecasts(ok, price=cfg["singles"].get("price", "best"))
    staked_today, staked_week = _staked(engine)
    singles = select_singles(tips, cfg["singles"], cfg["bankroll"], staked_today, staked_week)
    combos = build_combos(tips, cfg["combos"])
    return DailyPlan(forecasts, singles, combos, cfg, blocked)


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
