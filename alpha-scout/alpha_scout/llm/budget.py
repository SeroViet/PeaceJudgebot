"""Preise und hartes Tagesbudget für LLM-Aufrufe."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from alpha_scout.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    input: float  # USD pro 1 Mio. Tokens
    output: float
    cache_read: float
    cache_write: float


# Anthropic-Listenpreise (Stand Oktober 2026)
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
}
# Unbekanntes Modell (z. B. Server-Fallback): konservativ teuer rechnen
FALLBACK_PRICE = Price(10.00, 50.00, 1.00, 12.50)
BATCH_DISCOUNT = 0.5
CHARS_PER_TOKEN = 3.0  # konservativ (echte Werte eher 3.5–4)


def price_for(model: str) -> Price:
    return PRICES.get(model, FALLBACK_PRICE)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def worst_case_cost(model: str, input_chars: int, max_tokens: int, *, batch: bool = False) -> float:
    p = price_for(model)
    cost = (input_chars / CHARS_PER_TOKEN) * p.input / 1e6 + max_tokens * p.output / 1e6
    return cost * (BATCH_DISCOUNT if batch else 1.0)


def actual_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    batch: bool = False,
) -> float:
    p = price_for(model)
    cost = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read * p.cache_read
        + cache_write * p.cache_write
    ) / 1e6
    return cost * (BATCH_DISCOUNT if batch else 1.0)


class BudgetExceeded(Exception):
    pass


class BudgetGuard:
    """Hartes Tageslimit. Jeder Aufruf reserviert vorab den schlimmsten Fall."""

    def __init__(self, db: Database, daily_limit_usd: float, timezone: str = "Europe/Zurich"):
        self.db = db
        self.limit = daily_limit_usd
        self.tz = ZoneInfo(timezone)
        self._lock = asyncio.Lock()
        self._inflight = 0.0

    def today(self) -> date:
        return datetime.now(self.tz).date()

    async def spent_today(self) -> float:
        return await self.db.cost_for_day(self.today())

    async def remaining(self) -> float:
        return self.limit - await self.spent_today() - self._inflight

    async def reserve(self, amount: float) -> None:
        """Reserviert Budget für einen Live-Aufruf; wirft BudgetExceeded, wenn es nicht reicht."""
        async with self._lock:
            remaining = self.limit - await self.spent_today() - self._inflight
            if amount > remaining:
                raise BudgetExceeded(
                    f"Tagesbudget erschöpft: benötigt ${amount:.4f}, übrig ${max(remaining, 0):.4f}"
                )
            self._inflight += amount

    async def settle(
        self, reserved: float, *, model: str, agent: str, cost: float, usage: dict[str, int]
    ) -> None:
        async with self._lock:
            self._inflight = max(0.0, self._inflight - reserved)
        await self.db.add_cost(model=model, agent=agent, cost_usd=cost, day=self.today(), **usage)

    async def cancel(self, reserved: float) -> None:
        async with self._lock:
            self._inflight = max(0.0, self._inflight - reserved)

    async def reserve_batch(self, amount: float, ref: str, model: str) -> None:
        """Batch-Reservierung bleibt in der DB, bis die Ergebnisse abgerechnet sind."""
        async with self._lock:
            remaining = self.limit - await self.spent_today() - self._inflight
            if amount > remaining:
                raise BudgetExceeded(
                    f"Budget für Batch reicht nicht: ${amount:.4f} > ${max(remaining, 0):.4f}"
                )
            await self.db.add_cost(
                model=model, agent="batch", cost_usd=amount, day=self.today(), kind="reserved", ref=ref
            )
