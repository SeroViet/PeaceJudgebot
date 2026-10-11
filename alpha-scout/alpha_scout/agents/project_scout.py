"""ProjectScout: grosse Bauprojekte/Ausschreibungen (Strom, Rechenzentren, Pipelines, Halbleiter, Rüstung)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpha_scout.context import Context
from alpha_scout.data.tenders import Tender
from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import TenderVerdicts
from alpha_scout.models import AgentResult, Fact, FactSheet

log = logging.getLogger(__name__)

NAME = "project"


@dataclass
class ProjectHit:
    tender: Tender
    theme: str
    importance: int
    winner_ticker: str | None
    candidates: list[str]


def match_theme(text: str, themes: dict[str, list[str]]) -> str | None:
    t = text.lower()
    best, hits = None, 0
    for name, kws in themes.items():
        n = sum(1 for k in kws if k.lower() in t)
        if n > hits:
            best, hits = name, n
    return best


def map_contractor(winner: str | None, aliases: dict[str, str]) -> str | None:
    if not winner:
        return None
    w = winner.upper()
    for key, ticker in aliases.items():
        if key.upper() in w:
            return ticker
    return None


class ProjectScout:
    name = NAME

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def scan(self, days: int = 7) -> list[ProjectHit]:
        cfg = self.ctx.cfg.themes
        kw = {k: v.keywords for k, v in cfg.themes.items()}
        all_kw = sorted({k for v in kw.values() for k in v if " " not in k or len(k) < 30})
        tenders: list[Tender] = []
        tenders += await self.ctx.tenders.usaspending(days, cfg.min_award_usd)
        tenders += await self.ctx.tenders.ted(all_kw[:25], days)
        tenders += await self.ctx.tenders.simap(days)
        prelim = []
        for t in tenders:
            theme = match_theme(f"{t.title} {t.buyer}", kw)
            if theme is None:
                continue
            if t.value_usd is not None and t.value_usd < cfg.min_award_usd and t.source != "SIMAP (CH)":
                continue
            if not await self.ctx.db.mark_seen(f"tender:{t.source}:{t.id}"):
                continue
            prelim.append((t, theme))
        if not prelim:
            return []
        verdicts = await self._classify(prelim)
        hits = []
        for i, (t, theme) in enumerate(prelim):
            v = verdicts.get(i)
            if v is not None and (v.theme == "keins" or v.importance < 50):
                continue
            theme = v.theme if v is not None else theme
            importance = v.importance if v is not None else 60
            winner = map_contractor(t.winner, cfg.contractor_aliases)
            if winner is None and t.winner:
                winner = await self.ctx.sec.ticker_for_name(t.winner)
            hit = ProjectHit(t, theme, importance, winner, cfg.themes[theme].candidates)
            hits.append(hit)
            payload = {
                "title": t.title,
                "source": t.source,
                "url": t.url,
                "value_usd": t.value_usd,
                "theme": theme,
                "buyer": t.buyer,
                "winner": t.winner,
                "summary": v.summary if v else t.title,
            }
            if winner:
                await self.ctx.db.add_signal(NAME, winner, float(importance), payload)
            for c in hit.candidates:
                if c != winner:
                    await self.ctx.db.add_signal(NAME, c, importance / 2, payload)
        log.info("ProjectScout: %d Ausschreibungen, %d relevant", len(tenders), len(hits))
        return hits

    async def _classify(self, prelim: list[tuple[Tender, str]]) -> dict[int, object]:
        sheet = FactSheet(subject="Ausschreibungen")
        for i, (t, _) in enumerate(prelim[:60]):
            val = f"{t.value_usd / 1e6:.0f} Mio. USD" if t.value_usd else "Wert unbekannt"
            sheet.add(
                Fact(
                    id=f"proj.{i}",
                    label=f"{t.title} | Auftraggeber: {t.buyer} | Gewinner: {t.winner or '-'}",
                    value=val,
                    source=t.source,
                )
            )
        task = LLMTask(
            custom_id="projects",
            kind="project",
            agent=NAME,
            role="filter",
            facts_json=sheet.to_prompt_json(),
            instruction="Ordne jede Ausschreibung (proj.N) einem Thema zu.",
            max_tokens=6000,
            effort="low",
        )
        out = await self.ctx.llm.run(task)
        if not isinstance(out, TenderVerdicts):
            return {}
        result = {}
        for v in out.items:
            try:
                result[int(v.fact_id.split(".")[1])] = v
            except (IndexError, ValueError):
                continue
        return result

    async def facts_for(self, ticker: str) -> AgentResult:
        import json

        res = AgentResult(agent=NAME, subject=ticker)
        rows = [r for r in await self.ctx.db.recent_signals(ticker, days=90) if r["agent"] == NAME]
        for k, r in enumerate(rows[:3]):
            p = json.loads(r["payload"])
            res.facts.append(
                Fact(
                    id=f"proj.{k}",
                    label=f"Projekt ({p.get('theme')}): {p.get('summary', '')[:160]}",
                    value=p.get("value_usd"),
                    unit="USD",
                    source=p.get("source", ""),
                    url=p.get("url"),
                )
            )
        if rows:
            res.score = min(100.0, 55 + max(r["strength"] for r in rows) * 0.4)
        return res
