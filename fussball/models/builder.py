"""BetBuilder-Rechner: gemeinsame Wahrscheinlichkeit mehrerer Tipps im SELBEN Spiel.

Tipps im selben Spiel hängen zusammen ("Bayern gewinnt" und "Über 2.5 Tore" treten oft gemeinsam
auf). Wer sie einfach multipliziert, rechnet falsch. Wir rechnen exakt über die Verteilung aller
Endstände – mit den erwarteten Toren aus den Pinnacle-Quoten (λ Heim, μ Gast):

- nur Endstand-Tipps: Dixon-Coles-Matrix P(Heimtore=i, Gasttore=j)
- mit Halbzeit-Tipps: Tore je Halbzeit als unabhängige Poisson-Prozesse (1. Halbzeit ≈ 45 % der
  Tore), Raster über (Heim 1. HZ, Gast 1. HZ, Heim 2. HZ, Gast 2. HZ)

Ein Tipp ("Part") ist (Markt, Auswahl, Linie, Halbzeit):
  1X2 H/D/A · DC 1X/X2/12 · OU O/U + Linie · BTTS Y/N · HOME/AWAY O/U + Linie (Teamtore)
  HCP H/D/A + Linie (Sporttip-Handicap "0:1" = Linie -1 für Heim) · CS "2:1"
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
from scipy.stats import poisson

from fussball.models.dixon_coles import score_matrix
from fussball.models.implied import RHO

FIRST_HALF_SHARE = 0.45
MAX_GOALS = 10
HALF_GOALS = 7


@dataclass(frozen=True)
class Part:
    market: str          # 1X2, DC, OU, BTTS, HOME, AWAY, HCP, CS
    selection: str
    line: float = 0.0
    half: str = "ft"     # ft = Endstand, 1h = 1. Halbzeit

    def key(self) -> str:
        return f"{self.half}:{self.market}:{self.line:g}:{self.selection}"


def _mask(p: Part, h: np.ndarray, a: np.ndarray) -> np.ndarray:
    """Boolesche Maske über Tore Heim h / Gast a (gleich geformte Arrays)."""
    m, s, line = p.market, p.selection, p.line
    if m == "1X2":
        return {"H": h > a, "D": h == a, "A": h < a}[s]
    if m == "DC":
        return {"1X": h >= a, "X2": h <= a, "12": h != a}[s]
    if m == "OU":
        return (h + a > line) if s == "O" else (h + a < line)
    if m == "BTTS":
        both = (h > 0) & (a > 0)
        return both if s == "Y" else ~both
    if m in ("HOME", "AWAY"):
        g = h if m == "HOME" else a
        return (g > line) if s == "O" else (g < line)
    if m == "HCP":  # Heim bekommt `line` Tore dazu (Sporttip 0:1 → line = -1)
        d = h + line - a
        return {"H": d > 0, "D": d == 0, "A": d < 0}[s]
    if m == "CS":
        hh, aa = (int(x) for x in s.split(":"))
        return (h == hh) & (a == aa)
    raise ValueError(f"Unbekannter Markt {m}")


def joint_prob(lam: float, mu: float, parts: list[Part], rho: float = RHO) -> float:
    """P(alle Tipps gewinnen gemeinsam)."""
    if not parts:
        return 1.0
    if all(p.half == "ft" for p in parts):
        mat = score_matrix(lam, mu, rho, MAX_GOALS)
        h, a = np.indices(mat.shape)
        ok = np.ones_like(mat, dtype=bool)
        for p in parts:
            ok &= _mask(p, h, a)
        return float(mat[ok].sum())
    # Halbzeit-Raster: unabhängige Poisson-Tore je Team und Halbzeit
    g = np.arange(HALF_GOALS + 1)
    f = FIRST_HALF_SHARE
    ph1, pa1 = poisson.pmf(g, lam * f), poisson.pmf(g, mu * f)
    ph2, pa2 = poisson.pmf(g, lam * (1 - f)), poisson.pmf(g, mu * (1 - f))
    w = np.einsum("i,j,k,l->ijkl", ph1, pa1, ph2, pa2)
    w /= w.sum()
    h1, a1, h2, a2 = np.indices(w.shape)
    ok = np.ones_like(w, dtype=bool)
    for p in parts:
        ok &= _mask(p, h1, a1) if p.half == "1h" else _mask(p, h1 + h2, a1 + a2)
    return float(w[ok].sum())


def part_label(p: Part, home: str, away: str) -> str:
    from fussball.models.implied import label

    if p.market == "HCP":
        hl = f"{max(0, -p.line):g}:{max(0, p.line):g}" if p.line <= 0 else f"{p.line:g}:0"
        text = f"Handicap {hl} " + {"H": home, "D": "Unentschieden", "A": away}[p.selection]
    elif p.market == "CS":
        text = f"Resultat {p.selection}"
    else:
        code = f"{p.market}{p.line:g}" if p.market in ("OU", "HOME", "AWAY") else p.market
        if p.market in ("OU", "HOME", "AWAY") and "." not in code:
            code += ".0"
        text = label(code, p.selection, home, away)
    return f"1. HZ: {text}" if p.half == "1h" else text


# Bausteine für Vorschläge: nur Endstand-Tipps, die man bei Sporttip im BetBuilder findet
CANDIDATES = [Part("1X2", "H"), Part("1X2", "A"), Part("DC", "1X"), Part("DC", "X2"),
              Part("OU", "O", 1.5), Part("OU", "O", 2.5), Part("OU", "U", 2.5), Part("OU", "U", 3.5),
              Part("BTTS", "Y"), Part("BTTS", "N"),
              Part("HOME", "O", 0.5), Part("HOME", "O", 1.5), Part("AWAY", "O", 0.5), Part("AWAY", "O", 1.5)]
FAMILY = {"1X2": "result", "DC": "result", "OU": "total", "BTTS": "btts", "HOME": "home", "AWAY": "away"}


@dataclass
class Builder:
    parts: list[Part]
    prob: float           # gemeinsame Wahrscheinlichkeit
    naive: float          # Produkt der Einzelwahrscheinlichkeiten (so rechnen viele falsch)

    @property
    def fair_odds(self) -> float:
        return 1 / self.prob

    @property
    def lift(self) -> float:
        """> 1: Tipps verstärken sich (treten öfter gemeinsam auf als einzeln multipliziert)."""
        return self.prob / self.naive


def best_builders(lam: float, mu: float, min_prob: float = 0.40, max_prob: float = 0.60,
                  min_leg: float = 0.50, sizes=(2, 3), top: int = 3) -> list[Builder]:
    """Schlaue BetBuilder für ein Spiel: 2–3 Tipps, die sich gegenseitig verstärken, gemeinsam
    `min_prob`–`max_prob` (Quote ca. 1.7–2.5). Keine doppelten oder sich selbst enthaltenden Tipps."""
    single = {p: joint_prob(lam, mu, [p]) for p in CANDIDATES}
    pool = [p for p, q in single.items() if q >= min_leg]
    out = []
    for n in sizes:
        for combo in combinations(pool, n):
            if len({FAMILY[p.market] for p in combo}) < n:
                continue  # z. B. zweimal Über/Unter
            joint = joint_prob(lam, mu, list(combo))
            if not min_prob <= joint <= max_prob:
                continue
            # Jeder Tipp muss etwas beitragen (sonst ist er im anderen schon enthalten)
            if any(joint_prob(lam, mu, [q for q in combo if q != p]) - joint < 0.03 for p in combo):
                continue
            out.append(Builder(list(combo), joint, float(np.prod([single[p] for p in combo]))))
    out.sort(key=lambda b: (-(b.lift * b.prob), -b.prob))
    return out[:top]
