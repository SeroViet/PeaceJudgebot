"""InsiderAgent: Insider-Käufe (SEC Form 4). >= 2 Käufer in 30 Tagen = starkes Signal."""

from __future__ import annotations

from alpha_scout.context import Context
from alpha_scout.data.sec_edgar import SOURCE as SEC
from alpha_scout.data.sec_edgar import InsiderTrade, parse_iso
from alpha_scout.models import AgentResult, Fact

NAME = "insider"
MIN_BUY_USD = 10_000  # Kleinstkäufe ignorieren


def analyze_trades(subject: str, trades: list[InsiderTrade] | None) -> AgentResult:
    res = AgentResult(agent=NAME, subject=subject)
    if trades is None:
        res.findings.append("Insider-Daten nur für US-Firmen (SEC Form 4) verfügbar.")
        return res
    buys = [t for t in trades if t.code == "P" and (t.value or 0) >= MIN_BUY_USD]
    sells = [t for t in trades if t.code == "S"]
    buyers = sorted({t.owner for t in buys})
    buy_value = sum(t.value or 0 for t in buys)
    sell_value = sum(t.value or 0 for t in sells)
    res.facts.append(
        Fact(
            id="ins.buyers_30d",
            label="Insider mit Käufen (30 Tage)",
            value=len(buyers),
            source=SEC + " Form 4",
        )
    )
    res.facts.append(
        Fact(
            id="ins.buy_value_30d",
            label="Insider-Käufe (30 Tage)",
            value=round(buy_value),
            unit="USD",
            source=SEC + " Form 4",
        )
    )
    res.facts.append(
        Fact(
            id="ins.sell_value_30d",
            label="Insider-Verkäufe (30 Tage)",
            value=round(sell_value),
            unit="USD",
            source=SEC + " Form 4",
        )
    )
    if buys:
        latest = max(buys, key=lambda t: t.trade_date)
        res.facts.append(
            Fact(
                id="ins.latest_buy",
                label="Letzter Insider-Kauf",
                value=f"{latest.owner} ({latest.title or 'Insider'}), {latest.shares:,.0f} Aktien",
                source=SEC + " Form 4",
                as_of=parse_iso(latest.trade_date),
                url=latest.url,
            )
        )
    res.data = {
        "buyers": buyers,
        "buy_value": buy_value,
        "sell_value": sell_value,
        "urls": [t.url for t in buys][:3],
    }
    if len(buyers) >= 2:
        res.score = 85.0
        res.findings.append(f"Starkes Signal: {len(buyers)} Insider haben gekauft ({', '.join(buyers[:3])}).")
    elif len(buyers) == 1:
        res.score = 65.0
        res.findings.append(f"1 Insider-Kauf ({buyers[0]}).")
    elif sell_value > 0:
        res.findings.append("Nur Insider-Verkäufe (oft planmässig, z. B. 10b5-1) – schwaches Signal.")
    return res


class InsiderAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def run(self, ticker: str) -> AgentResult:
        trades = await self.ctx.sec.insider_trades(ticker, days=30)
        res = analyze_trades(ticker, trades)
        if res.score and res.score >= 85:
            key = f"insider:{ticker}:{'-'.join(res.data['buyers'][:3])}"
            if await self.ctx.db.mark_seen(key):
                await self.ctx.db.add_signal(NAME, ticker, res.score, res.data)
        return res
