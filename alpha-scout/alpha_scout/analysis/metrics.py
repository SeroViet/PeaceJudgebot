"""Reine Rechenfunktionen für Kennzahlen (deterministisch, gut testbar)."""

from __future__ import annotations

from collections.abc import Sequence

Series = Sequence[Sequence]  # [(datum, wert), ...] alt -> neu


def values(series: Series) -> list[float]:
    return [float(v) for _, v in series]


def last(series: Series) -> float | None:
    return float(series[-1][1]) if series else None


def cagr(first: float, last_: float, years: float) -> float | None:
    """Jährliche Wachstumsrate in Prozent."""
    if years <= 0 or first is None or last_ is None or first <= 0 or last_ <= 0:
        return None
    return ((last_ / first) ** (1 / years) - 1) * 100


def series_cagr(series: Series, max_years: int = 3) -> tuple[float | None, int]:
    """CAGR über die letzten max_years Jahresintervalle. Gibt (Prozent, Jahre) zurück."""
    vals = values(series)
    if len(vals) < 2:
        return None, 0
    window = vals[-(max_years + 1) :]
    years = len(window) - 1
    return cagr(window[0], window[-1], years), years


def ttm(quarterly: Series) -> float | None:
    """Summe der letzten 4 Quartale."""
    vals = values(quarterly)
    if len(vals) < 4:
        return None
    return sum(vals[-4:])


def net_debt(total_debt: float | None, cash: float | None) -> float | None:
    if total_debt is None and cash is None:
        return None
    return (total_debt or 0.0) - (cash or 0.0)


def net_debt_to_ebitda(nd: float | None, ebitda: float | None) -> float | None:
    """Nettoverschuldung / EBITDA. Netto-Cash -> negativer Wert. EBITDA <= 0 -> None."""
    if nd is None or ebitda is None or ebitda <= 0:
        return None
    return nd / ebitda


def margin(numerator: float | None, revenue: float | None) -> float | None:
    if numerator is None or not revenue:
        return None
    return numerator / revenue * 100


def dilution_per_year(shares: Series, max_years: int = 3) -> float | None:
    """Jährliche Veränderung der Aktienanzahl in Prozent (positiv = Verwässerung)."""
    vals = values(shares)
    if len(vals) < 2:
        return None
    window = vals[-(max_years + 1) :]
    years = len(window) - 1
    if window[0] <= 0 or window[-1] <= 0:
        return None
    return ((window[-1] / window[0]) ** (1 / years) - 1) * 100


def interp(x: float, points: list[tuple[float, float]]) -> float:
    """Lineare Interpolation über sortierte Stützstellen (x, y); ausserhalb wird abgeschnitten."""
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]  # pragma: no cover


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def drawdown_pct(price: float, high: float | None) -> float | None:
    if not high or high <= 0:
        return None
    return (price / high - 1) * 100


def moving_average(series: Series, n: int) -> float | None:
    vals = values(series)
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n
