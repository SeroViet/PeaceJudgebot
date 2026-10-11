"""CryptoAgent: nur Coins mit realer Nutzung (Gebühren), >= 3 Jahre Historie. Keine Memecoins."""

from __future__ import annotations

import json
from datetime import date

from alpha_scout.analysis import metrics as m
from alpha_scout.context import Context
from alpha_scout.models import AgentResult, Fact

NAME = "crypto"
CG = "CoinGecko"
DL = "DefiLlama"


def eligibility(
    symbol: str,
    *,
    allowlisted: bool,
    blocklisted: bool,
    history_years: float | None,
    fees_30d: float | None,
    min_years: float,
    min_fees: float,
) -> tuple[bool, list[str]]:
    reasons = []
    if blocklisted:
        return False, [f"{symbol} ist ein Memecoin – ausgeschlossen"]
    if allowlisted:
        return True, ["auf der Allowlist (BTC/ETH/SOL)"]
    if history_years is None or history_years < min_years:
        reasons.append(f"Historie < {min_years:.0f} Jahre")
    if fees_30d is None or fees_30d < min_fees:
        reasons.append("keine ausreichenden Gebühren-Einnahmen (reale Nutzung nicht belegt)")
    return not reasons, reasons or ["Kriterien erfüllt (nur beobachten, nie automatisch kaufen)"]


def crypto_score(drawdown_ath: float | None, fees_trend: float | None, eligible: bool) -> float:
    if not eligible:
        return 0.0
    score = 55.0
    if drawdown_ath is not None:
        score += m.interp(drawdown_ath, [(-80, 25), (-50, 20), (-30, 10), (-10, 0), (0, -5)])
    if fees_trend is not None:
        score += m.interp(fees_trend, [(-50, -15), (0, 0), (50, 10)])
    return round(m.clamp(score), 1)


class CryptoAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def analyze(self, symbol: str) -> AgentResult:
        symbol = symbol.upper()
        cfg = self.ctx.cfg.crypto
        res = AgentResult(agent=NAME, subject=symbol)
        asset = cfg.allowlist.get(symbol) or cfg.candidates.get(symbol)
        blocked = symbol in [b.upper() for b in cfg.memecoin_blocklist]
        if asset is None:
            ok, reasons = eligibility(
                symbol,
                allowlisted=False,
                blocklisted=blocked,
                history_years=None,
                fees_30d=None,
                min_years=cfg.min_history_years,
                min_fees=cfg.min_fees_30d_usd,
            )
            res.veto = True
            res.ko_reason = (
                "; ".join(reasons) if blocked else "Coin nicht in crypto.yaml erfasst – keine Empfehlung"
            )
            res.score = 0.0
            return res
        coin = await self.ctx.coingecko.coin(asset.coingecko)
        fees = await self.ctx.defillama.fees(asset.defillama_fees) if asset.defillama_fees else None
        today = date.today()
        if coin:
            d = date.fromisoformat(coin["as_of"])
            if coin.get("name"):
                res.facts.append(
                    Fact(id="profile.name", label="Name", value=coin["name"], source=CG, as_of=d)
                )
            for fid, label, key, unit in [
                ("price.last", "Kurs", "price_chf", "CHF"),
                ("price.usd", "Kurs in USD", "price_usd", "USD"),
                ("crypto.mcap_usd", "Marktkapitalisierung", "market_cap_usd", "USD"),
                ("crypto.ath_usd", "Allzeithoch", "ath_usd", "USD"),
                ("crypto.change_30d", "Veränderung 30 Tage", "change_30d_pct", "%"),
                ("crypto.change_1y", "Veränderung 1 Jahr", "change_1y_pct", "%"),
            ]:
                if coin.get(key) is not None:
                    res.facts.append(
                        Fact(id=fid, label=label, value=float(coin[key]), unit=unit, source=CG, as_of=d)
                    )
            dd = m.drawdown_pct(coin["price_usd"], coin["ath_usd"]) if coin.get("price_usd") else None
            high = await self.ctx.coingecko.high_365d(asset.coingecko)
            dd52 = m.drawdown_pct(coin["price_usd"], high) if coin.get("price_usd") and high else None
            if dd52 is not None:
                res.facts.append(
                    Fact(
                        id="price.drawdown_52w",
                        label="Abstand zum 52W-Hoch",
                        value=round(dd52, 1),
                        unit="%",
                        source=CG,
                        as_of=d,
                    )
                )
            if dd is not None:
                res.facts.append(
                    Fact(
                        id="crypto.drawdown_ath",
                        label="Abstand zum Allzeithoch",
                        value=round(dd, 1),
                        unit="%",
                        source=CG,
                        as_of=d,
                    )
                )
        else:
            dd = None
            res.findings.append("CoinGecko nicht erreichbar.")
        fees_trend = None
        if fees:
            d = date.fromisoformat(fees["as_of"])
            if fees.get("fees_30d") is not None:
                res.facts.append(
                    Fact(
                        id="crypto.fees_30d",
                        label="Gebühren-Einnahmen 30 Tage",
                        value=float(fees["fees_30d"]),
                        unit="USD",
                        source=DL,
                        as_of=d,
                    )
                )
            if fees.get("fees_1y") is not None:
                res.facts.append(
                    Fact(
                        id="crypto.fees_1y",
                        label="Gebühren-Einnahmen 1 Jahr",
                        value=float(fees["fees_1y"]),
                        unit="USD",
                        source=DL,
                        as_of=d,
                    )
                )
            if fees.get("fees_30d") and fees.get("fees_prev_30d"):
                fees_trend = (fees["fees_30d"] / fees["fees_prev_30d"] - 1) * 100
                res.facts.append(
                    Fact(
                        id="crypto.fees_trend",
                        label="Gebühren ggü. Vormonat",
                        value=round(fees_trend, 1),
                        unit="%",
                        source=DL,
                        as_of=d,
                    )
                )
        history_years = None
        if coin and coin.get("genesis_date"):
            history_years = (today - date.fromisoformat(coin["genesis_date"])).days / 365.25
        elif fees and fees.get("first_data"):
            history_years = (today - date.fromisoformat(fees["first_data"])).days / 365.25
        ok, reasons = eligibility(
            symbol,
            allowlisted=symbol in cfg.allowlist,
            blocklisted=blocked,
            history_years=history_years,
            fees_30d=fees.get("fees_30d") if fees else None,
            min_years=cfg.min_history_years,
            min_fees=cfg.min_fees_30d_usd,
        )
        res.facts.append(
            Fact(id="crypto.eligible", label="Erfüllt Krypto-Kriterien", value=ok, source="crypto.yaml")
        )
        res.findings.extend(reasons)
        if not ok:
            res.veto = True
            res.ko_reason = "; ".join(reasons)
        res.score = crypto_score(dd, fees_trend, ok)
        if symbol not in cfg.allowlist:
            res.score = min(res.score, 60.0)  # Kandidaten höchstens "beobachten"
        # Ereignisse aus dem NewsAgent (Regulierung, ETFs, Hacks, Unlocks)
        for k, r in enumerate((await self.ctx.db.recent_signals(symbol, days=14))[:5]):
            p = json.loads(r["payload"])
            res.facts.append(
                Fact(
                    id=f"news.{k}",
                    label=p.get("summary") or p.get("title", ""),
                    value=p.get("title"),
                    source=p.get("source", "News"),
                    as_of=date.fromisoformat(r["ts"][:10]),
                    url=p.get("url"),
                )
            )
        res.data = {"bucket": "rest", "drawdown_ath": dd}
        return res
