"""NewsAgent: weltweite Finanznews (GDELT), Notenbanken (RSS), Filter per Haiku."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alpha_scout.context import Context
from alpha_scout.data.news_sources import NewsItem, build_or_query
from alpha_scout.data.sec_edgar import normalize_name
from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import NewsVerdicts
from alpha_scout.models import AgentResult, Fact, FactSheet

log = logging.getLogger(__name__)

NAME = "news"
CHUNK = 40
CRYPTO_TERMS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


@dataclass
class RelevantNews:
    item: NewsItem
    importance: int
    category: str
    tickers: list[str]
    thesis_risk: bool
    summary: str
    fraud: bool = False


def search_term(name: str | None) -> str | None:
    """Kurzer Suchbegriff aus dem Firmennamen (max. 2 Wörter)."""
    if not name:
        return None
    words = normalize_name(name).split()
    if not words:
        return None
    term = " ".join(words[:2]) if len(words[0]) < 6 and len(words) > 1 else words[0]
    return term.lower() if len(term) >= 3 else None


def has_fraud_keyword(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


class NewsAgent:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def watch_terms(self) -> dict[str, str]:
        """Suchbegriff -> Ticker für Bestand + Watchlist."""
        wl = self.ctx.cfg.watchlist
        tickers = list(
            dict.fromkeys([p.ticker for p in self.ctx.cfg.holdings.positions] + wl.all_securities())
        )
        terms: dict[str, str] = {}
        for t in tickers:
            if t.upper() in CRYPTO_TERMS:
                continue
            q = await self.ctx.market.quote(t)
            if q and (q.get("quote_type") or "").upper() == "EQUITY":
                term = search_term(q.get("name"))
                if term:
                    terms[term] = t
        for c in wl.krypto:
            if c.upper() in CRYPTO_TERMS:
                terms[CRYPTO_TERMS[c.upper()]] = c.upper()
        return terms

    async def collect(self) -> list[NewsItem]:
        terms = await self.watch_terms()
        queries = build_or_query(list(terms)) + list(self.ctx.cfg.news.gdelt_macro)
        items: list[NewsItem] = []
        for q in queries:
            items.extend(await self.ctx.gdelt.search(q, timespan="6h", max_records=50))
        for feed in self.ctx.cfg.news.rss:
            items.extend(await self.ctx.rss.fetch(feed.name, feed.url))
        cutoff = datetime.now(UTC) - timedelta(days=2)
        fresh: list[NewsItem] = []
        seen_titles: set[str] = set()
        for it in items:
            if it.published and it.published < cutoff:
                continue
            tkey = it.title.lower()[:90]
            if tkey in seen_titles:
                continue
            seen_titles.add(tkey)
            if await self.ctx.db.mark_seen(it.key):
                fresh.append(it)
        return fresh

    def _sheet(self, chunk: list[NewsItem], watch: dict[str, str]) -> FactSheet:
        sheet = FactSheet(subject="Nachrichten")
        for i, it in enumerate(chunk):
            sheet.add(
                Fact(
                    id=f"news.{i}",
                    label=it.title,
                    value=it.source,
                    source=it.source,
                    as_of=it.published.date() if it.published else None,
                )
            )
        for p in self.ctx.cfg.holdings.positions:
            sheet.add(
                Fact(
                    id=f"port.{p.ticker}",
                    label=f"Position {p.ticker}",
                    value=p.thesis or "keine These hinterlegt",
                    source="holdings.yaml",
                )
            )
        return sheet

    async def classify(self, items: list[NewsItem]) -> list[RelevantNews]:
        terms = await self.watch_terms()
        watch_tickers = sorted(set(terms.values()))
        out: list[RelevantNews] = []
        min_imp = self.ctx.cfg.news.min_importance
        fraud_kw = self.ctx.cfg.news.fraud_keywords
        for start in range(0, len(items), CHUNK):
            chunk = items[start : start + CHUNK]
            sheet = self._sheet(chunk, terms)
            facts_json = json.dumps(
                {**json.loads(sheet.to_prompt_json()), "watch_tickers": watch_tickers},
                ensure_ascii=False,
                sort_keys=True,
            )
            task = LLMTask(
                custom_id=f"news-{start}",
                kind="news",
                agent=NAME,
                role="filter",
                facts_json=facts_json,
                instruction="Bewerte jede Schlagzeile (jede fact_id news.N genau einmal).",
                max_tokens=8000,
                effort="low",
            )
            verdicts = await self.ctx.llm.run(task)
            if not isinstance(verdicts, NewsVerdicts):
                # Ohne LLM: nur Notenbank-/SEC-Feeds und Betrugs-Stichworte durchlassen
                for it in chunk:
                    if not it.source.startswith("GDELT") or has_fraud_keyword(it.title, fraud_kw):
                        out.append(
                            RelevantNews(
                                it,
                                70,
                                "makro",
                                self._match(it.title, terms),
                                False,
                                it.title,
                                fraud=has_fraud_keyword(it.title, fraud_kw),
                            )
                        )
                continue
            for v in verdicts.items:
                try:
                    idx = int(v.fact_id.split(".")[1])
                    it = chunk[idx]
                except (IndexError, ValueError):
                    continue
                if v.importance < min_imp or v.category == "rauschen":
                    continue
                tickers = [t for t in v.tickers if t in watch_tickers] or self._match(it.title, terms)
                out.append(
                    RelevantNews(
                        it,
                        v.importance,
                        v.category,
                        tickers,
                        v.thesis_risk,
                        v.summary,
                        fraud=has_fraud_keyword(it.title, fraud_kw)
                        and v.category in ("firma", "regulierung"),
                    )
                )
        return out

    @staticmethod
    def _match(title: str, terms: dict[str, str]) -> list[str]:
        t = title.lower()
        return sorted({tick for term, tick in terms.items() if term in t})

    async def scan(self) -> list[RelevantNews]:
        items = await self.collect()
        if not items:
            return []
        relevant = await self.classify(items)
        for r in relevant:
            for subject in r.tickers or ["MAKRO"]:
                await self.ctx.db.add_signal(
                    "news_fraud" if r.fraud else NAME,
                    subject,
                    float(r.importance),
                    {
                        "title": r.item.title,
                        "url": r.item.url,
                        "source": r.item.source,
                        "published": r.item.published.isoformat() if r.item.published else None,
                        "category": r.category,
                        "summary": r.summary,
                        "thesis_risk": r.thesis_risk,
                    },
                )
        await self._sec_litigation(items)
        log.info("News: %d neu, %d relevant", len(items), len(relevant))
        return relevant

    async def _sec_litigation(self, items: list[NewsItem]) -> None:
        """Offizielle SEC-Verfahren: Firmenname im Titel einer Litigation Release -> Ausschluss-Signal."""
        terms = await self.watch_terms()
        for it in items:
            if it.source != "SEC Litigation":
                continue
            for tick in self._match(it.title, terms):
                await self.ctx.db.add_signal(
                    "sec_litigation",
                    tick,
                    100.0,
                    {
                        "title": it.title,
                        "url": it.url,
                        "source": it.source,
                        "summary": "SEC Litigation Release",
                        "published": it.published.isoformat() if it.published else None,
                    },
                )

    async def facts_for(self, ticker: str) -> AgentResult:
        res = AgentResult(agent=NAME, subject=ticker)
        rows = await self.ctx.db.recent_signals(ticker, days=14)
        k = 0
        for r in rows:
            if r["agent"] not in (NAME, "news_fraud", "sec_litigation"):
                continue
            p = json.loads(r["payload"])
            pub = (p.get("published") or r["ts"])[:10]
            res.facts.append(
                Fact(
                    id=f"news.{k}",
                    label=p.get("summary") or p.get("title", ""),
                    value=p.get("title"),
                    source=p.get("source", "News"),
                    as_of=datetime.fromisoformat(pub).date(),
                    url=p.get("url"),
                )
            )
            k += 1
            if k >= 8:
                break
        return res

    async def fraud_flags(self, ticker: str) -> list[str]:
        """Harte Ausschlüsse: nur offizielle SEC-Verfahren (Litigation Releases)."""
        return await self._flags(ticker, "sec_litigation", "Laufendes SEC-Verfahren")

    async def fraud_news(self, ticker: str) -> list[str]:
        """Presseberichte über Untersuchungen/Betrugsvorwürfe -> Risiko, Prüfung durch DevilsAdvocate."""
        return await self._flags(ticker, "news_fraud", "Bericht über Untersuchung/Vorwurf")

    async def _flags(self, ticker: str, agent: str, prefix: str) -> list[str]:
        rows = await self.ctx.db.recent_signals(ticker, days=180)
        flags = []
        for r in rows:
            if r["agent"] == agent:
                p = json.loads(r["payload"])
                flags.append(f"{prefix}: {p.get('title', '')[:120]} ({p.get('source', '')})")
        return flags[:2]
