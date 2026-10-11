"""DevilsAdvocate: sucht Gründe GEGEN jeden Kauf. Belegter K.-o.-Grund -> keine Empfehlung."""

from __future__ import annotations

from alpha_scout.agents.quality import _cid
from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import DevilsVerdict, valid_claim, valid_claims
from alpha_scout.models import AgentResult, FactSheet

NAME = "devils_advocate"


def devil_task(sheet: FactSheet, subject: str) -> LLMTask:
    return LLMTask(
        custom_id=f"devil-{_cid(subject)}",
        kind="devil",
        agent=NAME,
        role="analysis",
        facts_json=sheet.to_prompt_json(),
        instruction=f"Finde die stärksten Gegenargumente gegen einen Kauf von {subject} und prüfe auf K.-o.-Gründe.",
        max_tokens=5000,
        effort="medium",
    )


def interpret(subject: str, verdict: DevilsVerdict | None, known_ids: set[str]) -> AgentResult:
    res = AgentResult(agent=NAME, subject=subject)
    if verdict is None:
        res.findings.append("Gegenprüfung nicht durchgeführt (kein LLM/Budget).")
        return res
    args = valid_claims(verdict.counterarguments, known_ids)
    res.risks = [c.text for c in args]
    ko = valid_claim(verdict.knockout_reason, known_ids) if verdict.knockout else None
    if ko is not None:
        res.veto = True
        res.ko_reason = ko.text
    # Score: viele belegte Gegenargumente drücken leicht; K.-o. = 0
    res.score = 0.0 if res.veto else max(30.0, 80.0 - 8 * len(args))
    res.data = {"knockout": res.veto, "counterarguments": res.risks}
    return res
