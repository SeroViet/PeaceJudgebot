"""Kurse, Stammdaten und Fundamentaldaten über yfinance (mit Cache)."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from alpha_scout.db import Database

log = logging.getLogger(__name__)

QUOTE_TTL = 15 * 60
FUNDAMENTALS_TTL = 24 * 3600
HISTORY_TTL = 6 * 3600

SOURCE = "Yahoo Finance (yfinance)"


def _clean(v: Any) -> Any:
    if v is None:
        return None
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    except TypeError:
        return None
    if hasattr(v, "item"):  # numpy-Typen
        v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    return v


def _series(df: Any, names: list[str]) -> list[tuple[str, float]]:
    """Zeile aus einem yfinance-DataFrame als [(Datum, Wert)] alt -> neu."""
    if df is None or getattr(df, "empty", True):
        return []
    for name in names:
        if name in df.index:
            row = df.loc[name]
            out = []
            for col, val in row.items():
                val = _clean(val)
                if val is None:
                    continue
                d = col.date().isoformat() if hasattr(col, "date") else str(col)
                out.append((d, float(val)))
            return sorted(out)
    return []


QUOTE_FIELDS = {
    "price": ["currentPrice", "regularMarketPrice", "navPrice", "previousClose"],
    "currency": ["currency"],
    "financial_currency": ["financialCurrency"],
    "market_cap": ["marketCap"],
    "exchange": ["exchange"],
    "exchange_name": ["fullExchangeName"],
    "quote_type": ["quoteType"],
    "name": ["longName", "shortName"],
    "sector": ["sector"],
    "industry": ["industry"],
    "country": ["country"],
    "summary": ["longBusinessSummary"],
    "shares_outstanding": ["sharesOutstanding"],
    "forward_eps": ["forwardEps"],
    "trailing_eps": ["trailingEps"],
    "high_52w": ["fiftyTwoWeekHigh"],
    "low_52w": ["fiftyTwoWeekLow"],
    "ter": ["netExpenseRatio", "annualReportExpenseRatio"],
    "total_assets": ["totalAssets"],
    "gross_margin": ["grossMargins"],
    "operating_margin": ["operatingMargins"],
    "roe": ["returnOnEquity"],
    "volume_avg": ["averageVolume"],
}


class MarketData:
    """Async-Fassade über yfinance. Alle Aufrufe laufen in Threads und werden gecacht."""

    def __init__(self, db: Database | None, yf_module: Any = None):
        self.db = db
        self._yf = yf_module
        self._sem = asyncio.Semaphore(4)

    @property
    def yf(self) -> Any:
        if self._yf is None:
            import yfinance

            self._yf = yfinance
        return self._yf

    async def _cached(self, key: str, ttl: float, fn: Any) -> Any:
        if self.db is not None:
            hit = await self.db.cache_get(key)
            if hit is not None:
                return hit
        async with self._sem:
            try:
                value = await asyncio.to_thread(fn)
            except Exception as e:  # yfinance wirft sehr unterschiedliche Fehler
                log.warning("yfinance-Fehler bei %s: %s", key, e)
                return None
        if value is not None and self.db is not None:
            await self.db.cache_set(key, value, ttl)
        return value

    async def quote(self, ticker: str) -> dict[str, Any] | None:
        def fetch() -> dict[str, Any] | None:
            info = self.yf.Ticker(ticker).info or {}
            if not info or (info.get("quoteType") is None and info.get("regularMarketPrice") is None):
                return None
            out: dict[str, Any] = {"ticker": ticker, "as_of": date.today().isoformat()}
            for key, names in QUOTE_FIELDS.items():
                out[key] = next((_clean(info.get(n)) for n in names if _clean(info.get(n)) is not None), None)
            return out

        return await self._cached(f"yf:quote:{ticker}", QUOTE_TTL, fetch)

    async def financials(self, ticker: str) -> dict[str, Any] | None:
        def fetch() -> dict[str, Any] | None:
            t = self.yf.Ticker(ticker)
            inc, bs, cf = t.income_stmt, t.balance_sheet, t.cashflow
            qinc = t.quarterly_income_stmt
            data = {
                "revenue": _series(inc, ["Total Revenue", "Operating Revenue"]),
                "ebitda": _series(inc, ["EBITDA", "Normalized EBITDA"]),
                "operating_income": _series(inc, ["Operating Income", "Total Operating Income As Reported"]),
                "net_income": _series(inc, ["Net Income", "Net Income Common Stockholders"]),
                "diluted_shares": _series(inc, ["Diluted Average Shares"]),
                "total_debt": _series(bs, ["Total Debt"]),
                "cash": _series(
                    bs, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"]
                ),
                "shares": _series(bs, ["Ordinary Shares Number", "Share Issued"]),
                "equity": _series(bs, ["Stockholders Equity", "Common Stock Equity"]),
                "fcf": _series(cf, ["Free Cash Flow"]),
                "ocf": _series(cf, ["Operating Cash Flow"]),
                "q_net_income": _series(qinc, ["Net Income", "Net Income Common Stockholders"]),
                "q_revenue": _series(qinc, ["Total Revenue", "Operating Revenue"]),
                "q_operating_income": _series(qinc, ["Operating Income"]),
            }
            if not any(data.values()):
                return None
            data["as_of"] = date.today().isoformat()
            return data

        return await self._cached(f"yf:fin:{ticker}", FUNDAMENTALS_TTL, fetch)

    async def history(self, ticker: str, period: str = "1y") -> list[tuple[str, float]] | None:
        def fetch() -> list[tuple[str, float]] | None:
            df = self.yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if df is None or df.empty:
                return None
            return [
                (idx.date().isoformat(), float(v)) for idx, v in df["Close"].items() if _clean(v) is not None
            ]

        return await self._cached(f"yf:hist:{ticker}:{period}", HISTORY_TTL, fetch)

    async def close_on(self, ticker: str, day: date) -> float | None:
        """Schlusskurs am oder kurz vor einem Datum (für Performance-Messung)."""

        def fetch() -> float | None:
            start = day - timedelta(days=7)
            df = self.yf.Ticker(ticker).history(
                start=start.isoformat(), end=(day + timedelta(days=1)).isoformat()
            )
            if df is None or df.empty:
                return None
            return float(df["Close"].iloc[-1])

        return await self._cached(f"yf:close:{ticker}:{day.isoformat()}", 30 * 24 * 3600, fetch)

    async def fx(self, from_ccy: str, to_ccy: str) -> float | None:
        """Wechselkurs from->to. GBp (Pence) wird berücksichtigt."""
        factor = 1.0
        if from_ccy in ("GBp", "GBX"):
            from_ccy, factor = "GBP", 0.01
        if from_ccy == to_ccy:
            return factor
        q = await self.quote(f"{from_ccy}{to_ccy}=X")
        if not q or not q.get("price"):
            return None
        return float(q["price"]) * factor


def latest(series: list[tuple[str, float]] | list[list]) -> tuple[str, float] | None:
    return tuple(series[-1]) if series else None  # type: ignore[return-value]


def as_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        return None
