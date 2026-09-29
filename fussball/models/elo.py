"""ELO-Ratings für Klubs und Nationalteams (eloratings.net-Methodik vereinfacht)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize


@dataclass
class Elo:
    k: float = 20.0
    home_adv: float = 65.0
    start: float = 1500.0
    promoted_start: float = 1420.0
    ratings: dict[str, float] = field(default_factory=dict)

    def rating(self, team: str, promoted: bool = False) -> float:
        return self.ratings.get(team, self.promoted_start if promoted else self.start)

    def diff(self, home: str, away: str, neutral: bool = False) -> float:
        return self.rating(home) - self.rating(away) + (0.0 if neutral else self.home_adv)

    def update(self, home: str, away: str, hg: int, ag: int, neutral: bool = False, k_mult: float = 1.0) -> None:
        rh, ra = self.rating(home), self.rating(away)
        expected = 1.0 / (1.0 + 10 ** (-self.diff(home, away, neutral) / 400))
        score = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        gd = abs(hg - ag)
        mult = 1.0 if gd <= 1 else 1.5 if gd == 2 else (11 + gd) / 8
        delta = self.k * k_mult * mult * (score - expected)
        self.ratings[home], self.ratings[away] = rh + delta, ra - delta


@dataclass
class OrderedLogit:
    """Übersetzt ELO-Differenz in 1X2-Wahrscheinlichkeiten."""

    slope: float = 0.004
    c_away: float = -0.6
    c_home: float = 0.6

    def probs(self, d: float) -> dict[str, float]:
        p_away = 1 / (1 + math.exp(-(self.c_away - self.slope * d)))
        p_not_home = 1 / (1 + math.exp(-(self.c_home - self.slope * d)))
        return {"H": 1 - p_not_home, "D": p_not_home - p_away, "A": p_away}

    def fit(self, diffs: np.ndarray, outcomes: np.ndarray) -> "OrderedLogit":
        y = np.asarray(outcomes)  # 0 = Heim, 1 = Remis, 2 = Gast

        def nll(theta):
            s, c1, gap = theta
            c2 = c1 + np.exp(gap)
            pa = 1 / (1 + np.exp(-(c1 - s * diffs)))
            pnh = 1 / (1 + np.exp(-(c2 - s * diffs)))
            p = np.where(y == 2, pa, np.where(y == 1, pnh - pa, 1 - pnh))
            return -np.sum(np.log(np.clip(p, 1e-12, None)))

        res = minimize(nll, [self.slope, self.c_away, math.log(self.c_home - self.c_away)], method="Nelder-Mead")
        s, c1, gap = res.x
        self.slope, self.c_away, self.c_home = float(s), float(c1), float(c1 + math.exp(gap))
        return self
