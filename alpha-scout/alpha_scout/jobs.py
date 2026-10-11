"""Geplante Jobs: Intraday-Check, Nachtlauf (Batch), Wochen-Scan, 13F, Monatsbericht."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from alpha_scout.agents import devils_advocate as devil
from alpha_scout.agents.etf import ETFAgent
from alpha_scout.agents.performance import PerformanceTracker
from alpha_scout.agents.portfolio_keeper import PortfolioKeeper
from alpha_scout.agents.superinvestor import PositionChange, SuperinvestorAgent, days_since, describe_change
from alpha_scout.alerts import Dispatcher, esc
from alpha_scout.context import Context
from alpha_scout.llm.prompts import ThesisCheck
from alpha_scout.models import Alert, AlertType, AssetKind
from alpha_scout.orchestrator import Decision, build_alert, decide, recommendation_payload, thesis_task
from alpha_scout.pipeline import Analysis, Pipeline
from alpha_scout.report import format_holdings

log = logging.getLogger(__name__)

MAX_SUPERINVESTOR_ALERTS = 5
MAX_13F_AGE_DAYS = 30


class Jobs:
    def __init__(self, ctx: Context, dispatcher: Dispatcher):
        self.ctx = ctx
        self.dispatcher = dispatcher
        self.pipeline = Pipeline(ctx)
        self.keeper = PortfolioKeeper(ctx)
        self.performance = PerformanceTracker(ctx)
        self.superinvestor = SuperinvestorAgent(ctx)
        self.etf = ETFAgent(ctx)

    # ------------------------------------------------------------ Hilfen
    async def guarded(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Job ausführen; Fehler loggen statt abstürzen; Status für /status merken."""
        try:
            result = await fn()
            self.ctx.flags.last_runs[name] = self.ctx.now().strftime("%d.%m. %H:%M")
            self.ctx.flags.last_errors.pop(name, None)
            return result
        except Exception as e:
            log.error("Job %s fehlgeschlagen: %s\n%s", name, e, traceback.format_exc())
            self.ctx.flags.last_errors[name] = (
                f"{self.ctx.now().strftime('%d.%m. %H:%M')}: {type(e).__name__}: {e}"[:200]
            )
            return None

    def subjects(self, include_universe: bool = False) -> list[str]:
        """Priorität: Bestand > Watchlist > Signale > Universum."""
        wl = self.ctx.cfg.watchlist
        out = [p.ticker for p in self.ctx.cfg.holdings.positions]
        out += wl.all_securities()
        out += [c.upper() for c in wl.krypto]
        if include_universe:
            out += self.ctx.cfg.universe.all()
        return list(dict.fromkeys(out))

    async def signal_candidates(self, days: int = 7) -> list[str]:
        rows = await self.ctx.db.recent_signals(None, days=days)
        out = []
        for r in rows:
            if r["agent"] in ("superinvestor", "insider", "project") and r["strength"] >= 60:
                s = r["subject"]
                if s and s != "MAKRO" and " " not in s:
                    out.append(s)
        return list(dict.fromkeys(out))

    def position(self, subject: str):
        return next((p for p in self.ctx.cfg.holdings.positions if p.ticker == subject), None)

    async def etf_top_without_holding(self, a: Analysis) -> bool:
        if a.kind != AssetKind.ETF:
            return False
        cat, _ = self.etf.category_of(a.subject)
        if cat is None:
            return False
        ranking = await self.etf.ranking(cat)
        if not ranking or ranking[0][0].ticker != a.subject:
            return False
        held = {p.ticker for p in self.ctx.cfg.holdings.positions}
        return not any(self.etf.category_of(t)[0] == cat for t in held)

    async def act_on(
        self, a: Analysis, thesis: ThesisCheck | None = None, use_llm_writer: bool = True
    ) -> list[Decision]:
        decisions = decide(
            a,
            self.ctx.cfg.scoring,
            self.ctx.today(),
            self.position(a.subject),
            thesis,
            etf_is_top_without_holding=await self.etf_top_without_holding(a),
        )
        for d in decisions:
            if await self.ctx.db.recently_alerted(f"{d.type.name}:{a.subject}", 1):
                continue
            alert = await build_alert(self.ctx, a, d, use_llm=use_llm_writer)
            if thesis is not None and thesis.reasons:
                alert.why = [r.text for r in thesis.reasons][:3]
            p = recommendation_payload(a)
            await self.dispatcher.dispatch(
                alert, price=p["price"], currency=p["currency"], agent_scores=p["agent_scores"]
            )
        return decisions

    # ------------------------------------------------------------ Intraday (alle 30 Min.)
    async def intraday(self) -> None:
        from alpha_scout.agents.news import NewsAgent

        relevant = await NewsAgent(self.ctx).scan()
        held = {p.ticker for p in self.ctx.cfg.holdings.positions}
        risky = {t for r in relevant if r.thesis_risk for t in r.tickers if t in held}
        await self.pipeline.portfolio(refresh=True)
        for subject in self.subjects():
            a = await self.pipeline.prepare(subject, allow_stale_llm=True)
            a.pending_tasks.clear()
            a = await self.pipeline.finalize(a)
            thesis = None
            if subject in risky:
                thesis = await self.keeper.thesis_check(a)
            await self.act_on(a, thesis)
        await self.dispatcher.flush_pending()

    # ------------------------------------------------------------ Nachtlauf (Batch)
    async def nightly_submit(self, include_universe: bool | None = None) -> str | None:
        if include_universe is None:
            include_universe = self.ctx.today().weekday() == 6  # Sonntag
        subjects = self.subjects(include_universe) + await self.signal_candidates()
        subjects = list(dict.fromkeys(subjects))
        await self.pipeline.portfolio(refresh=True)
        tasks, kinds, prepared = [], {}, {}
        for s in subjects:
            a = await self.pipeline.prepare(s)
            if a.error:
                log.info("Nachtlauf: %s übersprungen (%s)", s, a.error)
                continue
            prepared[a.subject] = a
            for t in a.pending_tasks:
                tasks.append(t)
                kinds[t.custom_id] = t.kind
            if self.pipeline.devil_needed(a):
                t = devil.devil_task(a.sheet, a.subject)
                tasks.append(t)
                kinds[t.custom_id] = t.kind
            if self.position(a.subject) and a.sheet.value("port.thesis"):
                t = thesis_task(a)
                tasks.append(t)
                kinds[t.custom_id] = t.kind
        batch_id, submitted = await self.ctx.llm.submit_batch(tasks, purpose="nightly")
        submitted_ids = {t.custom_id for t in submitted}
        # Titel ohne offene LLM-Aufgaben sofort abschliessen
        waiting = []
        for subject, a in prepared.items():
            if any(cid.split("-", 1)[1] == _suffix(subject) for cid in submitted_ids):
                waiting.append(subject)
                continue
            a = await self.pipeline.finalize(a)
            await self.act_on(a)
        if batch_id:
            await self.ctx.db.add_batch(
                batch_id,
                "nightly",
                {"kinds": {k: v for k, v in kinds.items() if k in submitted_ids}, "subjects": waiting},
            )
        return batch_id

    async def poll_batches(self) -> None:
        for row in await self.ctx.db.open_batches():
            status = await self.ctx.llm.batch_status(row["id"])
            if status != "ended":
                continue
            meta = json.loads(row["meta"])
            outputs = await self.ctx.llm.collect_batch(row["id"], meta["kinds"])
            await self.ctx.db.set_batch_status(row["id"], "collected")
            await self.pipeline.portfolio(refresh=True)
            for subject in meta.get("subjects", []):
                a = await self.pipeline.prepare(subject)
                a.pending_tasks.clear()
                a = await self.pipeline.finalize(a, outputs)
                thesis = outputs.get(f"thesis-{_suffix(subject)}")
                await self.act_on(a, thesis if isinstance(thesis, ThesisCheck) else None)
        await self.dispatcher.flush_pending()

    # ------------------------------------------------------------ Wochen-Scan
    async def weekly(self) -> None:
        from alpha_scout.agents.project_scout import ProjectScout

        await ProjectScout(self.ctx).scan(days=7)
        ranking = {}
        for cat in self.ctx.cfg.etfs.categories:
            ranking[cat] = [(e.ticker, e.name, e.ter_pct, score) for e, score in await self.etf.ranking(cat)]
        await self.ctx.db.cache_set("etf:ranking", ranking, 8 * 24 * 3600)

    # ------------------------------------------------------------ 13F
    async def superinvestors(self) -> None:
        fresh = await self.superinvestor.refresh()
        # Nur frische Meldungen alarmieren (z. B. nicht beim ersten Start alte Quartale)
        fresh = [c for c in fresh if days_since(c.filing_date) <= MAX_13F_AGE_DAYS]
        if not fresh:
            return
        by_ticker: dict[str, list[PositionChange]] = {}
        for c in fresh:
            by_ticker.setdefault(c.ticker or c.issuer, []).append(c)
        ranked = sorted(
            by_ticker.items(),
            key=lambda kv: (-len({c.cik for c in kv[1]}), -max(c.weight_pct for c in kv[1])),
        )
        for subject, changes in ranked[:MAX_SUPERINVESTOR_ALERTS]:
            a = None
            if changes[0].ticker:
                a = await self.pipeline.prepare(changes[0].ticker, allow_stale_llm=True)
                a.pending_tasks.clear()
                a = await self.pipeline.finalize(a) if not a.error else a
            cluster = len({c.cik for c in changes}) >= self.ctx.cfg.superinvestors.cluster_min_investors
            facts = [describe_change(c) for c in changes]
            facts.append(
                f"13F-Stichtag {changes[0].period}, gemeldet {changes[0].filing_date} (bis 45 Tage Verzug)"
            )
            if a is not None and not a.error:
                q = a.sheet.value("quality.count")
                facts.append(
                    f"Qualitätsfilter: {q}/6 – Score {a.score:.0f}/100"
                    if a.score is not None
                    else f"Qualitätsfilter: {q}/6"
                )
            alert = Alert(
                type=AlertType.SUPERINVESTOR,
                subject=subject,
                title=(f"{a.name} ({subject})" if a is not None and not a.error else subject)
                + (" – KAUFCLUSTER" if cluster else ""),
                why=[
                    f"{len({c.cik for c in changes})} Top-Investor(en) haben laut 13F gross gekauft.",
                    "13F zeigt nur US-Long-Positionen zum Quartalsende – der Kauf kann schon Wochen zurückliegen.",
                    "Kein automatisches Kaufsignal: erst Qualitätsfilter und Bewertung prüfen.",
                ],
                facts=facts,
                risks=(a.results["risk"].risks if a is not None and "risk" in a.results else []),
                counterarguments=(a.vetoes if a is not None else []),
                action=f"Details mit /check {subject}"
                if changes[0].ticker
                else "Kein Ticker zuordenbar – manuell prüfen",
                sources=["SEC EDGAR 13F-HR"],
                swissquote=a.swissquote
                if a is not None and not a.error
                else Alert.model_fields["swissquote"].default,
                swissquote_note=a.swissquote_note if a is not None and not a.error else "",
                score=a.score if a is not None else None,
                dedupe_key=f"SUPERINVESTOR:{subject}:{changes[0].period}",
            )
            await self.dispatcher.dispatch(
                alert,
                price=a.price if a is not None else None,
                currency=a.currency if a is not None else "",
                agent_scores=a.agent_scores() if a is not None else {},
            )

    # ------------------------------------------------------------ Monatsbericht
    async def monthly_report(self) -> str:
        await self.performance.evaluate_due()
        perf = await self.performance.summary()
        snap = await self.pipeline.portfolio(refresh=True)
        rules = self.ctx.cfg.scoring.portfolio
        today = self.ctx.today()
        month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        costs = await self.ctx.db.costs_by_day(month_start)
        last_month = sum(r["cost"] for r in costs if r["day"] < today.replace(day=1).isoformat())
        parts = [f"📅 <b>Monatsbericht {month_start.strftime('%m.%Y')}</b>"]
        parts.append(
            format_holdings(
                snap, rules, self.ctx.cfg.scoring.min_holding_days_for_sell, self.ctx.settings.base_currency
            )
        )
        if snap.total_chf > 0 and snap.bucket_pct("kern") < rules.core_target_pct - 5:
            ranking = await self.etf.ranking("msci_world")
            if ranking:
                best = ranking[0][0]
                parts.append(
                    f"💡 Kern-Anteil unter Ziel ({snap.bucket_pct('kern'):.0f} % von {rules.core_target_pct:.0f} %). "
                    f"Günstigster MSCI-World-ETF laut Daten: <b>{esc(best.ticker)}</b> {esc(best.name)} "
                    f"(TER {best.ter_pct} %, Domizil {esc(best.domicile or 'prüfen')})."
                )
        lines = ["<b>Empfehlungen vs. MSCI World</b>"]
        if perf["by_horizon"]:
            for h in perf["by_horizon"]:
                lines.append(
                    f"• {h['horizon_months']} Mt.: Ø {h['ret']:+.1f} % vs. {h['bench']:+.1f} % "
                    f"({h['wins']}/{h['n']} besser)"
                )
        else:
            lines.append(
                f"• Noch keine fälligen Auswertungen ({perf['recommendations']} Empfehlungen erfasst)"
            )
        if perf["weights"]:
            lines.append(
                "Agenten-Gewichte: " + ", ".join(f"{w['agent']} {w['factor']:.2f}" for w in perf["weights"])
            )
        parts.append("\n".join(lines))
        parts.append(f"<b>LLM-Kosten Vormonat</b>: ${last_month:.2f}")
        text = "\n\n".join(parts)
        await self.dispatcher.send_text(text)
        return text


def _suffix(subject: str) -> str:
    from alpha_scout.agents.quality import _cid

    return _cid(subject)
