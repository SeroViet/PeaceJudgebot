"""SuperinvestorAgent: 13F-Meldungen der Top-Investoren – neue grosse Positionen und Kaufcluster."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from alpha_scout.config import SuperinvestorConfig
from alpha_scout.context import Context
from alpha_scout.data.sec_edgar import SOURCE as SEC
from alpha_scout.data.sec_edgar import Holding13F, normalize_name, parse_iso
from alpha_scout.models import AgentResult, Fact

log = logging.getLogger(__name__)

NAME = "superinvestor"


@dataclass
class PositionChange:
    investor: str
    cik: str
    issuer: str
    cusip: str
    ticker: str | None
    action: str  # new | increase
    weight_pct: float
    value_usd: float
    change_pct: float | None
    period: str
    filing_date: str


def diff_holdings(
    investor: str,
    cik: str,
    current: list[Holding13F],
    previous: list[Holding13F] | None,
    period: str,
    filing_date: str,
    cfg: SuperinvestorConfig,
) -> list[PositionChange]:
    """Neue grosse Positionen und starke Aufstockungen zwischen zwei Quartalen."""
    total = sum(h.value_usd for h in current) or 1.0
    prev = {h.cusip: h for h in (previous or [])}
    out = []
    for h in current:
        weight = h.value_usd / total * 100
        p = prev.get(h.cusip)
        if previous is not None and p is None:
            if weight >= cfg.big_new_position_pct:
                out.append(
                    PositionChange(
                        investor,
                        cik,
                        h.issuer,
                        h.cusip,
                        None,
                        "new",
                        weight,
                        h.value_usd,
                        None,
                        period,
                        filing_date,
                    )
                )
        elif p is not None and p.shares > 0:
            change = (h.shares / p.shares - 1) * 100
            if change >= cfg.big_increase_pct and weight >= cfg.big_increase_min_weight:
                out.append(
                    PositionChange(
                        investor,
                        cik,
                        h.issuer,
                        h.cusip,
                        None,
                        "increase",
                        weight,
                        h.value_usd,
                        change,
                        period,
                        filing_date,
                    )
                )
    return out


def find_clusters(changes: list[PositionChange], min_investors: int) -> dict[str, list[PositionChange]]:
    by_cusip: dict[str, list[PositionChange]] = defaultdict(list)
    for c in changes:
        by_cusip[c.cusip].append(c)
    return {k: v for k, v in by_cusip.items() if len({c.cik for c in v}) >= min_investors}


def holders_score(n_holders: int, recent_buyers: int) -> float | None:
    if n_holders == 0 and recent_buyers == 0:
        return None
    base = {0: 50, 1: 65, 2: 75}.get(n_holders, 85)
    return float(min(100, base + 10 * recent_buyers))


class SuperinvestorAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def cfg(self) -> SuperinvestorConfig:
        return self.ctx.cfg.superinvestors

    async def refresh(self) -> list[PositionChange]:
        """Liest neue 13F-Meldungen. Gibt nur Änderungen aus noch nicht gesehenen Meldungen zurück."""
        fresh: list[PositionChange] = []
        all_latest: list[PositionChange] = []
        for inv in self.cfg.investors:
            try:
                filings = await self.ctx.sec.latest_13f(inv.cik, n=2)
            except Exception as e:
                log.warning("13F für %s nicht lesbar: %s", inv.name, e)
                continue
            if not filings:
                continue
            for f in filings:
                await self._store(inv.cik, f["period"], f["holdings"])
            cur = filings[0]
            prev = filings[1]["holdings"] if len(filings) > 1 else None
            changes = diff_holdings(
                inv.name, inv.cik, cur["holdings"], prev, cur["period"], cur["filing_date"], self.cfg
            )
            for c in changes:
                c.ticker = await self.ctx.sec.ticker_for_name(c.issuer)
            all_latest.extend(changes)
            if await self.ctx.db.mark_seen(f"13f:{inv.cik}:{cur['accession']}"):
                fresh.extend(changes)
        for cusip, members in find_clusters(all_latest, self.cfg.cluster_min_investors).items():
            key = f"13f-cluster:{cusip}:{members[0].period}"
            if await self.ctx.db.mark_seen(key):
                for c in members:
                    if c not in fresh:
                        fresh.append(c)
        for c in fresh:
            subject = c.ticker or c.issuer
            await self.ctx.db.add_signal(
                NAME,
                subject,
                strength=min(100.0, 60 + c.weight_pct * 4),
                payload={
                    **c.__dict__,
                    "cluster": cusip_in_clusters(c, all_latest, self.cfg.cluster_min_investors),
                },
            )
        return fresh

    async def _store(self, cik: str, period: str, holdings: list[Holding13F]) -> None:
        row = await self.ctx.db.fetchone(
            "SELECT 1 FROM superinvestor_holdings WHERE cik = ? AND period = ? LIMIT 1", (cik, period)
        )
        if row is not None:
            return
        for h in holdings:
            ticker = await self.ctx.sec.ticker_for_name(h.issuer)
            await self.ctx.db.conn.execute(
                "INSERT OR REPLACE INTO superinvestor_holdings (cik, period, cusip, issuer, ticker, value_usd, shares) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cik, period, h.cusip, h.issuer, ticker, h.value_usd, h.shares),
            )
        await self.ctx.db.conn.commit()

    async def holders(self, ticker: str, company_name: str | None) -> AgentResult:
        """Welche Top-Investoren halten den Titel laut letztem 13F?"""
        res = AgentResult(agent=NAME, subject=ticker)
        names = {inv.cik: inv.name for inv in self.cfg.investors}
        rows = await self.ctx.db.fetchall(
            """SELECT h.cik, h.period, h.issuer, h.ticker, h.value_usd FROM superinvestor_holdings h
               JOIN (SELECT cik, MAX(period) AS p FROM superinvestor_holdings GROUP BY cik) l
               ON h.cik = l.cik AND h.period = l.p"""
        )
        norm = normalize_name(company_name) if company_name else None
        base = ticker.split(".")[0].upper()
        holders: list[tuple[str, str, float]] = []
        for r in rows:
            match = (r["ticker"] or "").upper() in (ticker.upper(), base) or (
                norm and normalize_name(r["issuer"]) == norm
            )
            if match and r["cik"] in names:
                holders.append((names[r["cik"]], r["period"], r["value_usd"]))
        holders.sort(key=lambda x: -x[2])
        signals = await self.ctx.db.recent_signals(ticker, days=120)
        recent_buyers = len({s["payload"] for s in signals if s["agent"] == NAME})
        res.data["holders"] = [h[0] for h in holders]
        if holders:
            res.facts.append(
                Fact(
                    id="si.holders",
                    label="Top-Investoren mit Position (letzter 13F)",
                    value=", ".join(h[0] for h in holders),
                    source=SEC + " 13F",
                    as_of=parse_iso(max(h[1] for h in holders)),
                )
            )
            res.facts.append(
                Fact(id="si.count", label="Anzahl Top-Investoren", value=len(holders), source=SEC + " 13F")
            )
            res.findings.append("Gehalten von: " + ", ".join(h[0] for h in holders[:5]))
        if recent_buyers:
            res.facts.append(
                Fact(
                    id="si.recent_buys",
                    label="Grosse Käufe von Top-Investoren (letzte 120 Tage)",
                    value=recent_buyers,
                    source=SEC + " 13F",
                )
            )
        res.score = holders_score(len(holders), recent_buyers)
        if not self.cfg.investors:
            res.findings.append("Keine Superinvestoren konfiguriert.")
        return res


def cusip_in_clusters(c: PositionChange, changes: list[PositionChange], min_investors: int) -> bool:
    return c.cusip in find_clusters(changes, min_investors)


def describe_change(c: PositionChange) -> str:
    if c.action == "new":
        return f"{c.investor}: NEUE Position {c.issuer} ({c.weight_pct:.1f} % des Portfolios, {c.value_usd / 1e6:,.0f} Mio. USD)"
    return f"{c.investor}: Aufstockung {c.issuer} um {c.change_pct:.0f} % (jetzt {c.weight_pct:.1f} % des Portfolios)"


def days_since(d: str) -> int:
    return (date.today() - parse_iso(d)).days
