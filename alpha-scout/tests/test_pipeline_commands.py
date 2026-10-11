from datetime import date

import pytest
import yaml

from alpha_scout.alerts import Dispatcher
from alpha_scout.jobs import Jobs
from alpha_scout.llm.prompts import Claim, DevilsVerdict, MoatAssessment
from alpha_scout.models import Alert, AlertType
from alpha_scout.pipeline import Pipeline
from alpha_scout.report import format_check
from alpha_scout.telegram.commands import Commands

from .conftest import FakeMarket, FakeSec

QUOTE = {
    "ticker": "QUAL",
    "price": 100.0,
    "currency": "USD",
    "financial_currency": "USD",
    "market_cap": 1e10,
    "exchange": "NMS",
    "exchange_name": "NasdaqGS",
    "quote_type": "EQUITY",
    "name": "Quality Corp",
    "sector": "Technology",
    "country": "United States",
    "summary": "Marktführer für Software.",
    "high_52w": 140.0,
    "forward_eps": 5.0,
    "gross_margin": 0.7,
    "roe": 0.3,
}
YEARS = ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
FIN = {
    "revenue": list(zip(YEARS, [1e9, 1.2e9, 1.44e9, 1.728e9], strict=True)),
    "ebitda": [(YEARS[-1], 6e8)],
    "operating_income": [(YEARS[-1], 5e8)],
    "net_income": [(YEARS[-1], 4e8)],
    "total_debt": [(YEARS[-1], 2e8)],
    "cash": [(YEARS[-1], 5e8)],
    "shares": list(zip(YEARS, [100e6, 100e6, 99e6, 98e6], strict=True)),
    "fcf": [(YEARS[-1], 1.2e9)],
    "q_net_income": [("q", 1e8)] * 4,
    "q_revenue": [("q", 4.5e8)] * 4,
}
MOAT = MoatAssessment(
    moat="stark",
    moat_types=["marktfuehrer"],
    evidence=[
        Claim(text="Hohe Bruttomarge", fact_ids=["fund.gross_margin"]),
        Claim(text="Marktführer laut Beschreibung", fact_ids=["profile.summary"]),
    ],
    concerns=[],
)
DEVIL_OK = DevilsVerdict(
    counterarguments=[Claim(text="Bewertung", fact_ids=["val.fcf_yield"])],
    knockout=False,
    knockout_reason=None,
)
DEVIL_KO = DevilsVerdict(
    counterarguments=[], knockout=True, knockout_reason=Claim(text="Betrug", fact_ids=["profile.summary"])
)


@pytest.fixture
async def ctx(make_ctx):
    return await make_ctx(
        FakeMarket({"QUAL": QUOTE}, {"QUAL": FIN}), FakeSec(trades=[]), {"moat": MOAT, "devil": DEVIL_OK}
    )


async def test_pipeline_live_good_company(ctx):
    a = await Pipeline(ctx).analyze_live("qual")
    assert a.error is None and a.subject == "QUAL"
    assert a.sheet.value("quality.passed") is True
    assert a.sheet.value("fund.revenue_cagr_3y") == pytest.approx(20.0, abs=0.01)
    assert a.results["valuation"].data["in_buy_zone"] is True  # FCF-Rendite 12 % -> günstig
    assert a.score and a.score >= 75 and not a.vetoes
    assert set(ctx.llm.calls) == {"moat-QUAL", "devil-QUAL"}
    text = format_check(a)
    assert "Quality Corp" in text and "Qualitätsfilter (5/6)" in text
    stored = await ctx.db.get_analysis("QUAL")
    assert stored["payload"]["moat"]["moat"] == "stark"


async def test_pipeline_reuses_llm_results(ctx):
    p = Pipeline(ctx)
    await p.analyze_live("QUAL")
    ctx.llm.calls.clear()
    a = await p.prepare("QUAL")
    assert a.moat is not None and a.devil is not None and a.pending_tasks == []


