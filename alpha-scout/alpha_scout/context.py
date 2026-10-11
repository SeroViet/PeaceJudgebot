"""Gemeinsamer Laufzeit-Kontext (Dienste, die alle Agenten nutzen)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from alpha_scout.config import AppConfig, RuntimeFlags, Settings, seed_config
from alpha_scout.data.crypto_sources import CoinGecko, DefiLlama
from alpha_scout.data.market import MarketData
from alpha_scout.data.news_sources import Gdelt, RssReader
from alpha_scout.data.sec_edgar import SecEdgar
from alpha_scout.data.tenders import Tenders
from alpha_scout.db import Database
from alpha_scout.http import HttpClient
from alpha_scout.llm.budget import BudgetGuard
from alpha_scout.llm.client import LLM


@dataclass
class Context:
    settings: Settings
    cfg: AppConfig
    db: Database
    http: HttpClient
    market: MarketData
    sec: SecEdgar
    llm: LLM
    gdelt: Gdelt
    rss: RssReader
    coingecko: CoinGecko
    defillama: DefiLlama
    tenders: Tenders
    flags: RuntimeFlags = field(default_factory=RuntimeFlags)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    async def close(self) -> None:
        await self.http.close()
        await self.db.close()


async def build_context(settings: Settings) -> Context:
    seed_config(settings.config_dir, settings.config_seed_dir)
    db = Database(settings.db_path)
    await db.connect()
    ua = settings.sec_edgar_user_agent or "AlphaScout (bitte SEC_EDGAR_USER_AGENT setzen)"
    http = HttpClient(db, user_agent=ua)
    budget = BudgetGuard(db, settings.daily_llm_budget_usd, settings.timezone)
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    llm = LLM(
        budget, api_key=api_key, model_filter=settings.model_filter, model_analysis=settings.model_analysis
    )
    return Context(
        settings=settings,
        cfg=AppConfig(settings.config_dir),
        db=db,
        http=http,
        market=MarketData(db),
        sec=SecEdgar(http),
        llm=llm,
        gdelt=Gdelt(http),
        rss=RssReader(http),
        coingecko=CoinGecko(http),
        defillama=DefiLlama(http),
        tenders=Tenders(http),
    )
