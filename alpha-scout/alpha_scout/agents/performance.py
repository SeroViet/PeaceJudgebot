"""PerformanceTracker: jede Empfehlung nach 3/6/12/36 Monaten vs. MSCI World; Agenten-Gewichte."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from alpha_scout.context import Context

log = logging.getLogger(__name__)

NAME = "performance"
BENCHMARK = "URTH"  # iShares MSCI World ETF (USD), lange Kurshistorie
HORIZONS = (3, 6, 12, 36)
MIN_SAMPLES = 5
BULLISH = {"NEW_CHANCE", "BUY_MORE", "SUPERINVESTOR"}


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError("ungültiges Datum")  # pragma: no cover


@dataclass
class Evaluation:
    rec_id: int
    horizon: int
    return_pct: float
    benchmark_pct: float

    @property
    def outperformance(self) -> float:
        return self.return_pct - self.benchmark_pct


def agent_hit(agent_score: float | None, rec_type: str, outperformance: float) -> bool | None:
    """War der Agent richtig? Kaufsignal: hoher Score + Outperformance; Verkaufssignal umgekehrt."""
    if agent_score is None:
        return None
    bullish_agent = agent_score >= 60
    if rec_type in BULLISH:
        return bullish_agent == (outperformance > 0)
    return (not bullish_agent) == (outperformance <= 0)


def factor_from_hit_rate(hit_rate: float) -> float:
    return round(max(0.5, min(1.5, 0.5 + hit_rate)), 2)


class PerformanceTracker:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def evaluate_due(self) -> list[Evaluation]:
        today = self.ctx.today()
        recs = await self.ctx.db.fetchall("SELECT * FROM recommendations ORDER BY id")
        done = {
            (r["rec_id"], r["horizon_months"])
            for r in await self.ctx.db.fetchall("SELECT rec_id, horizon_months FROM rec_evaluations")
        }
        out = []
        for r in recs:
            start = datetime.fromisoformat(r["ts"]).date()
            for h in HORIZONS:
                if (r["id"], h) in done:
                    continue
                target = add_months(start, h)
                if target > today:
                    continue
                price_end = await self._price(r["subject"], target)
                b0 = await self.ctx.market.close_on(BENCHMARK, start)
                b1 = await self.ctx.market.close_on(BENCHMARK, target)
                if price_end is None or not b0 or not b1 or not r["price"]:
                    continue
                ev = Evaluation(r["id"], h, (price_end / r["price"] - 1) * 100, (b1 / b0 - 1) * 100)
                await self.ctx.db.execute(
                    "INSERT OR REPLACE INTO rec_evaluations (rec_id, horizon_months, ts, return_pct, benchmark_pct) VALUES (?, ?, ?, ?, ?)",
                    (ev.rec_id, h, self.ctx.now().isoformat(), ev.return_pct, ev.benchmark_pct),
                )
                out.append(ev)
        if out:
            await self.update_weights()
        return out

    async def _price(self, subject: str, day: date) -> float | None:
        crypto = self.ctx.cfg.crypto.allowlist.get(subject.upper())
        if crypto:
            return await self.ctx.market.close_on(f"{subject.upper()}-USD", day)
        return await self.ctx.market.close_on(subject, day)

    async def update_weights(self) -> dict[str, float]:
        rows = await self.ctx.db.fetchall(
            """SELECT r.type, r.agent_scores, e.return_pct - e.benchmark_pct AS outperf
               FROM rec_evaluations e JOIN recommendations r ON r.id = e.rec_id
               WHERE e.horizon_months = (SELECT MAX(horizon_months) FROM rec_evaluations e2 WHERE e2.rec_id = e.rec_id)"""
        )
        stats: dict[str, list[bool]] = {}
        for row in rows:
            for agent, score in json.loads(row["agent_scores"]).items():
                hit = agent_hit(score, row["type"], row["outperf"])
                if hit is not None:
                    stats.setdefault(agent, []).append(hit)
        factors = {}
        for agent, hits in stats.items():
            hit_rate = sum(hits) / len(hits)
            factor = factor_from_hit_rate(hit_rate) if len(hits) >= MIN_SAMPLES else 1.0
            factors[agent] = factor
            await self.ctx.db.execute(
                "INSERT OR REPLACE INTO agent_weights (agent, factor, hit_rate, samples, updated) VALUES (?, ?, ?, ?, ?)",
                (agent, factor, hit_rate, len(hits), self.ctx.now().isoformat()),
            )
        return factors

    async def summary(self) -> dict:
        rows = await self.ctx.db.fetchall(
            "SELECT horizon_months, COUNT(*) AS n, AVG(return_pct) AS ret, AVG(benchmark_pct) AS bench, "
            "SUM(CASE WHEN return_pct > benchmark_pct THEN 1 ELSE 0 END) AS wins FROM rec_evaluations GROUP BY horizon_months"
        )
        weights = await self.ctx.db.fetchall(
            "SELECT agent, factor, hit_rate, samples FROM agent_weights ORDER BY agent"
        )
        open_recs = await self.ctx.db.fetchone("SELECT COUNT(*) AS n FROM recommendations")
        return {
            "by_horizon": [dict(r) for r in rows],
            "weights": [dict(w) for w in weights],
            "recommendations": open_recs["n"] if open_recs else 0,
        }
