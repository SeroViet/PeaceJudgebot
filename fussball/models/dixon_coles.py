"""Dixon-Coles-Modell (1997) mit Zeitgewichtung und Shrinkage.

log λ_heim = μ + heimvorteil + angriff[heim] − abwehr[gast]
log λ_gast = μ + angriff[gast] − abwehr[heim]

- Zeitgewichtung w = exp(−ξ · Tage): neuere Spiele zählen mehr.
- L2-Shrinkage zu einem Prior: bei wenig Daten wird ein Team zum Prior
  gezogen (Durchschnitt, bzw. schwächer für Aufsteiger).
- ρ korrigiert die Häufigkeit von 0:0, 1:0, 0:1, 1:1 und wird im zweiten
  Schritt bei festen Torraten geschätzt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import poisson

MAX_GOALS = 10


@dataclass
class DixonColesParams:
    teams: list[str]
    mu: float
    home_adv: float
    attack: np.ndarray
    defence: np.ndarray
    rho: float
    n_matches: dict[str, float] = field(default_factory=dict)  # effektive (gewichtete) Spiele je Team

    def index(self) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.teams)}

    def rates(self, home: str, away: str, neutral: bool = False,
              prior_attack: float = 0.0, prior_defence: float = 0.0) -> tuple[float, float]:
        """Erwartete Tore (λ_heim, λ_gast). Unbekannte Teams erhalten den Prior."""
        idx = self.index()
        ah, dh = (self.attack[idx[home]], self.defence[idx[home]]) if home in idx else (prior_attack, prior_defence)
        aa, da = (self.attack[idx[away]], self.defence[idx[away]]) if away in idx else (prior_attack, prior_defence)
        ha = 0.0 if neutral else self.home_adv
        return float(np.exp(self.mu + ha + ah - da)), float(np.exp(self.mu + aa - dh))


def tau_matrix(lam: float, mu: float, rho: float, size: int = MAX_GOALS + 1) -> np.ndarray:
    t = np.ones((size, size))
    t[0, 0] = 1 - lam * mu * rho
    t[0, 1] = 1 + lam * rho
    t[1, 0] = 1 + mu * rho
    t[1, 1] = 1 - rho
    return t


def score_matrix(lam: float, mu: float, rho: float = 0.0, max_goals: int = MAX_GOALS) -> np.ndarray:
    """P(Heimtore = i, Gasttore = j), normiert auf 1."""
    g = np.arange(max_goals + 1)
    m = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu)) * tau_matrix(lam, mu, rho, max_goals + 1)
    m = np.clip(m, 0, None)
    return m / m.sum()


def _tau_vec(hg, ag, lam, mu, rho):
    t = np.ones_like(lam)
    t = np.where((hg == 0) & (ag == 0), 1 - lam * mu * rho, t)
    t = np.where((hg == 0) & (ag == 1), 1 + lam * rho, t)
    t = np.where((hg == 1) & (ag == 0), 1 + mu * rho, t)
    t = np.where((hg == 1) & (ag == 1), 1 - rho, t)
    return t


def fit(
    home: np.ndarray,
    away: np.ndarray,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    age_days: np.ndarray,
    xi: float = 0.0019,
    ridge: float = 1.0,
    prior: dict[str, tuple[float, float]] | None = None,
    neutral: np.ndarray | None = None,
    init: DixonColesParams | None = None,
    league: np.ndarray | None = None,
    true_goals: tuple[np.ndarray, np.ndarray] | None = None,
) -> DixonColesParams:
    """Schätzt die Parameter per gewichteter Maximum-Likelihood.

    ξ = 0.0019/Tag entspricht einer Halbwertszeit von ca. 1 Jahr.
    `prior` = {team: (angriff, abwehr)}; nicht aufgeführte Teams haben Prior 0.
    `league` = Liga-Index je Spiel (0 = Hauptliga). Andere Ligen erhalten einen
    eigenen Tor-Offset; Auf-/Absteiger verbinden die Stärken über Ligen hinweg.
    Tore dürfen nicht ganzzahlig sein (z. B. Mischung aus Toren und Schüssen).
    """
    teams = sorted(set(home) | set(away))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    hi = np.array([idx[t] for t in home])
    ai = np.array([idx[t] for t in away])
    hg = np.asarray(home_goals, dtype=float)
    ag = np.asarray(away_goals, dtype=float)
    w = np.exp(-xi * np.asarray(age_days, dtype=float))
    ha_mask = np.ones_like(hg) if neutral is None else (~np.asarray(neutral, dtype=bool)).astype(float)
    prior = prior or {}
    p_att = np.array([prior.get(t, (0.0, 0.0))[0] for t in teams])
    p_def = np.array([prior.get(t, (0.0, 0.0))[1] for t in teams])
    lg = np.zeros(len(hg), dtype=int) if league is None else np.asarray(league, dtype=int)
    n_lg = int(lg.max()) + 1 if len(lg) else 1

    def unpack(theta):
        return theta[0], theta[1], theta[2 : 2 + n], theta[2 + n : 2 + 2 * n], theta[2 + 2 * n :]

    def objective(theta):
        mu, ha, att, dfn, off = unpack(theta)
        base = mu + np.concatenate([[0.0], off])[lg]
        log_l = base + ha * ha_mask + att[hi] - dfn[ai]
        log_m = base + att[ai] - dfn[hi]
        lam, lmu = np.exp(log_l), np.exp(log_m)
        ll = np.sum(w * (hg * log_l - lam + ag * log_m - lmu))
        pen = 0.5 * ridge * (np.sum((att - p_att) ** 2) + np.sum((dfn - p_def) ** 2))
        rh, ra = w * (hg - lam), w * (ag - lmu)
        g_att = np.bincount(hi, rh, n) + np.bincount(ai, ra, n) - ridge * (att - p_att)
        g_def = -np.bincount(ai, rh, n) - np.bincount(hi, ra, n) - ridge * (dfn - p_def)
        g_off = np.bincount(lg, rh + ra, n_lg)[1:]
        grad = np.concatenate([[rh.sum() + ra.sum(), np.sum(rh * ha_mask)], g_att, g_def, g_off])
        return -(ll - pen), -grad

    theta0 = np.zeros(2 + 2 * n + n_lg - 1)
    theta0[0], theta0[1] = np.log(max(hg.mean() * 0.5 + ag.mean() * 0.5, 0.1)), 0.2
    theta0[2 : 2 + n], theta0[2 + n : 2 + 2 * n] = p_att, p_def
    if init is not None:
        old = init.index()
        theta0[0], theta0[1] = init.mu, init.home_adv
        for t, i in idx.items():
            if t in old:
                theta0[2 + i], theta0[2 + n + i] = init.attack[old[t]], init.defence[old[t]]
    res = minimize(objective, theta0, jac=True, method="L-BFGS-B")
    mu, ha, att, dfn, off = unpack(res.x)

    base = mu + np.concatenate([[0.0], off])[lg]
    lam = np.exp(base + ha * ha_mask + att[hi] - dfn[ai])
    lmu = np.exp(base + att[ai] - dfn[hi])

    # ρ bezieht sich auf echte Tore, auch wenn die Raten aus Mischwerten geschätzt wurden.
    rho_hg, rho_ag = (np.asarray(true_goals[0], float), np.asarray(true_goals[1], float)) if true_goals else (hg, ag)

    def neg_ll_rho(rho):
        t = _tau_vec(rho_hg, rho_ag, lam, lmu, rho)
        return -np.sum(w * np.log(np.clip(t, 1e-10, None)))

    rho = minimize_scalar(neg_ll_rho, bounds=(-0.2, 0.2), method="bounded").x
    eff = np.bincount(hi, w, n) + np.bincount(ai, w, n)
    return DixonColesParams(teams, float(mu), float(ha), att.copy(), dfn.copy(), float(rho),
                            {t: float(eff[i]) for t, i in idx.items()})
