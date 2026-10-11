"""Gemeinsame Datenmodelle."""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Fact(BaseModel):
    """Ein abgerufener Datenpunkt mit Quelle. Grundlage jeder Aussage."""

    id: str
    label: str
    value: float | int | str | bool | None
    unit: str = ""
    source: str
    as_of: date | None = None
    url: str | None = None

    def display(self) -> str:
        v = self.value
        if v is None:
            txt = "unbekannt"
        elif isinstance(v, bool):
            txt = "ja" if v else "nein"
        elif isinstance(v, float):
            txt = fmt_number(v, self.unit)
        else:
            txt = f"{v}{(' ' + self.unit) if self.unit else ''}"
        return f"{self.label}: {txt}"


def fmt_number(v: float, unit: str = "") -> str:
    if unit == "%":
        return f"{v:.2f} %" if abs(v) < 1 else f"{v:.1f} %"
    if unit == "x":
        return f"{v:.2f}x"
    a = abs(v)
    if a >= 1e12:
        s = f"{v / 1e12:.2f} Bio."
    elif a >= 1e9:
        s = f"{v / 1e9:.2f} Mrd."
    elif a >= 1e6:
        s = f"{v / 1e6:.1f} Mio."
    elif a >= 100:
        s = f"{v:,.0f}".replace(",", "'")
    else:
        s = f"{v:.2f}"
    return f"{s} {unit}".strip()


class FactSheet(BaseModel):
    """Alle Fakten zu einem Titel – wird dem LLM als JSON übergeben."""

    subject: str
    facts: dict[str, Fact] = Field(default_factory=dict)

    def add(self, fact: Fact) -> Fact:
        self.facts[fact.id] = fact
        return fact

    def extend(self, facts: list[Fact]) -> None:
        for f in facts:
            self.add(f)

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def value(self, fact_id: str) -> Any:
        f = self.facts.get(fact_id)
        return None if f is None else f.value

    def to_prompt_json(self) -> str:
        rows = [
            {
                "id": f.id,
                "label": f.label,
                "value": f.value,
                "unit": f.unit,
                "source": f.source,
                "as_of": f.as_of.isoformat() if f.as_of else None,
            }
            for f in sorted(self.facts.values(), key=lambda x: x.id)
        ]
        return json.dumps({"subject": self.subject, "facts": rows}, ensure_ascii=False, sort_keys=True)

    def sources(self) -> list[str]:
        return sorted({f.source for f in self.facts.values()})


class AssetKind(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"


class AgentResult(BaseModel):
    agent: str
    subject: str
    score: float | None = None  # 0–100, None = keine Aussage
    veto: bool = False
    ko_reason: str | None = None
    findings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0


class AlertType(StrEnum):
    NEW_CHANCE = "💎 NEUE CHANCE"
    BUY_MORE = "📈 NACHKAUF"
    WARNING = "⚠️ WARNUNG"
    SELL_CHECK = "💰 VERKAUFEN PRÜFEN"
    SUPERINVESTOR = "🐋 SUPERINVESTOR"


class SwissquoteStatus(StrEnum):
    CONFIRMED = "✅"
    LIKELY = "⚠️"
    NO = "❌"


class Alert(BaseModel):
    type: AlertType
    subject: str
    title: str
    why: list[str] = Field(default_factory=list)  # max. 3 Sätze
    facts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list)
    action: str = ""
    sources: list[str] = Field(default_factory=list)
    swissquote: SwissquoteStatus = SwissquoteStatus.LIKELY
    swissquote_note: str = ""
    score: float | None = None
    dedupe_key: str = ""
    urgent: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class Recommendation(BaseModel):
    subject: str
    alert_type: AlertType
    score: float | None
    price: float
    currency: str
    agent_scores: dict[str, float | None]
    created_at: datetime
