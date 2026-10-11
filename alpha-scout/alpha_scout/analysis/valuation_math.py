"""Bewertungsmodell: FCF-basiertes Szenario-Modell auf Firmenebene (ADR-sicher)."""

from __future__ import annotations

from dataclasses import dataclass

from alpha_scout.config import ValuationConfig


@dataclass
class Scenario:
    name: str
    growth_pct: float
    exit_multiple: float
    equity_value_now: float  # fairer Wert heute (diskontiert)
    equity_value_3y: float  # erwarteter Wert in 3 Jahren (nicht diskontiert)
    equity_value_10y: float

    def per_share(self, price: float, market_cap: float) -> tuple[float, float, float]:
        """Umrechnung Firmenwert -> Kurs über das Verhältnis zur Marktkapitalisierung."""
        f = price / market_cap
        return self.equity_value_now * f, self.equity_value_3y * f, self.equity_value_10y * f


def project_fcf(fcf0: float, growth: float, years: int, fade: float) -> list[float]:
    """FCF-Pfad: Jahre 1–5 mit growth, Jahre 6–10 mit growth * fade."""
    out = []
    f = fcf0
    for t in range(1, years + 1):
        g = growth if t <= 5 else growth * fade
        f *= 1 + g
        out.append(f)
    return out


def scenario_value(fcf0: float, growth: float, multiple: float, cfg: ValuationConfig, name: str) -> Scenario:
    path = project_fcf(fcf0, growth, 10, cfg.terminal_fade)
    r = cfg.discount_rate
    pv_cf = sum(cf / (1 + r) ** t for t, cf in enumerate(path, start=1))
    terminal = path[-1] * multiple
    pv = pv_cf + terminal / (1 + r) ** 10
    return Scenario(
        name=name,
        growth_pct=growth * 100,
        exit_multiple=multiple,
        equity_value_now=pv,
        equity_value_3y=path[2] * multiple,
        equity_value_10y=terminal,
    )


def scenarios(fcf0: float, base_growth_pct: float, cfg: ValuationConfig) -> dict[str, Scenario]:
    base_g = max(0.0, min(base_growth_pct / 100, cfg.growth_cap))
    growths = {
        "bear": base_g * 0.5,
        "base": base_g,
        "bull": min(base_g * 1.3, cfg.growth_cap + 0.05),
    }
    return {name: scenario_value(fcf0, g, cfg.exit_multiple[name], cfg, name) for name, g in growths.items()}


def implied_growth(fcf0: float, market_cap: float, cfg: ValuationConfig) -> float | None:
    """Reverse-DCF: welches FCF-Wachstum (Jahre 1–5) preist der aktuelle Kurs ein? (Prozent)"""
    if fcf0 <= 0 or market_cap <= 0:
        return None
    multiple = cfg.exit_multiple["base"]

    def value(g: float) -> float:
        return scenario_value(fcf0, g, multiple, cfg, "x").equity_value_now

    lo, hi = -0.3, 1.0
    if value(lo) > market_cap:
        return lo * 100
    if value(hi) < market_cap:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if value(mid) < market_cap:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def expected_cagr(price_now: float, price_future: float, years: int) -> float | None:
    if price_now <= 0 or price_future <= 0:
        return None
    return ((price_future / price_now) ** (1 / years) - 1) * 100
