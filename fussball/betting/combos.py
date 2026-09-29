"""Kombi-Builder: nur Tipps mit eigenem Value, unabhängige Spiele, 4–6 Tipps.

Varianten:
- "sicher": höchste Gesamtwahrscheinlichkeit
- "ausgewogen": höchster Erwartungswert × Wahrscheinlichkeit
- "hoher EV": höchster Erwartungswert
Wenn keine Kombination die Regeln erfüllt, wird keine Kombi ausgegeben.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

from fussball.betting.value import Tip, kelly


@dataclass
class Combo:
    variant: str
    legs: list[Tip]
    boost: float = 0.0

    @property
    def prob(self) -> float:
        return math.prod(t.prob for t in self.legs)

    @property
    def odds(self) -> float:
        raw = math.prod(t.odds for t in self.legs)
        return 1 + (raw - 1) * (1 + self.boost)

    @property
    def ev(self) -> float:
        """Erwartungswert pro eingesetzter Einheit (0.10 = +10 %)."""
        return self.prob * self.odds - 1

    @property
    def bookmaker_margin(self) -> float:
        """Kumulierte Marge: wie viel die Kombi-Quote unter der fairen Marktquote liegt."""
        fair = math.prod(1 / t.market_prob for t in self.legs if t.market_prob)
        return 1 - self.odds / fair if fair else float("nan")

    def losing_streak_prob(self, n: int) -> float:
        return (1 - self.prob) ** n

    def stake(self, bankroll: float, fraction: float, max_pct: float) -> float:
        return round(min(kelly(self.prob, self.odds) * fraction, max_pct) * bankroll, 2)


def eligible_legs(tips: list[Tip], rules: dict) -> list[Tip]:
    """Pro Spiel höchstens der beste Tipp (keine korrelierten Märkte aus einem Spiel)."""
    best: dict[int, Tip] = {}
    for t in tips:
        if t.prob < rules["leg_min_prob"] or t.edge < rules["leg_min_edge"] or t.odds < rules["leg_min_odds"]:
            continue
        if t.match_id not in best or t.edge > best[t.match_id].edge:
            best[t.match_id] = t
    return sorted(best.values(), key=lambda t: t.edge, reverse=True)


def build_combos(tips: list[Tip], rules: dict, max_candidates: int = 14) -> list[Combo]:
    legs = eligible_legs(tips, rules)[:max_candidates]
    boost = {int(k): float(v) for k, v in (rules.get("boost") or {}).items()}
    options: list[Combo] = []
    for size in range(rules["min_legs"], rules["max_legs"] + 1):
        for combo in combinations(legs, size):
            c = Combo("", list(combo), boost.get(size, 0.0))
            if c.prob >= rules["min_total_prob"] and c.ev > 0:
                options.append(c)
    if not options:
        return []
    picks = {
        "sicher": max(options, key=lambda c: (c.prob, c.ev)),
        "ausgewogen": max(options, key=lambda c: c.ev * c.prob),
        "hoher EV": max(options, key=lambda c: c.ev),
    }
    out, seen = [], set()
    for variant, c in picks.items():
        key = tuple(sorted((t.match_id, t.selection) for t in c.legs))
        if key in seen:
            continue
        seen.add(key)
        out.append(Combo(variant, c.legs, c.boost))
    return out


def system_bet(legs: list[Tip], k: int) -> dict[str, float]:
    """System k aus n: jede k-er-Kombi mit 1 Einheit. EV und Trefferchance."""
    n = len(legs)
    subsets = list(combinations(legs, k))
    probs = [t.prob for t in legs]
    ev = 0.0
    # Exakte Verteilung über alle 2^n Ausgänge (n ≤ 8)
    p_any = 0.0
    for mask in range(1 << n):
        p = math.prod(probs[i] if mask >> i & 1 else 1 - probs[i] for i in range(n))
        payout = sum(math.prod(t.odds for t in s) for s in subsets
                     if all(mask >> legs.index(t) & 1 for t in s))
        ev += p * payout
        if payout > 0:
            p_any += p
    stake = len(subsets)
    return {"bets": stake, "ev": ev / stake - 1, "p_return": p_any}
