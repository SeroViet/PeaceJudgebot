import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from alpha_scout.agents.news import NewsAgent
from alpha_scout.agents.superinvestor import SuperinvestorAgent
from alpha_scout.alerts import Dispatcher
from alpha_scout.data.news_sources import NewsItem
from alpha_scout.data.sec_edgar import Holding13F
from alpha_scout.http import HttpClient, HttpError
from alpha_scout.jobs import Jobs
from alpha_scout.llm.budget import BudgetGuard
from alpha_scout.llm.client import LLM, LLMTask
from alpha_scout.llm.prompts import NewsItemVerdict, NewsVerdicts

from .conftest import FakeMarket, FakeSec
from .test_pipeline_commands import DEVIL_OK, FIN, MOAT, QUOTE


async def test_nightly_batch_roundtrip(make_ctx, config_dir):
    (config_dir / "watchlist.yaml").write_text("qualitaet: [QUAL]\nkrypto: []\n")
    ctx = await make_ctx(FakeMarket({"QUAL": QUOTE}, {"QUAL": FIN}), FakeSec(trades=[]))
    submitted = {}

    async def submit_batch(tasks, purpose):
        submitted["tasks"] = tasks
        return "batch_1", tasks

    async def batch_status(batch_id):
        return "ended"

    async def collect_batch(batch_id, kinds):
        return {"moat-QUAL": MOAT, "devil-QUAL": DEVIL_OK}

    ctx.llm.submit_batch = submit_batch
    ctx.llm.batch_status = batch_status
    ctx.llm.collect_batch = collect_batch
    sent = []

    async def sender(t):
        sent.append(t)

    ctx.settings.quiet_hours = "0-0"
    jobs = Jobs(ctx, Dispatcher(ctx, sender))
    batch_id = await jobs.nightly_submit(include_universe=False)
    assert batch_id == "batch_1"
    assert {t.custom_id for t in submitted["tasks"]} == {"moat-QUAL", "devil-QUAL"}
    assert (await ctx.db.open_batches())[0]["id"] == "batch_1"
    await jobs.poll_batches()
    assert await ctx.db.open_batches() == []
    stored = await ctx.db.get_analysis("QUAL")
    assert stored["payload"]["moat"]["moat"] == "stark" and stored["score"] > 0


async def test_submit_batch_drops_tasks_over_budget(db):
    created = {}

    class Batches:
        async def create(self, requests):
            created["n"] = len(requests)
            return SimpleNamespace(id="b1")

    client = SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    llm = LLM(
        BudgetGuard(db, 0.06),
        api_key=None,
        model_filter="claude-haiku-5-5",
        model_analysis="claude-sonnet-5-5",
        client=client,
    )
    tasks = [
        LLMTask(
            custom_id=f"moat-{i}",
            kind="moat",
            agent="q",
            role="analysis",
            facts_json="{}",
            instruction="x",
            max_tokens=4000,
        )
        for i in range(5)
    ]
    batch_id, sent = await llm.submit_batch(tasks, "nightly")
    assert batch_id == "b1" and 0 < len(sent) < 5 and created["n"] == len(sent)
    assert await llm.budget.spent_today() <= 0.06 + 1e-9  # Reservierung im Limit


async def test_superinvestor_refresh_and_holders(make_ctx):
    class Sec(FakeSec):
        async def latest_13f(self, cik, n=2):
            if cik != "0001067983":
                return []
            return [
                {
                    "period": "2026-06-30",
                    "filing_date": "2026-08-14",
                    "accession": "a2",
                    "holdings": [
                        Holding13F("QUALITY CORP", "C1", 50.0, 10),
                        Holding13F("OTHER INC", "C2", 50.0, 10),
                    ],
                },
                {
                    "period": "2026-03-31",
                    "filing_date": "2026-05-15",
                    "accession": "a1",
                    "holdings": [Holding13F("OTHER INC", "C2", 100.0, 10)],
                },
            ]

        async def ticker_for_name(self, name):
            return {"QUALITY CORP": "QUAL", "OTHER INC": "OTH"}.get(name)

    ctx = await make_ctx(FakeMarket(), Sec())
    agent = SuperinvestorAgent(ctx)
    fresh = await agent.refresh()
    assert [(c.ticker, c.action) for c in fresh] == [("QUAL", "new")]
    assert await agent.refresh() == []  # Meldung schon gesehen
    res = await agent.holders("QUAL", "Quality Corp")
    assert res.data["holders"] == ["Berkshire Hathaway (Buffett)"] and res.score >= 65


