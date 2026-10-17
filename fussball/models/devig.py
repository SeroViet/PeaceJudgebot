"""Marge aus Buchmacherquoten entfernen → faire Marktwahrscheinlichkeiten."""

from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def overround(odds: list[float]) -> float:
    return float(sum(1.0 / o for o in odds) - 1.0)


def multiplicative(odds: list[float]) -> list[float]:
    inv = np.array([1.0 / o for o in odds])
    return list(inv / inv.sum())


def power(odds: list[float]) -> list[float]:
    """p_i = (1/o_i)^k mit Σp = 1. Korrigiert den Favoriten-Aussenseiter-Bias
    besser als die multiplikative Methode."""
    inv = np.array([1.0 / o for o in odds])
    if inv.sum() <= 1.0 + 1e-9:  # keine Marge (z. B. Börse): nur normieren
        return list(inv / inv.sum())
    try:
        k = brentq(lambda k: np.sum(inv**k) - 1.0, 1.0, 5.0)
    except ValueError:
        return multiplicative(odds)
    return list(inv**k)


def shin(odds: list[float]) -> list[float]:
    """Shin (1993): modelliert Insider-Anteil z."""
    inv = np.array([1.0 / o for o in odds])
    total = inv.sum()
    if total <= 1.0:
        return list(inv / total)

    def probs(z):
        return (np.sqrt(z**2 + 4 * (1 - z) * inv**2 / total) - z) / (2 * (1 - z))

    z = brentq(lambda z: probs(z).sum() - 1.0, 0.0, 0.4)
    p = probs(z)
    return list(p / p.sum())


METHODS = {"power": power, "shin": shin, "multiplicative": multiplicative}


def fair_probs(odds: list[float], method: str = "power") -> list[float]:
    return METHODS[method](odds)