async def test_devil_knockout_blocks(make_ctx):
    ctx = await make_ctx(
        FakeMarket({"QUAL": QUOTE}, {"QUAL": FIN}), FakeSec(trades=[]), {"moat": MOAT, "devil": DEVIL_KO}
    )
    a = await Pipeline(ctx).analyze_live("QUAL")
    assert a.score == 0.0 and any("devils_advocate" in v for v in a.vetoes)


async def test_unknown_ticker(ctx):
    a = await Pipeline(ctx).analyze_live("NOPE")
    assert a.error and "Ticker" in format_check(a)


async def test_jobs_act_on_sends_new_chance_once(ctx):
    sent = []

    async def sender(text):
        sent.append(text)

    ctx.settings.quiet_hours = "0-0"
    jobs = Jobs(ctx, Dispatcher(ctx, sender))
    a = await jobs.pipeline.analyze_live("QUAL")
    a.score = 90.0
    await jobs.act_on(a, use_llm_writer=False)
    await jobs.act_on(a, use_llm_writer=False)
    assert len(sent) == 1 and "NEUE CHANCE" in sent[0] and "Quality Corp" in sent[0]
    recs = await ctx.db.fetchall("SELECT * FROM recommendations")
    assert len(recs) == 1 and recs[0]["price"] == 100.0


async def test_dispatch_quiet_hours_queues(ctx):
    sent = []

    async def sender(text):
        sent.append(text)

    d = Dispatcher(ctx, sender)
    ctx.settings.quiet_hours = "0-24"  # immer Ruhezeit
    alert = Alert(type=AlertType.NEW_CHANCE, subject="X", title="X", dedupe_key="NEW_CHANCE:X")
    assert await d.dispatch(alert)
    assert sent == [] and len(await ctx.db.pending_alerts()) == 1
    urgent = Alert(type=AlertType.WARNING, subject="Y", title="Y", dedupe_key="WARNING:Y", urgent=True)
    await d.dispatch(urgent)
    assert len(sent) == 1
    ctx.settings.quiet_hours = "0-0"
    assert await d.flush_pending() == 1 and len(sent) == 2


async def test_commands_buy_sell_roundtrip(ctx, config_dir):
    cmds = Commands(Jobs(ctx, Dispatcher(ctx, None)))
    msg = await cmds.buy(["QUAL", "10", "100", "qualitaet", "01.02.2026", "Software-Marktführer"])
    assert "Kauf erfasst" in msg and "01.08.2026" not in msg
    data = yaml.safe_load((config_dir / "holdings.yaml").read_text())
    pos = data["positions"][0]
    assert pos["ticker"] == "QUAL" and pos["currency"] == "USD" and pos["thesis"] == "Software-Marktführer"
    assert pos["buy_date"] == "2026-02-01"
    msg = await cmds.buy(["QUAL", "10", "120"])
    assert "Ø 110.00" in msg
    msg = await cmds.sell(["QUAL", "5", "130"])
    assert "Verkauf erfasst" in msg and "Rest: 15" in msg
    assert "nicht im Bestand" in await cmds.sell(["MSFT", "1"])
    assert "Nur 15" in await cmds.sell(["QUAL", "99"])
    holdings = await cmds.holdings([])
    assert "QUAL" in holdings and "Depot" in holdings


async def test_commands_misc(ctx):
    cmds = Commands(Jobs(ctx, Dispatcher(ctx, None)))
    assert "Bitte Ticker" in await cmds.check([])
    assert "Watchlist" in await cmds.watchlist([])
    assert "LLM-Kosten" in await cmds.costs([])
    assert "Status" in await cmds.status([])
    assert "Noch keine Analysen" in await cmds.top([])
    await cmds.check(["QUAL"])
    assert "QUAL" in await cmds.top([])
    assert "Format" in await cmds.buy(["X"])
    assert "Zahlen" in await cmds.buy(["X", "a", "b"])


async def test_sell_lock_warning(ctx):
    cmds = Commands(Jobs(ctx, Dispatcher(ctx, None)))
    today = date.today().strftime("%d.%m.%Y")
    await cmds.buy(["QUAL", "1", "100", today])
    msg = await cmds.sell(["QUAL", "1"])
    assert "Steuerregel" in msg and "Position geschlossen" in msg
