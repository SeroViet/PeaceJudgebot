"""Value-Erkennung und Einsatzberechnung (Bruchteil-Kelly mit Limits)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from fussball.config import CONFIG_DIR


def load_betting_config(path: Path | None = None) -> dict:
    with open(path or CONFIG_DIR / "betting.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def edge(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def kelly(prob: float, odds: float) -> float:
    """Voller Kelly-Anteil der Bankroll (0, wenn kein Value)."""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (prob * b - (1 - prob)) / b)


@dataclass
class Tip:
    match_id: int
    match: str
    kickoff: str
    comp: str
    market: str  # 1X2 | OU
    line: float
    selection: str
    label: str  # z. B. "Heimsieg Bayern", "Über 2.5"
    prob: float
    odds: float
    bookmaker: str
    fair_odds: float = 0.0
    market_prob: float | None = None

    @property
    def edge(self) -> float:
        return edge(self.prob, self.odds)

    @property
    def min_odds(self) -> float:
        """Quote, unter der der Tipp mit min_edge=0 keinen Value mehr hat."""
        return 1.0 / self.prob


def stake(prob: float, odds: float, bankroll: float, fraction: float, max_pct: float) -> float:
    return round(min(kelly(prob, odds) * fraction, max_pct) * bankroll, 2)


def select_singles(tips: list[Tip], rules: dict, bankroll: float,
                   staked_today: float = 0.0, staked_week: float = 0.0) -> list[tuple[Tip, float]]:
    """Tipps mit Edge ≥ Schwelle, sortiert nach Edge × Konfidenz, unter Einhaltung
    der Tages- und Wochenlimits."""
    cand = [t for t in tips if t.edge >= rules["min_edge"] and t.prob >= rules["min_prob"]
            and t.odds <= rules["max_odds"]]
    cand.sort(key=lambda t: t.edge * t.prob, reverse=True)
    day_left = rules["daily_limit_pct"] * bankroll - staked_today
    week_left = rules["weekly_limit_pct"] * bankroll - staked_week
    out, used_matches = [], set()
    for t in cand:
        if t.match_id in used_matches:  # höchstens ein Tipp pro Spiel
            continue
        s = stake(t.prob, t.odds, bankroll, rules["kelly_fraction"], rules["max_stake_pct"])
        s = round(min(s, day_left, week_left), 2)
        if s < 1.0:
            continue
        out.append((t, s))
        used_matches.add(t.match_id)
        day_left -= s
        week_left -= s
    return out
