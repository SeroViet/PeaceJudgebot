"""Einstellungen (.env) und YAML-Konfiguration."""

from __future__ import annotations

import re
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    sec_edgar_user_agent: str = ""
    daily_llm_budget_usd: float = 3.0

    timezone: str = "Europe/Zurich"
    quiet_hours: str = "22-7"
    base_currency: str = "CHF"
    log_level: str = "INFO"
    db_path: Path = Path("data/alpha_scout.db")
    config_dir: Path = Path("config")

    model_filter: str = "claude-haiku-5-5"
    model_analysis: str = "claude-sonnet-5-5"

    def quiet_window(self) -> tuple[int, int]:
        start, end = self.quiet_hours.split("-")
        return int(start), int(end)

    def secrets(self) -> list[str]:
        """Alle Geheimnisse – werden im Logging maskiert."""
        out = []
        for s in (self.anthropic_api_key, self.telegram_bot_token):
            if s is not None and s.get_secret_value():
                out.append(s.get_secret_value())
        return out


# ---------------------------------------------------------------- YAML-Modelle


class Watchlist(BaseModel):
    qualitaet: list[str] = []
    klein: list[str] = []
    ruestung: list[str] = []
    kaufstopp: dict[str, str] = {}
    beobachten: list[str] = []
    krypto: list[str] = []
    etf_kern: str = ""
    aliases: dict[str, str] = {}

    def resolve(self, ticker: str) -> str:
        for key, target in self.aliases.items():
            if key.upper() == ticker.upper():
                return target
        return ticker

    def stock_groups(self) -> dict[str, list[str]]:
        return {
            "qualitaet": self.qualitaet,
            "klein": self.klein,
            "ruestung": self.ruestung,
            "kaufstopp": list(self.kaufstopp),
            "beobachten": self.beobachten,
        }

    def all_securities(self) -> list[str]:
        """Alle Nicht-Krypto-Titel als Yahoo-Ticker (ohne Duplikate, Reihenfolge stabil)."""
        seen: dict[str, None] = {}
        for group in self.stock_groups().values():
            for t in group:
                seen[self.resolve(t)] = None
        return list(seen)

    def group_of(self, ticker: str) -> str | None:
        for name, group in self.stock_groups().items():
            if ticker in group or ticker in [self.resolve(t) for t in group]:
                return name
        if ticker.upper() in [c.upper() for c in self.krypto]:
            return "krypto"
        return None

    def buy_stop_until(self, ticker: str) -> tuple[date | None, str] | None:
        """Kaufstopp für Ticker? Gibt (Enddatum|None, Text) zurück."""
        for key, text in self.kaufstopp.items():
            if ticker in (key, self.resolve(key)):
                m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
                until = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
                return until, text
        return None

    def is_buy_stopped(self, ticker: str, today: date) -> bool:
        stop = self.buy_stop_until(ticker)
        if stop is None:
            return False
        until, _ = stop
        return until is None or today <= until


class Position(BaseModel):
    ticker: str
    shares: float
    buy_price: float
    currency: str = "USD"
    buy_date: date
    bucket: str = "qualitaet"  # kern | qualitaet | rest
    thesis: str = ""


class Holdings(BaseModel):
    positions: list[Position] = []
    cash_chf: float = 0.0


class SwissquoteUniverse(BaseModel):
    confirmed: list[str] = []
    crypto_confirmed: list[str] = []
    exchanges: dict[str, str] = {}
    excluded_us_exchanges: list[str] = []


class Superinvestor(BaseModel):
    name: str
    cik: str


class SuperinvestorConfig(BaseModel):
    investors: list[Superinvestor] = []
    big_new_position_pct: float = 3.0
    big_increase_pct: float = 50.0
    big_increase_min_weight: float = 1.0
    cluster_min_investors: int = 3


class ValuationConfig(BaseModel):
    discount_rate: float = 0.09
    safety_margin: float = 0.10
    exit_multiple: dict[str, float] = {"bear": 15, "base": 20, "bull": 25}
    growth_cap: float = 0.25
    terminal_fade: float = 0.5


class PortfolioRules(BaseModel):
    core_target_pct: float = 60
    quality_max_pct: float = 30
    rest_max_pct: float = 10
    single_stock_max_pct: float = 5
    sector_max_pct_of_stocks: float = 40
    tranche_pct: float = 1.0
    veto_countries: list[str] = []
    watch_countries: list[str] = []