async def test_news_classify_with_llm(make_ctx):
    verdicts = NewsVerdicts(
        items=[
            NewsItemVerdict(
                fact_id="news.0",
                importance=85,
                category="firma",
                tickers=["QUAL"],
                thesis_risk=True,
                summary="Gewinnwarnung",
            ),
            NewsItemVerdict(
                fact_id="news.1",
                importance=20,
                category="rauschen",
                tickers=[],
                thesis_risk=False,
                summary="-",
            ),
            NewsItemVerdict(
                fact_id="news.9",
                importance=99,
                category="makro",
                tickers=[],
                thesis_risk=False,
                summary="ungültig",
            ),
        ]
    )
    ctx = await make_ctx(FakeMarket({"QUAL": QUOTE}), FakeSec(), {"news": verdicts})
    agent = NewsAgent(ctx)

    async def terms():
        return {"quality": "QUAL"}

    agent.watch_terms = terms
    items = [
        NewsItem("Quality Corp cuts guidance", "u1", "GDELT/x", None),
        NewsItem("Stocks drift", "u2", "GDELT/y", None),
    ]
    out = await agent.classify(items)
    assert len(out) == 1 and out[0].tickers == ["QUAL"] and out[0].thesis_risk


@respx.mock
async def test_http_retry_and_cache(db):
    route = respx.get("https://example.test/data").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    http = HttpClient(db, "test", attempts=3)
    import alpha_scout.http as h

    h_wait = h.wait_exponential_jitter
    h.wait_exponential_jitter = lambda **k: lambda rs: 0
    try:
        assert await http.get_json("https://example.test/data", ttl=60) == {"ok": 1}
        assert await http.get_json("https://example.test/data", ttl=60) == {"ok": 1}  # aus Cache
        assert route.call_count == 2
        respx.get("https://example.test/404").mock(return_value=httpx.Response(404))
        with pytest.raises(HttpError):
            await http.get_json("https://example.test/404")
    finally:
        h.wait_exponential_jitter = h_wait
        await http.close()


def test_json_roundtrip_of_signal_payload():
    assert json.loads(json.dumps({"a": 1})) == {"a": 1}


async def test_superinvestor_job_ignores_old_filings(make_ctx):
    from datetime import date, timedelta

    class Sec(FakeSec):
        def __init__(self, filed):
            self.filed = filed

        async def latest_13f(self, cik, n=2):
            if cik != "0001067983":
                return []
            return [
                {
                    "period": "p2",
                    "filing_date": self.filed,
                    "accession": f"a-{self.filed}",
                    "holdings": [
                        Holding13F("QUALITY CORP", "C1", 50.0, 10),
                        Holding13F("OTHER INC", "C2", 50.0, 10),
                    ],
                },
                {
                    "period": "p1",
                    "filing_date": "2026-01-01",
                    "accession": "a1",
                    "holdings": [Holding13F("OTHER INC", "C2", 100.0, 10)],
                },
            ]

        async def ticker_for_name(self, name):
            return None

    sent = []

    async def sender(t):
        sent.append(t)

    old = (date.today() - timedelta(days=60)).isoformat()
    ctx = await make_ctx(FakeMarket(), Sec(old))
    ctx.settings.quiet_hours = "0-0"
    await Jobs(ctx, Dispatcher(ctx, sender)).superinvestors()
    assert sent == []
    new = (date.today() - timedelta(days=3)).isoformat()
    ctx.sec = Sec(new)
    jobs = Jobs(ctx, Dispatcher(ctx, sender))
    await jobs.superinvestors()
    assert len(sent) == 1 and "SUPERINVESTOR" in sent[0] and "QUALITY CORP" in sent[0]
