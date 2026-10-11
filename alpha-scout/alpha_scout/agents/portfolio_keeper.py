"""PortfolioKeeper: Bestand und Watchlist laufend prüfen; /buy und /sell."""

from __future__ import annotations

import logging
from datetime import date

from alpha_scout.config import Holdings, Position
from alpha_scout.context import Context
from alpha_scout.llm.prompts import ThesisCheck, valid_claims
from alpha_scout.orchestrator import thesis_task
from alpha_scout.pipeline import Analysis

log = logging.getLogger(__name__)

NAME = "portfolio_keeper"


def add_buy(
    h: Holdings,
    ticker: str,
    shares: float,
    price: float,
    currency: str,
    buy_date: date,
    bucket: str,
    thesis: str,
) -> Position:
    """Kauf erfassen. Bestehende Position: Durchschnittskurs; Kaufdatum bleibt das erste (6-Monats-Regel)."""
    existing = next((p for p in h.positions if p.ticker == ticker), None)
    if existing:
        total = existing.shares + shares
        existing.buy_price = (existing.buy_price * existing.shares + price * shares) / total
        existing.shares = total
        if thesis:
            existing.thesis = thesis
        return existing
    pos = Position(
        ticker=ticker,
        shares=shares,
        buy_price=price,
        currency=currency,
        buy_date=buy_date,
        bucket=bucket,
        thesis=thesis,
    )
    h.positions.append(pos)
    return pos


def apply_sell(h: Holdings, ticker: str, shares: float) -> Position | None:
    pos = next((p for p in h.positions if p.ticker == ticker), None)
    if pos is None:
        raise ValueError(f"{ticker} ist nicht im Bestand")
    if shares > pos.shares + 1e-9:
        raise ValueError(f"Nur {pos.shares:g} Stück im Bestand")
    pos.shares -= shares
    if pos.shares <= 1e-9:
        h.positions.remove(pos)
        return None
    return pos


def sell_lock_info(pos: Position, today: date, min_days: int) -> str | None:
    days = (today - pos.buy_date).days
    if days < min_days:
        from datetime import timedelta

        return f"Haltedauer {days} Tage – Verkauf frühestens ab {(pos.buy_date + timedelta(days=min_days)).strftime('%d.%m.%Y')}"
    return None


class PortfolioKeeper:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def thesis_check(self, a: Analysis) -> ThesisCheck | None:
        """LLM-Prüfung der Kaufthese – nur wenn es neue Informationen gibt (spart Budget)."""
        if not a.sheet.value("port.thesis"):
            return None
        has_news = any(k.startswith(("news.", "ins.", "risk.")) for k in a.sheet.facts)
        prev = await self.ctx.db.get_analysis(a.subject)
        changed = prev is None or prev["facts_hash"] != (a.facts_hash or a.stable_hash())
        if not (has_news or changed):
            return None
        out = await self.ctx.llm.run(thesis_task(a))
        if not isinstance(out, ThesisCheck):
            return None
        out.reasons = valid_claims(out.reasons, set(a.sheet.facts))
        if out.status in ("gefaehrdet", "zerbrochen") and not out.reasons:
            log.info("These-Check %s ohne belegte Gründe – ignoriert", a.subject)
            return None
        return out
