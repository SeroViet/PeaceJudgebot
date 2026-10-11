from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alpha_scout.config import AppConfig, Settings
from alpha_scout.context import Context
from alpha_scout.data.crypto_sources import CoinGecko, DefiLlama
from alpha_scout.data.news_sources import Gdelt, RssReader
from alpha_scout.data.tenders import Tenders
from alpha_scout.db import Database
from alpha_scout.http import HttpClient
from alpha_scout.llm.budget import BudgetGuard
from alpha_scout.llm.client import LLM
from alpha_scout.models import Fact, FactSheet

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(ROOT / "config", dst)
    return dst


def sheet_from(values: dict[str, Any], subject: str = "TEST") -> FactSheet:
    s = FactSheet(subject=subject)
    for k, v in values.items():
        s.add(Fact(id=k, label=k, value=v, source="Test", as_of=date(2026, 9, 30)))
    return s


class FakeMarket:
    """Ersetzt yfinance: feste Kurse/Fundamentaldaten je Ticker."""

    def __init__(self, quotes: dict[str, dict] | None = None, fins: dict[str, dict] | None = None):
        self.quotes = quotes or {}
        self.fins = fins or {}

    async def quote(self, ticker: str):
        if ticker.endswith("=X"):
            pair = ticker[:-2]
            return {"price": {"USDCHF": 0.8, "EURUSD": 1.1, "EURCHF": 0.94, "CHFUSD": 1.25}.get(pair, 1.0)}
        q = self.quotes.get(ticker)
        return dict(q, as_of="2026-10-01") if q else None

    async def financials(self, ticker: str):
        return self.fins.get(ticker)

    async def history(self, ticker: str, period: str = "1y"):
        return None

    async def close_on(self, ticker: str, day: date):
        return None

    async def fx(self, a: str, b: str):
        if a == b:
            return 1.0
        q = await self.quote(f"{a}{b}=X")
        return q["price"] if q else None


class FakeSec:
    def __init__(self, trades=None):
        self.trades = trades

    async def cik_for(self, ticker: str):
        return None

    async def company_facts(self, cik: str):
        return None

    async def insider_trades(self, ticker: str, days: int = 30):
        return self.trades

    async def ticker_for_name(self, name: str):
        return None


class FakeLLM(LLM):
    """LLM mit vorgegebenen Antworten je Aufgaben-Art (kein Netzwerk)."""

    def __init__(self, budget: BudgetGuard, answers: dict[str, Any] | None = None):
        super().__init__(
            budget, api_key=None, model_filter="claude-haiku-5-5", model_analysis="claude-sonnet-5-5"
        )
        self.answers = answers or {}
        self.calls: list[str] = []
        self.enabled = True

    async def run(self, task):
        self.calls.append(task.custom_id)
        return self.answers.get(task.kind)


@pytest.fixture
async def make_ctx(db, config_dir):
    created = []

    async def factory(market: FakeMarket | None = None, sec: FakeSec | None = None, answers=None) -> Context:
        settings = Settings(
            _env_file=None,
            config_dir=config_dir,
            db_path=Path(":memory:"),
            sec_edgar_user_agent="Test t@e.st",
        )
        http = HttpClient(db, "test")
        budget = BudgetGuard(db, 3.0)
        ctx = Context(
            settings=settings,
            cfg=AppConfig(config_dir),
            db=db,
            http=http,
            market=market or FakeMarket(),
            sec=sec or FakeSec(),
            llm=FakeLLM(budget, answers),
            gdelt=Gdelt(http),
            rss=RssReader(http),
            coingecko=CoinGecko(http),
            defillama=DefiLlama(http),
            tenders=Tenders(http),
        )
        created.append(http)
        return ctx

    yield factory
    for h in created:
        await h.close()
