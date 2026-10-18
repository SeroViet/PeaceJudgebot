"""Alle Wettmärkte aus einer Ergebnis-Matrix P(Heimtore=i, Gasttore=j)."""

from __future__ import annotations

import numpy as np

OU_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)


def one_x_two(m: np.ndarray) -> dict[str, float]:
    return {"H": float(np.tril(m, -1).sum()), "D": float(np.trace(m)), "A": float(np.triu(m, 1).sum())}


def over_under(m: np.ndarray, line: float) -> dict[str, float]:
    i, j = np.indices(m.shape)
    over = float(m[(i + j) > line].sum())
    return {"O": over, "U": 1.0 - over}


def btts(m: np.ndarray) -> dict[str, float]:
    yes = float(m[1:, 1:].sum())
    return {"Y": yes, "N": 1.0 - yes}


def double_chance(p: dict[str, float]) -> dict[str, float]:
    return {"1X": p["H"] + p["D"], "12": p["H"] + p["A"], "X2": p["D"] + p["A"]}


def draw_no_bet(p: dict[str, float]) -> dict[str, float]:
    s = p["H"] + p["A"]
    return {"H": p["H"] / s, "A": p["A"] / s}


def correct_scores(m: np.ndarray, top: int = 10) -> list[tuple[str, float]]:
    flat = [(f"{i}:{j}", float(m[i, j])) for i in range(m.shape[0]) for j in range(m.shape[1])]
    return sorted(flat, key=lambda x: -x[1])[:top]


def all_markets(m: np.ndarray) -> dict[str, dict[str, float]]:
    p = one_x_two(m)
    out = {"1X2": p, "DC": double_chance(p), "DNB": draw_no_bet(p), "BTTS": btts(m)}
    for line in OU_LINES:
        out[f"OU{line}"] = over_under(m, line)
    return out


def rps(probs: dict[str, float], outcome: str) -> float:
    """Ranked Probability Score für 1X2 (geordnet H < D < A)."""
    order = ["H", "D", "A"]
    cum_p = np.cumsum([probs[k] for k in order])[:-1]
    cum_o = np.cumsum([1.0 if k == outcome else 0.0 for k in order])[:-1]
    return float(np.sum((cum_p - cum_o) ** 2) / 2)
