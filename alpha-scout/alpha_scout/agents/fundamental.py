"""FundamentalAgent: Quartals-/Jahreszahlen, Wachstum, Marge, Schulden, Cashflow, Verwässerung, Auftragsbestand."""

from __future__ import annotations

import logging
from typing import Any

from alpha_scout.analysis import metrics as m
from alpha_scout.context import Context
from alpha_scout.data.market import SOURCE as YF
from alpha_scout.data.market import as_date
from alpha_scout.data.sec_edgar import SOURCE as SEC
from alpha_scout.models import AgentResult, Fact

log = logging.getLogger(__name__)

NAME = "fundamental"


def compute_metrics(fin: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Alle Kennzahlen aus yfinance-Rohdaten (Finanzwährung)."""
    rev = fin.get("revenue", [])
    rev_cagr, rev_years = m.series_cagr(rev, 3)
    rev_last = m.last(rev)
    ni_last = m.last(fin.get("net_income", []))
    ni_ttm = m.ttm(fin.get("q_net_income", []))
    rev_ttm = m.ttm(fin.get("q_revenue", []))
    op_last = m.last(fin.get("operating_income", []))
    ebitda = m.last(fin.get("ebitda", []))
    fcf = m.last(fin.get("fcf", []))
    nd = m.net_debt(m.last(fin.get("total_debt", [])), m.last(fin.get("cash", [])))
    shares_series = fin.get("shares") or fin.get("diluted_shares") or []
    return {
        "revenue_last": rev_last,
        "revenue_cagr_3y": rev_cagr,
        "revenue_years": rev_years,
        "revenue_ttm": rev_ttm,
        "net_income_last": ni_last,
        "net_income_ttm": ni_ttm,
        "operating_margin": m.margin(op_last, rev_last),
        "gross_margin": quote.get("gross_margin") * 100 if quote.get("gross_margin") is not None else None,
        "ebitda_last": ebitda,
        "fcf_last": fcf,
        "fcf_margin": m.margin(fcf, rev_last),
        "net_debt": nd,
        "net_debt_ebitda": m.net_debt_to_ebitda(nd, ebitda),
        "dilution_3y": m.dilution_per_year(shares_series, 3),
        "forward_eps": quote.get("forward_eps"),
        "roe": quote.get("roe") * 100 if quote.get("roe") is not None else None,
        "fiscal_date": rev[-1][0] if rev else None,
    }


def fundamental_score(mx: dict[str, Any]) -> float | None:
    parts: list[float] = []
    if mx["revenue_cagr_3y"] is not None:
        parts.append(m.interp(mx["revenue_cagr_3y"], [(-10, 10), (0, 40), (10, 70), (20, 100)]))
    if mx["operating_margin"] is not None:
        parts.append(m.interp(mx["operating_margin"], [(-10, 10), (0, 40), (15, 70), (30, 100)]))
    if mx["fcf_margin"] is not None:
        parts.append(m.interp(mx["fcf_margin"], [(-10, 10), (0, 40), (10, 70), (25, 100)]))
    if mx["net_debt"] is not None:
        if mx["net_debt"] <= 0:
            parts.append(100)
        elif mx["net_debt_ebitda"] is not None:
            parts.append(m.interp(mx["net_debt_ebitda"], [(0, 95), (1, 85), (3, 55), (5, 15)]))
        else:
            parts.append(15)  # Schulden bei negativem EBITDA
    if mx["dilution_3y"] is not None:
        parts.append(m.interp(mx["dilution_3y"], [(-2, 100), (0, 90), (2, 70), (5, 40), (10, 5)]))
    if not parts:
        return None
    return round(sum(parts) / len(parts), 1)


class FundamentalAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def run(self, ticker: str) -> AgentResult:
        res = AgentResult(agent=NAME, subject=ticker)
        quote = await self.ctx.market.quote(ticker)
        if not quote or quote.get("price") is None:
            res.findings.append("Keine Kursdaten gefunden – Ticker prüfen (Yahoo-Format, z. B. NESN.SW).")
            return res
        res.data["quote"] = quote
        q_date = as_date(quote.get("as_of"))
        ccy = quote.get("currency") or ""
        f = res.facts
        f.append(Fact(id="price.last", label="Kurs", value=quote["price"], unit=ccy, source=YF, as_of=q_date))
        f.append(Fact(id="profile.name", label="Name", value=quote.get("name"), source=YF, as_of=q_date))
        for key, label in [
            ("sector", "Sektor"),
            ("industry", "Branche"),
            ("country", "Land"),
            ("exchange_name", "Börse"),
        ]:
            if quote.get(key):
                f.append(Fact(id=f"profile.{key}", label=label, value=quote[key], source=YF, as_of=q_date))
        if quote.get("summary"):
            f.append(
                Fact(
                    id="profile.summary",
                    label="Firmenbeschreibung",
                    value=quote["summary"][:1500],
                    source=YF,
                    as_of=q_date,
                )
            )
        if quote.get("market_cap"):
            f.append(
                Fact(
                    id="mcap.value",
                    label="Marktkapitalisierung",
                    value=float(quote["market_cap"]),
                    unit=ccy,
                    source=YF,
                    as_of=q_date,
                )
            )
        usd = await self.ctx.market.fx(ccy, "USD") if ccy else None
        if usd:
            f.append(
                Fact(
                    id="price.usd",
                    label="Kurs in USD",
                    value=round(quote["price"] * usd, 2),
                    unit="USD",
                    source=YF,
                    as_of=q_date,
                )
            )
            if quote.get("market_cap"):
                f.append(
                    Fact(
                        id="mcap.usd",
                        label="Marktkap. in USD",
                        value=float(quote["market_cap"]) * usd,
                        unit="USD",
                        source=YF,
                        as_of=q_date,
                    )
                )
        if quote.get("high_52w"):
            f.append(
                Fact(
                    id="price.high_52w",
                    label="52-Wochen-Hoch",
                    value=quote["high_52w"],
                    unit=ccy,
                    source=YF,
                    as_of=q_date,
                )
            )
            dd = m.drawdown_pct(quote["price"], quote["high_52w"])
            if dd is not None:
                f.append(
                    Fact(
                        id="price.drawdown_52w",
                        label="Abstand zum 52W-Hoch",
                        value=round(dd, 1),
                        unit="%",
                        source=YF,
                        as_of=q_date,
                    )
                )

        if (quote.get("quote_type") or "").upper() != "EQUITY":
            res.data["kind"] = (quote.get("quote_type") or "").lower()
            return res

        fin = await self.ctx.market.financials(ticker)
        if not fin:
            res.findings.append("Keine Fundamentaldaten verfügbar.")
            return res
        fccy = quote.get("financial_currency") or ccy
        mx = compute_metrics(fin, quote)
        res.data["metrics"] = mx
        res.data["financial_currency"] = fccy
        fdate = as_date(mx["fiscal_date"])

        def add(fid: str, label: str, value: Any, unit: str = "", as_of: Any = fdate, src: str = YF) -> None:
            if value is not None:
                f.append(
                    Fact(
                        id=fid,
                        label=label,
                        value=round(value, 2) if isinstance(value, float) else value,
                        unit=unit,
                        source=src,
                        as_of=as_of,
                    )
                )

        add("fund.revenue_last", "Umsatz letztes Geschäftsjahr", mx["revenue_last"], fccy)
        add("fund.revenue_ttm", "Umsatz letzte 4 Quartale", mx["revenue_ttm"], fccy)
        if mx["revenue_cagr_3y"] is not None:
            add(
                "fund.revenue_cagr_3y",
                f"Umsatzwachstum p. a. ({mx['revenue_years']} J.)",
                mx["revenue_cagr_3y"],
                "%",
            )
        add("fund.net_income_last", "Reingewinn letztes GJ", mx["net_income_last"], fccy)
        add("fund.net_income_ttm", "Reingewinn letzte 4 Quartale", mx["net_income_ttm"], fccy)
        add("fund.operating_margin", "Operative Marge", mx["operating_margin"], "%")
        add("fund.gross_margin", "Bruttomarge", mx["gross_margin"], "%", as_of=q_date)
        add("fund.fcf_last", "Freier Cashflow letztes GJ", mx["fcf_last"], fccy)
        add("fund.fcf_margin", "FCF-Marge", mx["fcf_margin"], "%")
        add("fund.ebitda_last", "EBITDA letztes GJ", mx["ebitda_last"], fccy)
        add("fund.net_debt", "Nettoverschuldung (negativ = Netto-Cash)", mx["net_debt"], fccy)
        add("fund.net_debt_ebitda", "Nettoverschuldung / EBITDA", mx["net_debt_ebitda"], "x")
        add("fund.dilution_3y", "Veränderung Aktienanzahl p. a.", mx["dilution_3y"], "%")
        add(
            "fund.forward_eps",
            "Gewinn je Aktie (Analystenschätzung nächstes Jahr)",
            mx["forward_eps"],
            ccy,
            as_of=q_date,
        )
        add("fund.roe", "Eigenkapitalrendite", mx["roe"], "%", as_of=q_date)

        await self._backlog(ticker, res)
        res.score = fundamental_score(mx)
        return res

    async def _backlog(self, ticker: str, res: AgentResult) -> None:
        """Auftragsbestand (Remaining Performance Obligation) aus SEC-XBRL, nur US-Firmen."""
        try:
            cik = await self.ctx.sec.cik_for(ticker)
            if cik is None:
                return
            facts = await self.ctx.sec.company_facts(cik)
            if not facts:
                return
            rpo = self.ctx.sec.latest_fact(facts, "RevenueRemainingPerformanceObligation")
            if rpo:
                val, end, form = rpo
                res.facts.append(
                    Fact(
                        id="fund.backlog",
                        label=f"Auftragsbestand (RPO, {form})",
                        value=val,
                        unit="USD",
                        source=SEC,
                        as_of=as_date(end),
                    )
                )
        except Exception as e:  # Auftragsbestand ist optional
            log.info("Kein Auftragsbestand für %s: %s", ticker, e)
