"""Analyse-Pipeline je Titel: Daten -> Agenten -> (LLM live oder Batch) -> Score."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alpha_scout.agents import devils_advocate as devil
from alpha_scout.agents import quality as quality_mod
from alpha_scout.agents.crypto import CryptoAgent
from alpha_scout.agents.etf import ETFAgent
from alpha_scout.agents.fundamental import FundamentalAgent
from alpha_scout.agents.insider import InsiderAgent
from alpha_scout.agents.news import NewsAgent
from alpha_scout.agents.portfolio import PortfolioSnapshot, snapshot
from alpha_scout.agents.project_scout import ProjectScout
from alpha_scout.agents.risk import assess_risk
from alpha_scout.agents.superinvestor import SuperinvestorAgent
from alpha_scout.agents.valuation import valuate
from alpha_scout.context import Context
from alpha_scout.data.swissquote import check_swissquote
from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import DevilsVerdict, MoatAssessment
from alpha_scout.models import AgentResult, AssetKind, Fact, FactSheet, SwissquoteStatus
from alpha_scout.orchestrator import combine_scores

log = logging.getLogger(__name__)

MOAT_REUSE_DAYS = 30
DEVIL_REUSE_DAYS = 7
# Diese Fakten ändern sich mit dem Kurs; für die Wiederverwendung von LLM-Ergebnissen ignorieren
VOLATILE_PREFIXES = ("price.", "mcap.", "val.", "port.", "decision.", "risk.")


@dataclass
class Analysis:
    subject: str
    kind: AssetKind
    sheet: FactSheet
    results: dict[str, AgentResult] = field(default_factory=dict)
    quote: dict[str, Any] | None = None
    bucket: str = "qualitaet"
    swissquote: SwissquoteStatus = SwissquoteStatus.LIKELY
    swissquote_note: str = ""
    score: float | None = None
    vetoes: list[str] = field(default_factory=list)
    moat: MoatAssessment | None = None
    devil: DevilsVerdict | None = None
    pending_tasks: list[LLMTask] = field(default_factory=list)
    error: str | None = None
    facts_hash: str = ""  # Stand der Fakten nach prepare() (vor LLM/Quality/Risk)

    @property
    def name(self) -> str:
        return str(self.sheet.value("profile.name") or self.subject)

    @property
    def price(self) -> float | None:
        v = self.sheet.value("price.last")
        return float(v) if isinstance(v, int | float) else None

    @property
    def currency(self) -> str:
        f = self.sheet.get("price.last")
        return f.unit if f else ""

    def stable_hash(self) -> str:
        stable = {k: f.value for k, f in self.sheet.facts.items() if not k.startswith(VOLATILE_PREFIXES)}
        return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def agent_scores(self) -> dict[str, float | None]:
        return {k: r.score for k, r in self.results.items()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "name": self.name,
            "kind": self.kind.value,
            "bucket": self.bucket,
            "score": self.score,
            "vetoes": self.vetoes,
            "price": self.price,
            "currency": self.currency,
            "agent_scores": self.agent_scores(),
            "findings": {k: r.findings for k, r in self.results.items()},
            "risks": {k: r.risks for k, r in self.results.items()},
            "valuation": self.results["valuation"].data if "valuation" in self.results else {},
            "quality": self.results["quality"].data if "quality" in self.results else {},
            "moat": self.moat.model_dump() if self.moat else None,
            "devil": self.devil.model_dump() if self.devil else None,
            "swissquote": [self.swissquote.value, self.swissquote_note],
        }


def detect_kind(subject: str, ctx: Context, quote: dict | None) -> AssetKind:
    if subject.upper() in ctx.cfg.crypto.allowlist or subject.upper() in ctx.cfg.crypto.candidates:
        return AssetKind.CRYPTO
    if subject.upper() in [b.upper() for b in ctx.cfg.crypto.memecoin_blocklist]:
        return AssetKind.CRYPTO
    qt = (quote or {}).get("quote_type") or ""
    if qt.upper() in ("ETF", "MUTUALFUND"):
        return AssetKind.ETF
    return AssetKind.STOCK


def stock_bucket(ticker: str, ctx: Context) -> str:
    pos = next((p for p in ctx.cfg.holdings.positions if p.ticker == ticker), None)
    if pos:
        return pos.bucket
    group = ctx.cfg.watchlist.group_of(ticker)
    return "rest" if group in ("ruestung", "beobachten") else "qualitaet"


class Pipeline:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.fundamental = FundamentalAgent(ctx)
        self.insider = InsiderAgent(ctx)
        self.superinvestor = SuperinvestorAgent(ctx)
        self.news = NewsAgent(ctx)
        self.projects = ProjectScout(ctx)
        self.etf = ETFAgent(ctx)
        self.crypto = CryptoAgent(ctx)
        self._snap: PortfolioSnapshot | None = None

    async def portfolio(self, refresh: bool = False) -> PortfolioSnapshot:
        if self._snap is None or refresh:
            self._snap = await snapshot(
                self.ctx.cfg.holdings,
                self.ctx.market,
                self.ctx.settings.base_currency,
                crypto_prices=await self.crypto_prices(),
            )
        return self._snap

    async def crypto_prices(self) -> dict[str, float]:
        out = {}
        for p in self.ctx.cfg.holdings.positions:
            asset = self.ctx.cfg.crypto.allowlist.get(p.ticker.upper())
            if asset:
                coin = await self.ctx.coingecko.coin(asset.coingecko)
                if coin and coin.get("price_chf"):
                    out[p.ticker.upper()] = float(coin["price_chf"])
        return out

    def _port_facts(self, a: Analysis) -> None:
        pos = next((p for p in self.ctx.cfg.holdings.positions if p.ticker == a.subject), None)
        if pos:
            a.sheet.add(Fact(id="port.held", label="Im Depot", value=True, source="holdings.yaml"))
            a.sheet.add(
                Fact(
                    id="port.buy_price",
                    label="Kaufkurs",
                    value=pos.buy_price,
                    unit=pos.currency,
                    source="holdings.yaml",
                    as_of=pos.buy_date,
                )
            )
            a.sheet.add(
                Fact(
                    id="port.buy_date",
                    label="Kaufdatum",
                    value=pos.buy_date.isoformat(),
                    source="holdings.yaml",
                )
            )
            if pos.thesis:
                a.sheet.add(
                    Fact(
                        id="port.thesis",
                        label="Ursprüngliche Kaufthese",
                        value=pos.thesis,
                        source="holdings.yaml",
                    )
                )
        group = self.ctx.cfg.watchlist.group_of(a.subject)
        if group:
            a.sheet.add(Fact(id="port.watch", label="Auf der Watchlist", value=True, source="watchlist.yaml"))
            a.sheet.add(
                Fact(id="port.watch_group", label="Watchlist-Gruppe", value=group, source="watchlist.yaml")
            )
        stop = self.ctx.cfg.watchlist.buy_stop_until(a.subject)
        if stop and self.ctx.cfg.watchlist.is_buy_stopped(a.subject, self.ctx.today()):
            a.sheet.add(
                Fact(
                    id="port.buy_stop", label="Kaufstopp (Watchlist)", value=stop[1], source="watchlist.yaml"
                )
            )

    # ------------------------------------------------------------ Vorbereitung (ohne LLM)
    async def prepare(self, subject: str, allow_stale_llm: bool = False) -> Analysis:
        subject = self.ctx.cfg.watchlist.resolve(subject.strip()).upper()
        if (
            subject.upper() in self.ctx.cfg.crypto.allowlist
            or subject.upper() in [b.upper() for b in self.ctx.cfg.crypto.memecoin_blocklist]
            or subject.upper() in self.ctx.cfg.crypto.candidates
        ):
            return await self._prepare_crypto(subject.upper())
        fund = await self.fundamental.run(subject)
        quote = fund.data.get("quote")
        kind = detect_kind(subject, self.ctx, quote)
        a = Analysis(subject=subject, kind=kind, sheet=FactSheet(subject=subject), quote=quote)
        a.sheet.extend(fund.facts)
        if quote is None:
            a.error = fund.findings[0] if fund.findings else "Keine Daten"
            return a
        a.swissquote, a.swissquote_note = check_swissquote(
            subject, kind, self.ctx.cfg.swissquote, quote.get("exchange")
        )
        self._port_facts(a)
        if kind == AssetKind.ETF:
            etf = await self.etf.analyze(subject)
            a.results["etf"] = etf
            a.sheet.extend(etf.facts)
            a.bucket = etf.data.get("bucket", "rest")
            return a
        a.results["fundamental"] = fund
        a.bucket = stock_bucket(subject, self.ctx)
        ins, holders, news, proj = await asyncio.gather(
            self.insider.run(subject),
            self.superinvestor.holders(subject, quote.get("name")),
            self.news.facts_for(subject),
            self.projects.facts_for(subject),
        )
        for r in (ins, holders, news, proj):
            a.results[r.agent] = r
            a.sheet.extend(r.facts)
        for k, txt in enumerate(await self.news.fraud_news(subject)):
            a.sheet.add(
                Fact(
                    id=f"risk.fraud_news{k}",
                    label="Pressebericht über Untersuchung/Vorwurf",
                    value=txt,
                    source="News",
                )
            )
        mx = fund.data.get("metrics") or {}
        fccy = fund.data.get("financial_currency") or a.currency
        fx = await self.ctx.market.fx(fccy, a.currency) if fccy and a.currency else None
        val = valuate(
            subject,
            price=a.price or 0.0,
            currency=a.currency,
            market_cap=quote.get("market_cap"),
            fcf=mx.get("fcf_last"),
            fx_fin_to_quote=fx,
            revenue_cagr=mx.get("revenue_cagr_3y"),
            cfg=self.ctx.cfg.scoring.valuation,
        )
        a.results["valuation"] = val
        a.sheet.extend(val.facts)
        q = quality_mod.check_quality(
            a.sheet, holders.data.get("holders", []), await self.news.fraud_flags(subject)
        )
        a.results["quality_pre"] = AgentResult(agent="quality_pre", subject=subject, data={"check": q})
        # LLM-Ergebnisse wiederverwenden, wenn sich die Fakten nicht geändert haben
        a.facts_hash = a.stable_hash()
        prev = await self.ctx.db.get_analysis(subject)
        reuse_moat = False
        if prev and (allow_stale_llm or prev["facts_hash"] == a.facts_hash):
            age = (self.ctx.now() - datetime.fromisoformat(prev["ts"]).astimezone(self.ctx.tz)).days
            p = prev["payload"]
            if p.get("moat") and age <= MOAT_REUSE_DAYS:
                a.moat, reuse_moat = MoatAssessment.model_validate(p["moat"]), True
            if p.get("devil") and age <= DEVIL_REUSE_DAYS:
                a.devil = DevilsVerdict.model_validate(p["devil"])
        if not q.exclusions and not reuse_moat:
            a.pending_tasks.append(quality_mod.moat_task(a.sheet, subject))
        return a

    async def _prepare_crypto(self, symbol: str) -> Analysis:
        res = await self.crypto.analyze(symbol)
        a = Analysis(subject=symbol, kind=AssetKind.CRYPTO, sheet=FactSheet(subject=symbol), bucket="rest")
        a.sheet.extend(res.facts)
        a.results["crypto"] = res
        a.swissquote, a.swissquote_note = check_swissquote(symbol, AssetKind.CRYPTO, self.ctx.cfg.swissquote)
        self._port_facts(a)
        return a

    def devil_needed(self, a: Analysis) -> bool:
        """Gegenprüfung nur für echte Kandidaten oder Bestand (spart Kosten)."""
        if a.kind != AssetKind.STOCK or a.devil is not None:
            return False
        q = a.results["quality_pre"].data["check"]
        held = bool(a.sheet.value("port.held"))
        if q.exclusions:
            return held
        # Kandidat, wenn ohne Burggraben schon >= 3 Kriterien erfüllt (mit Burggraben dann >= 4)
        return held or q.passed_count >= 3

    # ------------------------------------------------------------ Abschluss
    async def finalize(self, a: Analysis, outputs: dict[str, Any] | None = None) -> Analysis:
        outputs = outputs or {}
        if a.error:
            return a
        known = set(a.sheet.facts)
        if a.kind == AssetKind.STOCK:
            q = a.results.pop("quality_pre").data["check"]
            moat = outputs.get(f"moat-{quality_mod._cid(a.subject)}", a.moat)
            if isinstance(moat, MoatAssessment):
                a.moat = moat
            evidence, concerns = quality_mod.apply_moat(q, a.moat, known)
            qres = quality_mod.to_result(a.subject, q, evidence, concerns)
            a.results["quality"] = qres
            a.sheet.extend(qres.facts)
            dv = outputs.get(f"devil-{quality_mod._cid(a.subject)}", a.devil)
            if isinstance(dv, DevilsVerdict):
                a.devil = dv
            if a.devil is not None:
                a.results["devils_advocate"] = devil.interpret(a.subject, a.devil, known)
        snap = await self.portfolio()
        risk = assess_risk(
            a.subject,
            a.sheet,
            bucket=a.bucket,
            snap=snap,
            rules=self.ctx.cfg.scoring.portfolio,
            name=a.name if a.kind == AssetKind.ETF else None,
        )
        a.results["risk"] = risk
        a.sheet.extend(risk.facts)
        factors = await agent_factors(self.ctx)
        a.score, a.vetoes = combine_scores(a.results, self.ctx.cfg.scoring, factors, a.kind)
        a.sheet.add(
            Fact(id="decision.score", label="Gesamtscore (0–100)", value=a.score, source="Orchestrator")
        )
        await self.ctx.db.save_analysis(a.subject, a.facts_hash or a.stable_hash(), a.score, a.to_payload())
        return a

    # ------------------------------------------------------------ Live (z. B. /check)
    async def analyze_live(self, subject: str) -> Analysis:
        a = await self.prepare(subject)
        if a.error:
            return a
        tasks = list(a.pending_tasks)
        outputs: dict[str, Any] = {}
        if tasks:
            results = await asyncio.gather(*(self.ctx.llm.run(t) for t in tasks))
            outputs = {t.custom_id: r for t, r in zip(tasks, results, strict=True)}
        if a.kind == AssetKind.STOCK:
            # Burggraben zuerst einarbeiten, dann Gegenprüfung mit vollständigem Bild
            m = outputs.get(f"moat-{quality_mod._cid(a.subject)}")
            if isinstance(m, MoatAssessment):
                a.moat = m
            if self.devil_needed(a):
                outputs[f"devil-{quality_mod._cid(a.subject)}"] = await self.ctx.llm.run(
                    devil.devil_task(a.sheet, a.subject)
                )
        return await self.finalize(a, outputs)


async def agent_factors(ctx: Context) -> dict[str, float]:
    rows = await ctx.db.fetchall("SELECT agent, factor FROM agent_weights")
    return {r["agent"]: float(r["factor"]) for r in rows}
