"""Depot-Bewertung in CHF (Grundlage für RiskAgent, PortfolioKeeper und Monatsbericht)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from alpha_scout.config import Holdings, Position
from alpha_scout.data.market import MarketData

log = logging.getLogger(__name__)


@dataclass
class PositionValue:
    position: Position
    price: float | None
    currency: str
    value_chf: float | None
    sector: str | None
    country: str | None
    pnl_pct: float | None

    @property
    def holding_days(self) -> int:
        return (date.today() - self.position.buy_date).days


@dataclass
class PortfolioSnapshot:
    positions: list[PositionValue] = field(default_factory=list)
    cash_chf: float = 0.0
    missing_prices: list[str] = field(default_factory=list)

    @property
    def invested_chf(self) -> float:
        return sum(p.value_chf or 0 for p in self.positions)

    @property
    def total_chf(self) -> float:
        return self.invested_chf + self.cash_chf

    def pct(self, value: float) -> float:
        return value / self.total_chf * 100 if self.total_chf > 0 else 0.0

    def bucket_pct(self, bucket: str) -> float:
        return self.pct(sum(p.value_chf or 0 for p in self.positions if p.position.bucket == bucket))

    def ticker_pct(self, ticker: str) -> float:
        return self.pct(sum(p.value_chf or 0 for p in self.positions if p.position.ticker == ticker))

    def sector_pct_of_stocks(self, sector: str) -> float:
        stocks = [p for p in self.positions if p.position.bucket == "qualitaet"]
        total = sum(p.value_chf or 0 for p in stocks)
        if total <= 0:
            return 0.0
        return sum(p.value_chf or 0 for p in stocks if p.sector == sector) / total * 100

    def get(self, ticker: str) -> PositionValue | None:
        return next((p for p in self.positions if p.position.ticker == ticker), None)


async def snapshot(
    holdings: Holdings, market: MarketData, base: str = "CHF", crypto_prices: dict[str, float] | None = None
) -> PortfolioSnapshot:
    snap = PortfolioSnapshot(cash_chf=holdings.cash_chf)
    for pos in holdings.positions:
        price, ccy, sector, country = None, pos.currency, None, None
        if crypto_prices and pos.ticker.upper() in crypto_prices:
            price, ccy = crypto_prices[pos.ticker.upper()], base
        else:
            q = await market.quote(pos.ticker)
            if q and q.get("price") is not None:
                price, ccy = float(q["price"]), q.get("currency") or pos.currency
                sector, country = q.get("sector"), q.get("country")
        value = None
        if price is not None:
            fx = await market.fx(ccy, base)
            if fx is not None:
                value = price * pos.shares * fx
        if value is None:
            snap.missing_prices.append(pos.ticker)
        pnl = (price / pos.buy_price - 1) * 100 if price and pos.buy_price and ccy == pos.currency else None
        snap.positions.append(PositionValue(pos, price, ccy, value, sector, country, pnl))
    return snap
