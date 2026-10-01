"""Markt-implizite Torverteilung: aus Pinnacle-Quoten (1X2 + Tore-Linie) die
erwarteten Tore beider Teams schätzen und daraus ALLE Märkte ableiten
(Über/Unter 0.5–4.5, beide treffen, doppelte Chance, Draw No Bet, Teamtore …).

So erhalten auch Märkte, für die es keine Pinnacle-Quote gibt, eine Wahrscheinlichkeit,
die auf dem schärfsten Markt beruht statt auf dem eigenen (schwächeren) Modell.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

from fussball.models.dixon_coles import score_matrix
from fussball.models.markets import all_markets, one_x_two

RHO = -0.05  # typische Dixon-Coles-Korrektur für Unentschieden mit wenig Toren


def p_over_line(m: np.ndarray, line: float) -> float:
    """P(Über) für eine Linie; bei ganzzahliger Linie ohne Push (Einsatz zurück)."""
    i, j = np.indices(m.shape)
    total = i + j
    over, push = m[total > line].sum(), m[total == line].sum()
    return float(over / (1 - push)) if push < 1 else 0.5


def fit_rates(p1x2: dict[str, float], total_line: float | None = None, p_over: float | None = None,
              rho: float = RHO) -> tuple[float, float]:
    """(λ_heim, λ_gast), die die Marktwahrscheinlichkeiten bestmöglich reproduzieren."""

    def loss(x):
        lam, mu = np.exp(x)
        m = score_matrix(lam, mu, rho)
        p = one_x_two(m)
        err = (p["H"] - p1x2["H"]) ** 2 + (p["A"] - p1x2["A"]) ** 2 + (p["D"] - p1x2["D"]) ** 2
        if total_line is not None and p_over is not None:
            err += 2.0 * (p_over_line(m, total_line) - p_over) ** 2
        return err

    # Startwert: Standardtore, Verteilung nach Favoritenrolle
    x0 = np.log([1.45 * (0.5 + p1x2["H"]), 1.45 * (0.5 + p1x2["A"])])
    res = minimize(loss, x0, method="Nelder-Mead", options={"xatol": 1e-5, "fatol": 1e-9})
    lam, mu = np.exp(res.x)
    return float(lam), float(mu)


def team_totals(m: np.ndarray) -> dict[str, dict[str, float]]:
    home = m.sum(axis=1)
    away = m.sum(axis=0)
    return {"HOME0.5": {"O": float(1 - home[0]), "U": float(home[0])},
            "HOME1.5": {"O": float(1 - home[:2].sum()), "U": float(home[:2].sum())},
            "AWAY0.5": {"O": float(1 - away[0]), "U": float(away[0])},
            "AWAY1.5": {"O": float(1 - away[:2].sum()), "U": float(away[:2].sum())}}


def implied_markets(lam: float, mu: float, rho: float = RHO) -> dict[str, dict[str, float]]:
    m = score_matrix(lam, mu, rho)
    out = all_markets(m)
    out.update(team_totals(m))
    return out


# Anzeige-Texte wie auf dem Wettschein (Sporttip-Stil)
def label(market: str, sel: str, home: str, away: str) -> str:
    if market == "1X2":
        return {"H": f"1 (Sieg {home})", "D": "X (Unentschieden)", "A": f"2 (Sieg {away})"}[sel]
    if market == "DC":
        return {"1X": f"1X ({home})", "X2": f"X2 ({away})", "12": "12 (kein Unentschieden)"}[sel]
    if market == "DNB":
        return f"Draw No Bet: {home if sel == 'H' else away}"
    if market == "BTTS":
        return "Beide Teams treffen: Ja" if sel == "Y" else "Beide Teams treffen: Nein"
    if market.startswith("OU"):
        line = market[2:]
        return f"{'Über' if sel == 'O' else 'Unter'} {line} Tore"
    if market.startswith(("HOME", "AWAY")):
        team = home if market.startswith("HOME") else away
        line = market[4:]
        if line == "0.5":
            return f"{team} trifft (Team über 0.5 Tore)" if sel == "O" else f"{team} trifft nicht"
        return f"{team} {'über' if sel == 'O' else 'unter'} {line} Tore (Team)"
    return f"{market} {sel}"


def outcome(market: str, sel: str, hg: int, ag: int) -> bool | None:
    """Hat der Tipp gewonnen? None = zurück (Draw No Bet bei Unentschieden)."""
    total = hg + ag
    if market == "1X2":
        return {"H": hg > ag, "D": hg == ag, "A": hg < ag}[sel]
    if market == "DC":
        return {"1X": hg >= ag, "X2": hg <= ag, "12": hg != ag}[sel]
    if market == "DNB":
        return None if hg == ag else (hg > ag) == (sel == "H")
    if market == "BTTS":
        return (hg > 0 and ag > 0) == (sel == "Y")
    if market.startswith("OU"):
        return (total > float(market[2:])) == (sel == "O")
    if market.startswith("HOME"):
        return (hg > float(market[4:])) == (sel == "O")
    if market.startswith("AWAY"):
        return (ag > float(market[4:])) == (sel == "O")
    return None
