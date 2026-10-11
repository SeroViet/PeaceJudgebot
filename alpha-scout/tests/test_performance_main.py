from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha_scout.agents.performance import PerformanceTracker
from alpha_scout.alerts import Dispatcher
from alpha_scout.jobs import Jobs
from alpha_scout.main import schedule

from .conftest import FakeMarket


class PerfMarket(FakeMarket):
    """Aktie +20 %, MSCI World in USD +10 %, USD/CHF -10 % -> Benchmark in CHF ~ -1 %."""

    def __init__(self, start: date):
        super().__init__()
        self.start = start

    async def close_on(self, ticker, day):
        early = day <= self.start
        return {
            "QUAL.SW": 100.0 if early else 120.0,
            "URTH": 100.0 if early else 110.0,
            "USDCHF=X": 0.9 if early else 0.81,
        }.get(ticker)


async def test_performance_in_recommendation_currency(make_ctx):
    start = date.today() - timedelta(days=100)
    ctx = await make_ctx(PerfMarket(start))
    await ctx.db.execute(
        "INSERT INTO recommendations (ts, subject, type, score, price, currency, agent_scores) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            start.isoformat() + "T08:00:00",
            "QUAL.SW",
            "NEW_CHANCE",
            85,
            100.0,
            "CHF",
            '{"quality": 90, "insider": 40}',
        ),
    )
    evals = await PerformanceTracker(ctx).evaluate_due()
    assert [e.horizon for e in evals] == [3]
    e = evals[0]
    assert round(e.return_pct, 1) == 20.0
    assert round(e.benchmark_pct, 1) == -1.0  # 110*0.81 / (100*0.9) - 1
    assert e.outperformance > 0
    assert await PerformanceTracker(ctx).evaluate_due() == []  # nicht doppelt


async def test_schedule_registers_all_jobs(make_ctx):
    ctx = await make_ctx()
    jobs = Jobs(ctx, Dispatcher(ctx, None))
    sched = AsyncIOScheduler(timezone="Europe/Zurich")
    schedule(sched, jobs, "Europe/Zurich")
    ids = {j.id for j in sched.get_jobs()}
    assert {"intraday", "nightly", "batch_poll", "weekly", "superinvestor", "monthly", "flush"} <= ids
