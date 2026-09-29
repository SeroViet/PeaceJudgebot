"""Walk-Forward-Backtest: Prognose zum Zeitpunkt der Vorab-Quoten, nur mit
Wissen, das zu diesem Zeitpunkt bekannt war.

Ablauf je Liga:
1. Spiele nach "Slot" gruppieren (= Zeitpunkt, zu dem die Vorab-Quoten
   bekannt wurden; Wochenende/Unter der Woche).
2. Pro Slot: Dixon-Coles auf allen bis dahin bekannten Ergebnissen fitten,
   ELO fortschreiben, für jedes Spiel des Slots Wahrscheinlichkeiten
   berechnen und Markt- (Vorab/Schluss) und Wettquoten dazuschreiben.
3. `evaluate()` kombiniert Modell und Markt mit Gewichten, die nur auf
   früheren Saisons gewählt werden, und misst Log Loss, Brier, RPS, CLV, ROI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from fussball.models import dixon_coles as dc
from fussball.models.devig import fair_probs
from fussball.models.elo import Elo, OrderedLogit
from fussball.models.markets import one_x_two, over_under, rps
from fussball.models.pooling import pool

log = logging.getLogger(__name__)

SHARP_BOOKS = ("PS", "BFE", "Avg")  # Priorität für "Marktwahrscheinlichkeit"
# Aggregate, Börsen und die Referenz selbst sind keine Buchmacher, bei denen man "shoppt".
NOT_BETTABLE = {"PS", "BFE", "MBK", "Max", "Avg", "BbMx", "BbAv"}
SEL_1X2 = ("H", "D", "A")
SEL_OU = ("O", "U")


@dataclass
class ModelConfig:
    xi: float = 0.0019
    ridge: float = 1.0
    window_days: int = 1100
    promoted_prior: tuple[float, float] = (-0.25, -0.2)
    min_history: int = 300  # Mindestanzahl Spiele in der Liga vor der ersten Prognose
    # 1.0 = nur Tore. Darunter wird mit Schüssen aufs Tor gemischt (stabileres Signal).
    goal_weight: float = 1.0
    use_lower_tier: bool = True


# Hauptliga -> Ligen, die mitgefittet werden (Auf-/Absteiger verbinden die Stärken)
RELATED_LEAGUES = {
    "E0": ["E1"], "D1": ["D2"], "I1": ["I2"], "SP1": ["SP2"], "F1": ["F2"],
    "E1": ["E0"], "D2": ["D1"], "I2": ["I1"], "SP2": ["SP1"], "F2": ["F1"],
}  # fmt: skip


def load_frame(engine: Engine) -> pd.DataFrame:
    """Ein Spiel pro Zeile inkl. Quoten der relevanten Buchmacher."""
    matches = pd.read_sql(
        text("""
            SELECT m.id AS match_id, c.code AS comp, m.season, m.kickoff_utc, m.known_at,
                   th.name AS home, ta.name AS away, m.ft_home, m.ft_away, m.status, m.neutral_venue,
                   m.shots_on_target_home AS sot_home, m.shots_on_target_away AS sot_away
            FROM matches m
            JOIN competitions c ON c.id = m.competition_id
            JOIN teams th ON th.id = m.home_team_id
            JOIN teams ta ON ta.id = m.away_team_id
        """),
        engine,
    )
    for col in ("kickoff_utc", "known_at"):  # robust gegen gemischte Formate (mit/ohne Mikrosekunden)
        matches[col] = pd.to_datetime(matches[col], format="ISO8601")
    odds = pd.read_sql(
        text("""
            SELECT match_id, bookmaker, market, selection, is_closing, price, known_at
            FROM odds
            WHERE market = '1X2' OR (market = 'OU' AND line = 2.5)
        """),
        engine,
    )
    odds["known_at"] = pd.to_datetime(odds["known_at"], format="ISO8601")
    odds = odds.sort_values("known_at")
    odds["is_closing"] = odds["is_closing"].astype(bool)
    main = odds[odds["bookmaker"].isin(["PS", "BFE", "Avg", "Max", "B365"])].copy()
    main["col"] = main["bookmaker"] + "_" + np.where(main["is_closing"], "C", "P") + "_" + main["selection"]
    wide = main.pivot_table(index="match_id", columns="col", values="price", aggfunc="last")  # jüngste Quote
    # Beste Vorab-Quote eines echten Buchmachers je Auswahl (für Tipps mit Anbietername)
    soft = odds[~odds["is_closing"] & ~odds["bookmaker"].isin(NOT_BETTABLE)]
    soft = soft.drop_duplicates(["match_id", "bookmaker", "selection"], keep="last")
    top = soft.sort_values("price").drop_duplicates(["match_id", "selection"], keep="last")
    best = top.pivot(index="match_id", columns="selection", values="price").add_prefix("best_")
    book = top.pivot(index="match_id", columns="selection", values="bookmaker").add_prefix("bestbook_")
    slot = odds[~odds["is_closing"]].groupby("match_id")["known_at"].min().rename("slot")
    return (matches.join(wide, on="match_id").join(best, on="match_id").join(book, on="match_id")
            .join(slot, on="match_id").sort_values("kickoff_utc"))


def _market(row, closing: bool, sels) -> tuple[dict[str, float] | None, str | None]:
    tag = "C" if closing else "P"
    for book in SHARP_BOOKS:
        prices = [row.get(f"{book}_{tag}_{s}") for s in sels]
        if all(p is not None and not pd.isna(p) and p > 1 for p in prices):
            return dict(zip(sels, fair_probs(prices))), book
    return None, None


def walk_forward(frame: pd.DataFrame, comp: str, cfg: ModelConfig | None = None,
                 start: pd.Timestamp | None = None) -> pd.DataFrame:
    """`start`: nur Slots ab diesem Zeitpunkt prognostizieren (ELO läuft trotzdem
    über die gesamte Historie). Für Live-Prognosen: start = jetzt."""
    cfg = cfg or ModelConfig()
    df = frame[frame["comp"] == comp].copy()
    df = df[df["slot"].notna() | (df["status"] == "scheduled")]
    df["slot"] = df["slot"].fillna(df["kickoff_utc"] - pd.Timedelta(hours=24))
    done = df[df["status"] == "finished"].sort_values("known_at")
    related = RELATED_LEAGUES.get(comp, []) if cfg.use_lower_tier else []
    pool_df = frame[frame["comp"].isin([comp, *related]) & (frame["status"] == "finished")].copy()
    pool_df["league_idx"] = (pool_df["comp"] != comp).astype(int)
    pool_df = pool_df.sort_values("known_at")
    seasons = sorted(df["season"].unique())
    teams_by_season = {s: set(df.loc[df["season"] == s, "home"]) | set(df.loc[df["season"] == s, "away"])
                       for s in seasons}

    elo, elo_ptr = Elo(), 0
    ologit = OrderedLogit()
    elo_hist: list[tuple[float, int, str]] = []
    ologit_fits = 0
    params: dc.DixonColesParams | None = None
    rows = []

    for slot, group in df.groupby("slot", sort=True):
        # ELO mit allen Ergebnissen fortschreiben, die vor dem Slot bekannt waren
        while elo_ptr < len(done) and done.iloc[elo_ptr]["known_at"] <= slot:
            r = done.iloc[elo_ptr]
            outcome = 0 if r.ft_home > r.ft_away else 1 if r.ft_home == r.ft_away else 2
            elo_hist.append((elo.diff(r.home, r.away), outcome, r.season))
            elo.update(r.home, r.away, int(r.ft_home), int(r.ft_away))
            elo_ptr += 1

        if start is not None and slot < start:
            continue
        hist = pool_df[(pool_df["known_at"] <= slot)
                       & (pool_df["kickoff_utc"] >= slot - pd.Timedelta(days=cfg.window_days))]
        if (hist["league_idx"] == 0).sum() < cfg.min_history:
            continue
        season = group["season"].iloc[0]
        prev = seasons[seasons.index(season) - 1] if seasons.index(season) > 0 else None
        promoted = {t for t in teams_by_season[season] if prev and t not in teams_by_season[prev]}
        known_teams = set(hist["home"]) | set(hist["away"])
        # Prior nur für Teams ohne jede Historie (bei Zweitliga-Daten selten)
        prior = {t: cfg.promoted_prior for t in promoted if t not in known_teams}
        for t in promoted:
            elo.ratings.setdefault(t, elo.promoted_start)

        age = (slot - hist["kickoff_utc"]).dt.total_seconds().to_numpy() / 86400
        hg, ag = hist["ft_home"].to_numpy(float), hist["ft_away"].to_numpy(float)
        th, ta = hg, ag
        if cfg.goal_weight < 1.0:
            sh, sa = hist["sot_home"].to_numpy(float), hist["sot_away"].to_numpy(float)
            ok = ~(np.isnan(sh) | np.isnan(sa))
            k = (hg[ok].sum() + ag[ok].sum()) / max(sh[ok].sum() + sa[ok].sum(), 1)
            gw = cfg.goal_weight
            th = np.where(ok, gw * hg + (1 - gw) * k * np.nan_to_num(sh), hg)
            ta = np.where(ok, gw * ag + (1 - gw) * k * np.nan_to_num(sa), ag)
        params = dc.fit(hist["home"].to_numpy(), hist["away"].to_numpy(), th, ta, age, xi=cfg.xi,
                        ridge=cfg.ridge, prior=prior, init=params, league=hist["league_idx"].to_numpy(),
                        true_goals=(hg, ag))
        if len(elo_hist) > 200 and (ologit_fits := ologit_fits + 1) % 5 == 1:
            recent = elo_hist[-1500:]
            ologit.fit(np.array([d for d, _, _ in recent]), np.array([o for _, o, _ in recent]))

        for r in group.itertuples(index=False):
            lam, mu = params.rates(r.home, r.away, prior_attack=cfg.promoted_prior[0],
                                   prior_defence=cfg.promoted_prior[1])
            m = dc.score_matrix(lam, mu, params.rho)
            rd = r._asdict()
            mk_pre, book_pre = _market(rd, False, SEL_1X2)
            mk_close, _ = _market(rd, True, SEL_1X2)
            ou_pre, _ = _market(rd, False, SEL_OU)
            ou_close, _ = _market(rd, True, SEL_OU)
            out = {
                "match_id": r.match_id, "comp": comp, "season": r.season, "kickoff_utc": r.kickoff_utc,
                "slot": slot, "home": r.home, "away": r.away, "status": r.status,
                "ft_home": r.ft_home, "ft_away": r.ft_away, "lam": lam, "mu": mu,
                "elo_diff": elo.diff(r.home, r.away), "market_book": book_pre,
                "eff_n_home": params.n_matches.get(r.home, 0.0), "eff_n_away": params.n_matches.get(r.away, 0.0),
            }  # fmt: skip
            for k, v in one_x_two(m).items():
                out[f"dc_{k}"] = v
            for k, v in ologit.probs(out["elo_diff"]).items():
                out[f"elo_{k}"] = v
            for k, v in over_under(m, 2.5).items():
                out[f"dc_{k}"] = v
            for src, name in ((mk_pre, "mkt"), (mk_close, "close"), (ou_pre, "mkt"), (ou_close, "close")):
                for k, v in (src or {}).items():
                    out[f"{name}_{k}"] = v
            for sel in (*SEL_1X2, *SEL_OU):
                out[f"avg_{sel}"] = rd.get(f"Avg_P_{sel}")
                out[f"max_{sel}"] = rd.get(f"Max_P_{sel}")
                out[f"best_{sel}"] = rd.get(f"best_{sel}")
                out[f"bestbook_{sel}"] = rd.get(f"bestbook_{sel}")
            rows.append(out)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- Auswertung


def outcome_1x2(df: pd.DataFrame) -> pd.Series:
    return np.where(df["ft_home"] > df["ft_away"], "H", np.where(df["ft_home"] == df["ft_away"], "D", "A"))


def blend_1x2(df: pd.DataFrame, w_dc: float, w_elo: float) -> pd.DataFrame:
    w_mkt = 1.0 - w_dc - w_elo
    out = []
    for r in df.itertuples(index=False):
        srcs, ws = [{k: getattr(r, f"dc_{k}") for k in SEL_1X2}], [w_dc]
        srcs.append({k: getattr(r, f"elo_{k}") for k in SEL_1X2}); ws.append(w_elo)  # noqa: E702
        if not pd.isna(getattr(r, "mkt_H", np.nan)):
            srcs.append({k: getattr(r, f"mkt_{k}") for k in SEL_1X2}); ws.append(w_mkt)  # noqa: E702
        else:
            s = w_dc + w_elo
            ws = [w / s for w in ws]
        out.append(pool(srcs, ws))
    return pd.DataFrame(out, index=df.index).add_prefix("p_")


def blend_ou(df: pd.DataFrame, w_dc: float) -> pd.DataFrame:
    out = []
    for r in df.itertuples(index=False):
        srcs, ws = [{k: getattr(r, f"dc_{k}") for k in SEL_OU}], [w_dc]
        if not pd.isna(getattr(r, "mkt_O", np.nan)):
            srcs.append({k: getattr(r, f"mkt_{k}") for k in SEL_OU}); ws.append(1 - w_dc)  # noqa: E702
        else:
            ws = [1.0]
        out.append(pool(srcs, ws))
    return pd.DataFrame(out, index=df.index).add_prefix("p_")


def scores_1x2(df: pd.DataFrame, probs: pd.DataFrame) -> dict[str, float]:
    y = outcome_1x2(df)
    p_true = np.choose(pd.Series(y).map({"H": 0, "D": 1, "A": 2}).to_numpy(),
                       [probs["p_H"].to_numpy(), probs["p_D"].to_numpy(), probs["p_A"].to_numpy()])
    onehot = np.stack([(y == k).astype(float) for k in SEL_1X2], axis=1)
    brier = np.mean(np.sum((probs[["p_H", "p_D", "p_A"]].to_numpy() - onehot) ** 2, axis=1))
    rps_v = np.mean([rps({"H": a, "D": b, "A": c}, o)
                     for a, b, c, o in zip(probs["p_H"], probs["p_D"], probs["p_A"], y)])
    return {"logloss": float(-np.mean(np.log(np.clip(p_true, 1e-12, None)))), "brier": float(brier), "rps": float(rps_v)}


def logloss_ou(df: pd.DataFrame, probs: pd.DataFrame) -> float:
    over = (df["ft_home"] + df["ft_away"]) > 2.5
    p = np.where(over, probs["p_O"], probs["p_U"])
    return float(-np.mean(np.log(np.clip(p, 1e-12, None))))


@dataclass
class BetRules:
    min_edge: float = 0.05
    min_prob: float = 0.0
    max_odds: float = 6.0
    price_col: str = "avg"  # Quote, zu der gewettet wird (avg = realistischer Durchschnitt)


def simulate_bets(df: pd.DataFrame, probs: pd.DataFrame, sels, rules: BetRules) -> pd.DataFrame:
    """Flat-Stake-Wetten (1 Einheit) auf jeden Tipp mit Edge ≥ min_edge."""
    y1 = outcome_1x2(df)
    over = ((df["ft_home"] + df["ft_away"]) > 2.5).to_numpy()
    bets = []
    for i, (idx, r) in enumerate(df.iterrows()):
        for s in sels:
            price = r.get(f"{rules.price_col}_{s}")
            p = probs.loc[idx, f"p_{s}"]
            if price is None or pd.isna(price) or price > rules.max_odds or p < rules.min_prob:
                continue
            edge = p * price - 1
            if edge < rules.min_edge:
                continue
            won = (y1[i] == s) if s in SEL_1X2 else (over[i] if s == "O" else not over[i])
            close_p = r.get(f"close_{s}")
            clv = price * close_p - 1 if close_p is not None and not pd.isna(close_p) else np.nan
            bets.append({"match_id": r["match_id"], "season": r["season"], "comp": r["comp"], "sel": s,
                         "price": price, "p": p, "edge": edge, "won": won,
                         "pnl": price - 1 if won else -1.0, "clv": clv})
    return pd.DataFrame(bets)


def summarize_bets(bets: pd.DataFrame) -> dict[str, float]:
    if bets.empty:
        return {"bets": 0, "roi": 0.0, "hit": 0.0, "clv": float("nan"), "t": 0.0}
    n = len(bets)
    roi = bets["pnl"].mean()
    sd = bets["pnl"].std(ddof=1) if n > 1 else 0.0
    t = roi / (sd / np.sqrt(n)) if sd > 0 else 0.0
    return {"bets": n, "roi": float(roi), "hit": float(bets["won"].mean()),
            "clv": float(bets["clv"].mean()), "t": float(t), "avg_odds": float(bets["price"].mean())}


W_GRID_1X2 = [(a, b) for a in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0) for b in (0.0, 0.1, 0.2) if a + b <= 1.0]
W_GRID_OU = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]


def choose_weights(train: pd.DataFrame) -> tuple[tuple[float, float], float]:
    """Gewichte, die auf Trainingsdaten den Log Loss minimieren."""
    t1 = train[train["mkt_H"].notna()]
    best_1x2 = min(W_GRID_1X2, key=lambda w: scores_1x2(t1, blend_1x2(t1, *w))["logloss"])
    t2 = train[train["mkt_O"].notna()]
    best_ou = min(W_GRID_OU, key=lambda w: logloss_ou(t2, blend_ou(t2, w)))
    return best_1x2, best_ou


def evaluate(preds: pd.DataFrame, rules: BetRules) -> dict:
    """Pro Testsaison: Gewichte auf allen früheren Saisons wählen, dann messen."""
    fin = preds[preds["status"] == "finished"].copy()
    seasons = sorted(fin["season"].unique())
    report, all_bets = [], []
    for i, season in enumerate(seasons[1:], start=1):
        train, test = fin[fin["season"].isin(seasons[:i])], fin[fin["season"] == season]
        (w_dc, w_elo), w_ou = choose_weights(train)
        t1 = test[test["mkt_H"].notna()]
        p1 = blend_1x2(t1, w_dc, w_elo)
        pm = t1[["mkt_H", "mkt_D", "mkt_A"]].rename(columns=lambda c: c.replace("mkt_", "p_"))
        pdc = t1[["dc_H", "dc_D", "dc_A"]].rename(columns=lambda c: c.replace("dc_", "p_"))
        t2 = test[test["mkt_O"].notna()]
        p2 = blend_ou(t2, w_ou)
        b1 = simulate_bets(t1, p1, SEL_1X2, rules)
        b2 = simulate_bets(t2, p2, SEL_OU, rules)
        all_bets += [b1, b2]
        report.append({
            "season": season, "matches": len(t1), "w_dc": w_dc, "w_elo": w_elo, "w_ou": w_ou,
            "model": scores_1x2(t1, pdc), "market": scores_1x2(t1, pm), "blend": scores_1x2(t1, p1),
            "ou_market": logloss_ou(t2, t2[["mkt_O", "mkt_U"]].rename(columns=lambda c: c.replace("mkt_", "p_"))),
            "ou_blend": logloss_ou(t2, p2), "bets_1x2": summarize_bets(b1), "bets_ou": summarize_bets(b2),
        })  # fmt: skip
    non_empty = [b for b in all_bets if not b.empty]
    bets = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
    return {"seasons": report, "bets": bets, "total": summarize_bets(bets)}


# Beste Konfiguration laut Variantenvergleich (siehe README, Abschnitt Modell).
BEST_CONFIG: dict = {"goal_weight": 0.6, "xi": 0.003, "use_lower_tier": True}
MIN_BETS_FOR_RELEASE = 30


def utcnow_iso() -> str:
    from fussball.data.schema import utcnow

    return utcnow().isoformat()


def league_summary(preds: pd.DataFrame, rules: BetRules) -> dict:
    """Aggregiert alle Testsaisons einer Liga und bestimmt die Live-Gewichte.

    Freigabe nur, wenn (a) Modell+Markt im Log Loss besser als der Markt allein ist
    und (b) die simulierten Wetten positiven CLV und mind. 30 Wetten hatten."""
    ev = evaluate(preds, rules)
    rows = ev["seasons"]
    n = np.array([r["matches"] for r in rows], dtype=float)

    def wavg(key, sub=None):
        vals = np.array([r[key][sub] if sub else r[key] for r in rows], dtype=float)
        return float(np.average(vals, weights=n)) if n.sum() else float("nan")

    fin = preds[preds["status"] == "finished"]
    (w_dc, w_elo), w_ou = choose_weights(fin)
    tot = ev["total"]
    ll_market, ll_blend = wavg("market", "logloss"), wavg("blend", "logloss")
    clv = tot.get("clv", float("nan"))
    beats = bool(ll_blend < ll_market - 1e-4 and tot["bets"] >= MIN_BETS_FOR_RELEASE and clv == clv and clv > 0)
    return {
        "test_seasons": [r["season"] for r in rows], "matches": int(n.sum()),
        "ll_model": wavg("model", "logloss"), "ll_market": ll_market, "ll_blend": ll_blend,
        "rps_model": wavg("model", "rps"), "rps_market": wavg("market", "rps"), "rps_blend": wavg("blend", "rps"),
        "ou_market": wavg("ou_market"), "ou_blend": wavg("ou_blend"),
        "bets": int(tot["bets"]), "roi": float(tot["roi"]), "clv": float(clv) if clv == clv else 0.0,
        "t": float(tot["t"]) if np.isfinite(tot["t"]) else 0.0, "hit": float(tot["hit"]),
        "w_dc": w_dc, "w_elo": w_elo, "w_ou": w_ou, "beats_market": beats,
        "per_season": [{"season": r["season"], "w_dc": r["w_dc"], "w_ou": r["w_ou"],
                        "ll_market": r["market"]["logloss"], "ll_blend": r["blend"]["logloss"],
                        "bets": r["bets_1x2"]["bets"] + r["bets_ou"]["bets"]} for r in rows],
    }  # fmt: skip


def market_value_summary(frame: pd.DataFrame, min_edge: float = 0.03, max_odds: float = 4.0) -> dict:
    """Strategie "Markt-Value": faire Wahrscheinlichkeit aus Pinnacle-Vorabquoten,
    Wette zur besten Quote eines echten Buchmachers (keine Börsen/Aggregate).
    Unabhängig vom Modell. Freigabe bei ≥ 300 Wetten, positivem CLV und CLV-t-Wert ≥ 2
    (CLV schwankt viel weniger als der Gewinn und ist daher der verlässlichere Test)."""
    fin = frame[frame["status"] == "finished"]
    bets = []
    for r in fin.to_dict("records"):
        for sels in (SEL_1X2, SEL_OU):
            pre = [r.get(f"PS_P_{k}") for k in sels]
            close = [r.get(f"PS_C_{k}") for k in sels]
            if any(x is None or pd.isna(x) for x in pre):
                continue
            fair = dict(zip(sels, fair_probs(pre)))
            fair_close = dict(zip(sels, fair_probs(close))) if not any(x is None or pd.isna(x) for x in close) else {}
            total = r["ft_home"] + r["ft_away"]
            result = {"H": r["ft_home"] > r["ft_away"], "D": r["ft_home"] == r["ft_away"],
                      "A": r["ft_home"] < r["ft_away"], "O": total > 2.5, "U": total < 2.5}
            for k in sels:
                price = r.get(f"best_{k}")
                if price is None or pd.isna(price) or price > max_odds or fair[k] * price - 1 < min_edge:
                    continue
                bets.append({"season": r["season"], "comp": r["comp"], "book": r.get(f"bestbook_{k}"), "sel": k,
                             "price": price, "edge": fair[k] * price - 1, "won": result[k],
                             "pnl": price - 1 if result[k] else -1.0,
                             "clv": price * fair_close[k] - 1 if k in fair_close else np.nan})
    b = pd.DataFrame(bets)
    s = summarize_bets(b)
    clv = b["clv"].dropna() if len(b) else pd.Series(dtype=float)
    clv_t = float(clv.mean() / (clv.std(ddof=1) / np.sqrt(len(clv)))) if len(clv) > 1 and clv.std() > 0 else 0.0
    per_season = (b.groupby("season").agg(bets=("pnl", "size"), roi=("pnl", "mean"), clv=("clv", "mean"))
                  .round(4).reset_index().to_dict("records")) if len(b) else []
    return {"min_edge": min_edge, "max_odds": max_odds, "reference": "Pinnacle", **s, "clv_t": clv_t,
            "per_season": per_season, "enabled": bool(s["bets"] >= 300 and s["clv"] > 0 and clv_t >= 2)}