class ScoringConfig(BaseModel):
    weights: dict[str, float]
    neutral_score: float = 50
    alert_new_chance_min: float = 80
    nachkauf_drop_pct: float = 20
    min_holding_days_for_sell: int = 183
    dedupe_days: int = 14
    valuation: ValuationConfig = ValuationConfig()
    portfolio: PortfolioRules = PortfolioRules()


class EtfCandidate(BaseModel):
    ticker: str
    domicile: str | None = None


class EtfCategory(BaseModel):
    label: str
    candidates: list[EtfCandidate]


class EtfUniverse(BaseModel):
    categories: dict[str, EtfCategory]


class CryptoAsset(BaseModel):
    coingecko: str
    defillama_fees: str | None = None


class CryptoConfig(BaseModel):
    allowlist: dict[str, CryptoAsset]
    candidates: dict[str, CryptoAsset] = {}
    min_history_years: float = 3
    min_fees_30d_usd: float = 1_000_000
    memecoin_blocklist: list[str] = []


class Theme(BaseModel):
    keywords: list[str]
    candidates: list[str] = []


class ThemesConfig(BaseModel):
    themes: dict[str, Theme]
    min_award_usd: float = 100_000_000
    contractor_aliases: dict[str, str] = {}


class RssFeed(BaseModel):
    name: str
    url: str


class NewsConfig(BaseModel):
    rss: list[RssFeed] = []
    gdelt_macro: list[str] = []
    min_importance: int = 70
    fraud_keywords: list[str] = []


class Universe(BaseModel):
    tickers: dict[str, list[str]] = {}

    def all(self) -> list[str]:
        return list(dict.fromkeys(t for group in self.tickers.values() for t in group))


class AppConfig:
    """Lädt die YAML-Dateien aus dem Config-Ordner (bei jedem Zugriff frisch für Watchlist/Bestand)."""

    def __init__(self, config_dir: Path):
        self.dir = config_dir

    def _load(self, name: str) -> dict[str, Any]:
        path = self.dir / name
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # Dateien, die du im Betrieb änderst -> immer frisch lesen
    @property
    def watchlist(self) -> Watchlist:
        return Watchlist.model_validate(self._load("watchlist.yaml"))

    @property
    def holdings(self) -> Holdings:
        return Holdings.model_validate(self._load("holdings.yaml"))

    def save_holdings(self, holdings: Holdings) -> None:
        path = self.dir / "holdings.yaml"
        data = holdings.model_dump(mode="json")
        header = (
            "# Dein Bestand. Pflege per Telegram (/buy, /sell) oder direkt hier.\n"
            "# bucket: kern (MSCI-World-ETF) | qualitaet (Einzelaktien) | rest (Krypto, Themen, Spekulation)\n"
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(path)

    @property
    def swissquote(self) -> SwissquoteUniverse:
        return SwissquoteUniverse.model_validate(self._load("swissquote_universe.yaml"))

    # Selten geänderte Dateien -> einmal laden
    @cached_property
    def superinvestors(self) -> SuperinvestorConfig:
        return SuperinvestorConfig.model_validate(self._load("superinvestors.yaml"))

    @cached_property
    def scoring(self) -> ScoringConfig:
        return ScoringConfig.model_validate(self._load("scoring.yaml"))

    @cached_property
    def etfs(self) -> EtfUniverse:
        return EtfUniverse.model_validate(self._load("etf_universe.yaml"))

    @cached_property
    def crypto(self) -> CryptoConfig:
        return CryptoConfig.model_validate(self._load("crypto.yaml"))

    @cached_property
    def themes(self) -> ThemesConfig:
        return ThemesConfig.model_validate(self._load("themes.yaml"))

    @cached_property
    def news(self) -> NewsConfig:
        return NewsConfig.model_validate(self._load("news.yaml"))

    @cached_property
    def universe(self) -> Universe:
        return Universe.model_validate(self._load("universe.yaml"))


class RuntimeFlags(BaseModel):
    """Laufzeit-Infos für /status."""

    started_at: str = ""
    last_runs: dict[str, str] = Field(default_factory=dict)
    last_errors: dict[str, str] = Field(default_factory=dict)
