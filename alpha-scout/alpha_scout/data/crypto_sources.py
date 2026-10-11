"""Krypto-Daten: CoinGecko (Preise, Historie) und DefiLlama (Gebühren-Einnahmen)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from alpha_scout.http import HttpClient, HttpError

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3"
DEFILLAMA_FEES = "https://api.llama.fi/summary/fees/{slug}"


class CoinGecko:
    def __init__(self, http: HttpClient):
        self.http = http

    async def coin(self, coin_id: str) -> dict[str, Any] | None:
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
        try:
            data = await self.http.get_json(f"{COINGECKO}/coins/{coin_id}", params=params, ttl=30 * 60)
        except HttpError as e:
            log.warning("CoinGecko %s: %s", coin_id, e)
            return None
        md = data.get("market_data", {})

        def usd(key: str) -> float | None:
            v = md.get(key, {})
            return v.get("usd") if isinstance(v, dict) else None

        return {
            "id": coin_id,
            "symbol": (data.get("symbol") or "").upper(),
            "name": data.get("name"),
            "price_usd": usd("current_price"),
            "price_chf": (md.get("current_price") or {}).get("chf"),
            "market_cap_usd": usd("market_cap"),
            "ath_usd": usd("ath"),
            "ath_date": (md.get("ath_date") or {}).get("usd"),
            "change_30d_pct": md.get("price_change_percentage_30d"),
            "change_1y_pct": md.get("price_change_percentage_1y"),
            "genesis_date": data.get("genesis_date"),
            "categories": data.get("categories") or [],
            "as_of": date.today().isoformat(),
        }

    async def high_365d(self, coin_id: str) -> float | None:
        """Höchster Tageskurs (USD) der letzten 365 Tage."""
        try:
            data = await self.http.get_json(
                f"{COINGECKO}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": "365", "interval": "daily"},
                ttl=12 * 3600,
            )
        except HttpError:
            return None
        prices = [p[1] for p in data.get("prices") or [] if p and p[1] is not None]
        return max(prices) if prices else None

    async def first_price_date(self, coin_id: str) -> date | None:
        """Ältestes verfügbares Kursdatum (Free-API: bis 365 Tage) – Fallback, wenn kein genesis_date."""
        try:
            data = await self.http.get_json(
                f"{COINGECKO}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": "365"},
                ttl=24 * 3600,
            )
        except HttpError:
            return None
        prices = data.get("prices") or []
        if not prices:
            return None
        return datetime.fromtimestamp(prices[0][0] / 1000, UTC).date()


class DefiLlama:
    def __init__(self, http: HttpClient):
        self.http = http

    async def fees(self, slug: str) -> dict[str, Any] | None:
        try:
            data = await self.http.get_json(
                DEFILLAMA_FEES.format(slug=slug), params={"dataType": "dailyFees"}, ttl=6 * 3600
            )
        except HttpError as e:
            log.warning("DefiLlama %s: %s", slug, e)
            return None
        chart = data.get("totalDataChart") or []
        first = datetime.fromtimestamp(chart[0][0], UTC).date().isoformat() if chart else None
        # 30 Tage vs. vorherige 30 Tage für den Trend
        last60 = [v for _, v in chart[-60:]]
        prev30 = sum(last60[:30]) if len(last60) >= 60 else None
        return {
            "fees_24h": data.get("total24h"),
            "fees_30d": data.get("total30d"),
            "fees_prev_30d": prev30,
            "fees_1y": data.get("total1y"),
            "first_data": first,
            "as_of": date.today().isoformat(),
        }
