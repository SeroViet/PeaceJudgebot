"""QualityAgent: Qualitätsfilter (mind. 4 von 6) + harte Ausschlüsse. Burggraben per LLM."""

from __future__ import annotations

from dataclasses import dataclass, field

from alpha_scout.llm.client import LLMTask
from alpha_scout.llm.prompts import MoatAssessment, valid_claims
from alpha_scout.models import AgentResult, Fact, FactSheet

NAME = "quality"
SRC = "Alpha Scout (berechnet)"

CRITERIA = {
    1: "Profitabel oder klarer Weg zur Profitabilität",
    2: "Umsatzwachstum > 10 % p. a. über 3 Jahre",
    3: "Starker Burggraben",
    4: "Solide Bilanz (Nettoverschuldung < 3x EBITDA)",
    5: "Keine Verwässerung > 5 % p. a.",
    6: "Von Top-Investoren gehalten (13F)",
}


@dataclass
class QualityCheck:
    criteria: dict[int, bool | None] = field(default_factory=dict)  # None = keine Daten
    notes: dict[int, str] = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for v in self.criteria.values() if v)

    @property
    def passed(self) -> bool:
        return not self.exclusions and self.passed_count >= 4

    def score(self) -> float:
        if self.exclusions:
            return 0.0
        base = {6: 100, 5: 90, 4: 75, 3: 40, 2: 25, 1: 10, 0: 0}[self.passed_count]
        return float(base)


def check_quality(sheet: FactSheet, superinvestor_holders: list[str], fraud_flags: list[str]) -> QualityCheck:
    """Kriterien 1, 2, 4, 5, 6 und Ausschlüsse deterministisch. Kriterium 3 kommt vom LLM."""
    q = QualityCheck()
    v = sheet.value

    # 1 Profitabilität
    ni_ttm, ni_last, fwd = v("fund.net_income_ttm"), v("fund.net_income_last"), v("fund.forward_eps")
    profitable = None
    if ni_ttm is not None or ni_last is not None:
        profitable = (ni_ttm if ni_ttm is not None else ni_last) > 0
    if profitable:
        q.criteria[1], q.notes[1] = True, "profitabel"
    elif fwd is not None and fwd > 0:
        q.criteria[1], q.notes[1] = True, "Verlust, aber Analysten erwarten Gewinn im nächsten Jahr"
    else:
        q.criteria[1] = False if profitable is False else None
        q.notes[1] = "nicht profitabel" if profitable is False else "keine Daten"

    # 2 Wachstum
    g = v("fund.revenue_cagr_3y")
    q.criteria[2] = None if g is None else g > 10
    q.notes[2] = "keine Daten" if g is None else f"{g:.1f} % p. a."

    # 4 Bilanz
    nd, nde = v("fund.net_debt"), v("fund.net_debt_ebitda")
    if nd is not None and nd <= 0:
        q.criteria[4], q.notes[4] = True, "Netto-Cash"
    elif nde is not None:
        q.criteria[4], q.notes[4] = nde < 3, f"{nde:.2f}x EBITDA"
    elif nd is not None:
        q.criteria[4], q.notes[4] = False, "Schulden bei negativem EBITDA"
    else:
        q.criteria[4], q.notes[4] = None, "keine Daten"

    # 5 Verwässerung
    d = v("fund.dilution_3y")
    q.criteria[5] = None if d is None else d <= 5
    q.notes[5] = "keine Daten" if d is None else f"{d:+.1f} % p. a."

    # 6 Superinvestoren
    q.criteria[6] = bool(superinvestor_holders)
    q.notes[6] = (
        ", ".join(superinvestor_holders[:4]) if superinvestor_holders else "kein Top-Investor im letzten 13F"
    )

    # 3 Burggraben: noch offen
    q.criteria[3] = None
    q.notes[3] = "noch nicht beurteilt"

    # Ausschlüsse
    price_usd = v("price.usd")
    if price_usd is not None and price_usd < 5:
        q.exclusions.append(f"Penny Stock (Kurs {price_usd:.2f} USD < 5 USD)")
    mcap_usd = v("mcap.usd")
    if mcap_usd is not None and mcap_usd < 1e9 and profitable is False:
        q.exclusions.append("Marktkapitalisierung < 1 Mrd USD bei Verlust")
    q.exclusions.extend(fraud_flags)
    return q


def apply_moat(
    q: QualityCheck, moat: MoatAssessment | None, known_ids: set[str]
) -> tuple[list[str], list[str]]:
    """Burggraben-Ergebnis übernehmen. Gibt (Belege, Bedenken) als Texte zurück."""
    if moat is None:
        q.criteria[3], q.notes[3] = None, "nicht beurteilt (kein LLM/Budget)"
        return [], []
    evidence = valid_claims(moat.evidence, known_ids)
    concerns = valid_claims(moat.concerns, known_ids)
    strong = moat.moat == "stark" and len(evidence) >= 2
    q.criteria[3] = strong
    q.notes[3] = f"{moat.moat}" + (f" ({', '.join(moat.moat_types)})" if moat.moat_types else "")
    if moat.moat == "stark" and not strong:
        q.notes[3] += " – zu wenig belegt, zählt nicht"
    return [c.text for c in evidence], [c.text for c in concerns]


def moat_task(sheet: FactSheet, subject: str) -> LLMTask:
    return LLMTask(
        custom_id=f"moat-{_cid(subject)}",
        kind="moat",
        agent=NAME,
        role="analysis",
        facts_json=sheet.to_prompt_json(),
        instruction=f"Beurteile den Burggraben von {subject}.",
        max_tokens=4000,
        effort="medium",
    )


def to_result(subject: str, q: QualityCheck, evidence: list[str], concerns: list[str]) -> AgentResult:
    res = AgentResult(agent=NAME, subject=subject, score=q.score())
    for i, label in CRITERIA.items():
        ok = q.criteria.get(i)
        mark = "✅" if ok else ("❔" if ok is None else "❌")
        res.findings.append(f"{mark} {i}. {label}: {q.notes.get(i, '')}")
        res.facts.append(Fact(id=f"quality.c{i}", label=f"Kriterium {i}: {label}", value=ok, source=SRC))
    res.facts.append(Fact(id="quality.passed", label="Qualitätsfilter bestanden", value=q.passed, source=SRC))
    res.facts.append(
        Fact(id="quality.count", label="Erfüllte Kriterien (von 6)", value=q.passed_count, source=SRC)
    )
    for k, ex in enumerate(q.exclusions):
        res.facts.append(Fact(id=f"quality.exclusion{k}", label="Ausschlussgrund", value=ex, source=SRC))
    res.findings.extend(f"Burggraben: {e}" for e in evidence)
    res.risks.extend(concerns)
    if q.exclusions:
        res.veto = True
        res.ko_reason = "; ".join(q.exclusions)
    elif not q.passed:
        res.veto = True
        res.ko_reason = f"Qualitätsfilter nicht bestanden ({q.passed_count}/6)"
    res.data = {"criteria": q.criteria, "notes": q.notes, "exclusions": q.exclusions, "passed": q.passed}
    return res


def _cid(subject: str) -> str:
    """custom_id erlaubt nur [a-zA-Z0-9_-]."""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in subject)[:50]
