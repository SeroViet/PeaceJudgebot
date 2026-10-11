"""RiskAgent (Veto): Klumpenrisiko, Bilanzrisiko, Geopolitik, Anlageregeln."""

from __future__ import annotations

from alpha_scout.agents.portfolio import PortfolioSnapshot
from alpha_scout.config import PortfolioRules
from alpha_scout.models import AgentResult, Fact, FactSheet

NAME = "risk"
SRC = "Alpha Scout Risikoregeln"

BANNED_WORDS = (
    "leveraged",
    "2x",
    "3x",
    "ultra",
    "short",
    "inverse",
    "bull ",
    "bear ",
    "turbo",
    "mini future",
    "warrant",
)


def assess_risk(
    subject: str,
    sheet: FactSheet,
    *,
    bucket: str,
    snap: PortfolioSnapshot,
    rules: PortfolioRules,
    name: str | None = None,
) -> AgentResult:
    res = AgentResult(agent=NAME, subject=subject, score=100.0)
    v = sheet.value

    hard: list[str] = []
    allocation: list[str] = []

    def veto(reason: str, kind: str = "hard") -> None:
        res.veto = True
        res.ko_reason = (res.ko_reason + "; " if res.ko_reason else "") + reason
        (allocation if kind == "allocation" else hard).append(reason)

    def penalty(points: float, text: str) -> None:
        res.score = max(0.0, (res.score or 0) - points)
        res.risks.append(text)

    # Keine Hebel-/Derivate-Produkte
    lname = (name or "").lower()
    if any(w in lname for w in BANNED_WORDS):
        veto(f"Hebel-/Derivatprodukt erkannt ({name})")

    # Bilanzrisiko
    nde = v("fund.net_debt_ebitda")
    if nde is not None and nde >= 3:
        veto(f"Bilanzrisiko: Nettoverschuldung {nde:.1f}x EBITDA")
    elif nde is not None and nde >= 2:
        penalty(15, f"Erhöhte Verschuldung ({nde:.1f}x EBITDA)")
    fcf, nd = v("fund.fcf_last"), v("fund.net_debt")
    if fcf is not None and fcf < 0 and nd is not None and nd > 0:
        penalty(25, "Negativer freier Cashflow bei Nettoverschuldung")

    # Geopolitik
    country = v("profile.country")
    if country in rules.veto_countries:
        veto(f"Geopolitik: Sitz in {country}")
    elif country in rules.watch_countries:
        penalty(10, f"Geopolitisches Risiko: Sitz in {country}")

    # Allokation
    tranche = rules.tranche_pct
    current = snap.ticker_pct(subject)
    max_pos = None
    if snap.total_chf <= 0:
        res.findings.append("Depot leer/unbekannt – Allokationsregeln nicht prüfbar.")
    else:
        if bucket == "qualitaet":
            bucket_now = snap.bucket_pct("qualitaet")
            if current + tranche > rules.single_stock_max_pct:
                veto(
                    f"Position wäre > {rules.single_stock_max_pct:.0f} % des Depots (jetzt {current:.1f} %)",
                    "allocation",
                )
            if bucket_now + tranche > rules.quality_max_pct:
                veto(
                    f"Qualitätsaktien-Anteil wäre > {rules.quality_max_pct:.0f} % (jetzt {bucket_now:.1f} %)",
                    "allocation",
                )
            sector = v("profile.sector")
            if sector:
                sp = snap.sector_pct_of_stocks(sector)
                if sp > rules.sector_max_pct_of_stocks:
                    penalty(20, f"Klumpenrisiko: {sector} = {sp:.0f} % der Einzelaktien")
            max_pos = max(0.0, rules.single_stock_max_pct - current)
        elif bucket == "rest":
            rest_now = snap.bucket_pct("rest")
            if rest_now + tranche > rules.rest_max_pct:
                veto(
                    f"Rest-Anteil (Krypto/Themen) wäre > {rules.rest_max_pct:.0f} % (jetzt {rest_now:.1f} %)",
                    "allocation",
                )
            max_pos = max(0.0, rules.rest_max_pct - rest_now)
        elif bucket == "kern":
            max_pos = max(0.0, rules.core_target_pct - snap.bucket_pct("kern"))
        res.facts.append(
            Fact(
                id="port.current_pct",
                label="Aktueller Depotanteil",
                value=round(current, 2),
                unit="%",
                source=SRC,
            )
        )
    if max_pos is None:
        max_pos = {
            "qualitaet": rules.single_stock_max_pct,
            "rest": rules.rest_max_pct,
            "kern": rules.core_target_pct,
        }.get(bucket, 1.0)
    res.facts.append(
        Fact(
            id="decision.max_position_pct",
            label="Maximale Zielgrösse der Position",
            value=round(max_pos, 2),
            unit="%",
            source=SRC,
        )
    )
    res.facts.append(Fact(id="decision.bucket", label="Depot-Topf", value=bucket, source=SRC))
    res.data = {"max_position_pct": max_pos, "bucket": bucket, "hard": hard, "allocation": allocation}
    return res
