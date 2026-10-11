"""ETFAgent: beste ETFs/Fonds je Kategorie – Kosten (TER), Grösse, Domizil (CH/IE)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from alpha_scout.analysis import metrics as m
from alpha_scout.context import Context
from alpha_scout.data.market import SOURCE as YF
from alpha_scout.data.market import as_date
from alpha_scout.models import AgentResult, Fact

NAME = "etf"
CORE_CATEGORIES = {"msci_world", "schweiz", "usa", "schwellenlaender"}


@dataclass
class EtfInfo:
    ticker: str
    category: str | None
    name: str | None
    currency: str | None
    price: float | None
    ter_pct: float | None
    aum: float | None
    domicile: str | None
    return_1y: float | None
    high_52w: float | None


def domicile_from_name(name: str | None) -> str | None:
    if not name:
        return None
    m_ = re.search(r"\((CH|IE|LU)\)", name)
    return m_.group(1) if m_ else None


def etf_score(e: EtfInfo) -> float:
    parts = []
    if e.ter_pct is not None:
        parts.append(m.interp(e.ter_pct, [(0.05, 100), (0.2, 80), (0.4, 55), (0.8, 20)]))
    if e.aum is not None:
        parts.append(m.interp(e.aum / 1e9, [(0.1, 20), (0.5, 50), (1, 70), (5, 95)]))
    dom = {"CH": 100, "IE": 95, "LU": 75}.get(e.domicile or "", 50)
    parts.append(dom)
    return round(sum(parts) / len(parts), 1)


def rank(etfs: list[EtfInfo]) -> list[tuple[EtfInfo, float]]:
    return sorted(((e, etf_score(e)) for e in etfs), key=lambda x: -x[1])


class ETFAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def category_of(self, ticker: str) -> tuple[str | None, str | None]:
        for cat, c in self.ctx.cfg.etfs.categories.items():
            for cand in c.candidates:
                if cand.ticker.upper() == ticker.upper():
                    return cat, cand.domicile
        return None, None

    async def info(self, ticker: str) -> EtfInfo | None:
        q = await self.ctx.market.quote(ticker)
        if not q:
            return None
        cat, dom = self.category_of(ticker)
        hist = await self.ctx.market.history(ticker, "1y")
        ret = None
        if hist and len(hist) > 200:
            ret = (hist[-1][1] / hist[0][1] - 1) * 100
        return EtfInfo(
            ticker=ticker,
            category=cat,
            name=q.get("name"),
            currency=q.get("currency"),
            price=q.get("price"),
            ter_pct=q.get("ter"),
            aum=q.get("total_assets"),
            domicile=dom or domicile_from_name(q.get("name")),
            return_1y=ret,
            high_52w=q.get("high_52w"),
        )

    async def ranking(self, category: str) -> list[tuple[EtfInfo, float]]:
        cat = self.ctx.cfg.etfs.categories.get(category)
        if not cat:
            return []
        infos = [i for i in [await self.info(c.ticker) for c in cat.candidates] if i is not None]
        return rank(infos)

    async def analyze(self, ticker: str) -> AgentResult:
        res = AgentResult(agent=NAME, subject=ticker)
        e = await self.info(ticker)
        if e is None:
            res.findings.append("Keine ETF-Daten gefunden.")
            return res
        q = await self.ctx.market.quote(ticker) or {}
        d = as_date(q.get("as_of"))

        def add(fid: str, label: str, value, unit: str = "") -> None:
            if value is not None:
                res.facts.append(
                    Fact(
                        id=fid,
                        label=label,
                        value=round(value, 3) if isinstance(value, float) else value,
                        unit=unit,
                        source=YF,
                        as_of=d,
                    )
                )

        add("profile.name", "Name", e.name)
        add("price.last", "Kurs", e.price, e.currency or "")
        add("etf.ter", "Laufende Kosten (TER)", e.ter_pct, "%")
        add("etf.aum", "Fondsvolumen", e.aum, "USD")
        res.facts.append(
            Fact(
                id="etf.domicile",
                label="Domizil",
                value=e.domicile or "unbekannt – bitte prüfen",
                source="etf_universe.yaml / Fondsname",
            )
        )
        add("etf.return_1y", "Rendite 1 Jahr", e.return_1y, "%")
        if e.price and e.high_52w:
            add("price.high_52w", "52-Wochen-Hoch", e.high_52w, e.currency or "")
            add("price.drawdown_52w", "Abstand zum 52W-Hoch", m.drawdown_pct(e.price, e.high_52w), "%")
        res.score = etf_score(e)
        if e.category:
            ranking = await self.ranking(e.category)
            pos = next((i for i, (x, _) in enumerate(ranking) if x.ticker == ticker), None)
            if pos is not None:
                res.facts.append(
                    Fact(
                        id="etf.rank",
                        label=f"Rang in Kategorie {e.category}",
                        value=f"{pos + 1} von {len(ranking)}",
                        source="Alpha Scout (TER, Grösse, Domizil)",
                    )
                )
                best = ranking[0][0]
                if best.ticker != ticker:
                    res.findings.append(
                        f"Günstigere/bessere Alternative: {best.ticker} ({best.name}, TER {best.ter_pct} %)"
                    )
        if e.domicile not in ("CH", "IE"):
            res.risks.append("Domizil nicht CH/IE bestätigt – Quellensteuer/Domizil bei Swissquote prüfen")
        res.data = {"category": e.category, "bucket": "kern" if e.category in CORE_CATEGORIES else "rest"}
        return res
