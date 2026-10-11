"""Orchestrator: Score 0–100, Entscheidung über Alerts, Meldungstext, Versand."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from alpha_scout.config import Position, ScoringConfig
from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import AlertText, ThesisCheck, valid_claim, valid_claims
from alpha_scout.models import AgentResult, Alert, AlertType, AssetKind, Fact

if TYPE_CHECKING:
    from alpha_scout.context import Context
    from alpha_scout.pipeline import Analysis

log = logging.getLogger(__name__)

STOCK_AGENTS = ("quality", "valuation", "fundamental", "superinvestor", "insider", "news", "project")
DEDUPE_DAYS = {
    AlertType.NEW_CHANCE: 60,
    AlertType.BUY_MORE: 30,
    AlertType.WARNING: 7,
    AlertType.SELL_CHECK: 30,
    AlertType.SUPERINVESTOR: 365,
}
CRYPTO_DROP_FACTOR = 2.0  # Krypto schwankt stärker: Nachkauf erst ab doppeltem Rückgang
HARD_VETO_AGENTS = ("quality", "devils_advocate", "crypto")


def combine_scores(
    results: dict[str, AgentResult], scoring: ScoringConfig, factors: dict[str, float], kind: AssetKind
) -> tuple[float | None, list[str]]:
    vetoes = [f"{r.agent}: {r.ko_reason}" for r in results.values() if r.veto and r.ko_reason]
    if kind == AssetKind.ETF:
        base = results.get("etf")
        score = base.score if base else None
    elif kind == AssetKind.CRYPTO:
        base = results.get("crypto")
        score = base.score if base else None
    else:
        num = den = 0.0
        for agent in STOCK_AGENTS:
            w = scoring.weights.get(agent, 0) * factors.get(agent, 1.0)
            r = results.get(agent)
            s = r.score if r is not None and r.score is not None else scoring.neutral_score
            num += w * s
            den += w
        score = round(num / den, 1) if den else None
    if vetoes:
        return 0.0, vetoes
    return score, vetoes


def hard_vetoes(a: Analysis) -> list[str]:
    """Vetos, die die Anlage-These betreffen (nicht nur die Positionsgrösse)."""
    out = [f"{r.ko_reason}" for k, r in a.results.items() if k in HARD_VETO_AGENTS and r.veto and r.ko_reason]
    risk = a.results.get("risk")
    if risk is not None:
        out.extend(risk.data.get("hard", []))
    return out


@dataclass
class Decision:
    type: AlertType
    reason: str
    urgent: bool = False


def decide(
    a: Analysis,
    scoring: ScoringConfig,
    today: date,
    position: Position | None,
    thesis: ThesisCheck | None = None,
    etf_is_top_without_holding: bool = False,
) -> list[Decision]:
    if a.error or a.price is None:
        return []
    out: list[Decision] = []
    v = a.sheet.value
    hard = hard_vetoes(a)
    buy_stopped = v("port.buy_stop") is not None
    drop = scoring.nachkauf_drop_pct * (CRYPTO_DROP_FACTOR if a.kind == AssetKind.CRYPTO else 1)
    thesis_bad = thesis is not None and thesis.status in ("gefaehrdet", "zerbrochen")
    score = a.score or 0

    if position is not None:
        days = (today - position.buy_date).days
        may_sell = days >= scoring.min_holding_days_for_sell
        if hard or thesis_bad:
            why = "; ".join(hard) if hard else f"These {thesis.status}"  # type: ignore[union-attr]
            if may_sell and (thesis is not None and thesis.status == "zerbrochen" or hard):
                out.append(Decision(AlertType.SELL_CHECK, f"These zerbrochen/gefährdet: {why}", urgent=True))
            else:
                out.append(Decision(AlertType.WARNING, f"Kaufgrund in Gefahr: {why}", urgent=True))
        elif v("val.in_sell_zone") is True:
            if may_sell:
                out.append(
                    Decision(AlertType.SELL_CHECK, "Kurs in der Verkaufszone (deutlich über fairem Wert)")
                )
    if out:
        return out

    # Nachkauf: gute Firma >= 20 % gefallen, Kaufgründe intakt
    if not a.vetoes and not buy_stopped and score >= 60:
        dd = v("price.drawdown_52w")
        fell = dd is not None and dd <= -drop
        if position is not None and position.buy_price and a.currency == position.currency:
            fell = fell or a.price <= position.buy_price * (1 - drop / 100)
        quality_ok = a.kind != AssetKind.STOCK or bool(v("quality.passed"))
        watched = position is not None or a.sheet.value("port.watch") is True
        if fell and quality_ok and watched:
            out.append(Decision(AlertType.BUY_MORE, f"{abs(dd or 0):.0f} % unter Hoch, Kaufgründe intakt"))
            return out

    # Neue Chance
    if position is None and not a.vetoes and not buy_stopped and score >= scoring.alert_new_chance_min:
        if a.kind != AssetKind.ETF or etf_is_top_without_holding:
            out.append(
                Decision(AlertType.NEW_CHANCE, f"Score {score:.0f} – Qualität, Bewertung und Signale passen")
            )
    return out


def writer_task(a: Analysis, d: Decision) -> LLMTask:
    from alpha_scout.agents.quality import _cid

    sheet = a.sheet.model_copy(deep=True)
    sheet.add(Fact(id="decision.type", label="Meldungstyp", value=d.type.value, source="Orchestrator"))
    sheet.add(Fact(id="decision.reason", label="Grund", value=d.reason, source="Orchestrator"))
    return LLMTask(
        custom_id=f"writer-{_cid(a.subject)}",
        kind="writer",
        agent="orchestrator",
        role="analysis",
        facts_json=sheet.to_prompt_json(),
        instruction=f"Schreibe die Meldung '{d.type.value}' für {a.name} ({a.subject}).",
        max_tokens=5000,
        effort="low",
    )


def action_plan(a: Analysis, d: Decision, scoring: ScoringConfig) -> str:
    v = a.sheet.value
    ccy = a.currency
    max_pct = v("decision.max_position_pct")
    buy_max = v("val.buy_zone_max")
    if d.type in (AlertType.NEW_CHANCE, AlertType.BUY_MORE):
        size = (
            f"Zielgrösse max. {max_pct:.1f} % des Depots"
            if isinstance(max_pct, int | float)
            else "Positionsgrösse gemäss Depotregeln"
        )
        zone = f", nur bis {buy_max:.2f} {ccy} (Kaufzone)" if isinstance(buy_max, int | float) else ""
        in_zone = v("val.in_buy_zone")
        start = (
            "Tranche 1 jetzt"
            if in_zone in (True, None)
            else "Kurs über der Kaufzone: Limit setzen und abwarten"
        )
        return f"Kauf in 3 Tranchen über 2–3 Monate. {start}{zone}. {size}. Bei Swissquote mit Limit-Order kaufen."
    if d.type == AlertType.SELL_CHECK:
        return (
            "Verkauf prüfen: Teilverkauf (z. B. 1/3) erwägen, Gewinne laufen lassen, wenn die These intakt ist. "
            "Haltedauer ≥ 6 Monate ist erfüllt."
        )
    if d.type == AlertType.WARNING:
        return "Nicht nachkaufen. Position beobachten und These prüfen; nach 6 Monaten Haltedauer Verkauf erwägen, falls bestätigt."
    return "Für Details: /check " + a.subject


async def build_alert(ctx: Context, a: Analysis, d: Decision, use_llm: bool = True) -> Alert:
    known = set(a.sheet.facts)
    why: list[str] = []
    facts: list[str] = []
    risks: list[str] = []
    if use_llm:
        task = writer_task(a, d)
        out = await ctx.llm.run(task)
        if isinstance(out, AlertText):
            known_w = known | {"decision.type", "decision.reason"}
            why = [c.text for c in valid_claims(out.why, known_w)][:3]
            facts = [c.text for c in valid_claims(out.key_facts, known_w)][:6]
            risks = [c.text for c in valid_claims(out.risks, known_w)][:4]
            _ = valid_claim(out.action, known_w)
    if not why:
        why = [d.reason, f"Gesamtscore {a.score if a.score is not None else '–'} von 100."]
    if not facts:
        facts = default_facts(a)
    risk_res = a.results.get("risk")
    if risk_res:
        risks += [r for r in risk_res.risks if r not in risks]
    counter: list[str] = []
    dv = a.results.get("devils_advocate")
    if dv:
        counter += dv.risks[:4]
    q = a.results.get("quality")
    if q:
        counter += [r for r in q.risks[:2] if r not in counter]
    sources = sorted(
        {f.source for f in a.sheet.facts.values() if not f.source.startswith(("Alpha Scout", "Orchestrator"))}
    )
    urls = [f.url for f in a.sheet.facts.values() if f.url][:3]
    return Alert(
        type=d.type,
        subject=a.subject,
        title=f"{a.name} ({a.subject})",
        why=why,
        facts=facts,
        risks=risks[:5],
        counterarguments=counter[:5],
        action=action_plan(a, d, ctx.cfg.scoring),
        sources=sources + urls,
        swissquote=a.swissquote,
        swissquote_note=a.swissquote_note,
        score=a.score,
        dedupe_key=f"{d.type.name}:{a.subject}",
        urgent=d.urgent,
    )


def default_facts(a: Analysis) -> list[str]:
    keys = [
        "price.last",
        "price.drawdown_52w",
        "fund.revenue_cagr_3y",
        "fund.operating_margin",
        "fund.fcf_margin",
        "fund.net_debt_ebitda",
        "val.fair_base",
        "val.buy_zone_max",
        "val.sell_zone_min",
        "quality.count",
        "si.holders",
        "ins.buyers_30d",
        "etf.ter",
        "etf.domicile",
        "crypto.drawdown_ath",
        "crypto.fees_30d",
    ]
    out = []
    for k in keys:
        f = a.sheet.get(k)
        if f is not None and f.value is not None:
            out.append(f.display())
    return out[:7]


def thesis_task(a: Analysis) -> LLMTask:
    from alpha_scout.agents.quality import _cid

    return LLMTask(
        custom_id=f"thesis-{_cid(a.subject)}",
        kind="thesis",
        agent="portfolio_keeper",
        role="analysis",
        facts_json=a.sheet.to_prompt_json(),
        instruction=f"Prüfe, ob die Kaufthese für {a.subject} noch intakt ist.",
        max_tokens=4000,
        effort="low",
    )


def recommendation_payload(a: Analysis) -> dict[str, Any]:
    return {"agent_scores": a.agent_scores(), "price": a.price, "currency": a.currency}
