"""ValuationAgent: fairer Wert (Szenarien 3 und 10 Jahre), Kauf- und Verkaufszone."""

from __future__ import annotations

from alpha_scout.analysis import metrics as m
from alpha_scout.analysis.valuation_math import expected_cagr, implied_growth, scenarios
from alpha_scout.config import ValuationConfig
from alpha_scout.models import AgentResult, Fact

NAME = "valuation"
SRC = "Alpha Scout Bewertungsmodell (aus Yahoo-Daten)"


def valuate(
    subject: str,
    *,
    price: float,
    currency: str,
    market_cap: float | None,
    fcf: float | None,
    fx_fin_to_quote: float | None,
    revenue_cagr: float | None,
    cfg: ValuationConfig,
) -> AgentResult:
    res = AgentResult(agent=NAME, subject=subject)
    if not market_cap or fcf is None or fx_fin_to_quote is None:
        res.findings.append("Bewertung nicht möglich: Marktkapitalisierung oder freier Cashflow fehlt.")
        return res
    fcf_q = fcf * fx_fin_to_quote
    if fcf_q <= 0:
        res.findings.append("Bewertung nicht möglich: freier Cashflow negativ – kein FCF-Modell anwendbar.")
        res.risks.append("Negativer freier Cashflow")
        res.score = 25.0
        res.facts.append(Fact(id="val.fcf_negative", label="Freier Cashflow negativ", value=True, source=SRC))
        return res

    growth = revenue_cagr if revenue_cagr is not None else 5.0
    sc = scenarios(fcf_q, growth, cfg)
    per = {k: s.per_share(price, market_cap) for k, s in sc.items()}
    fair_base = per["base"][0]
    buy_max = fair_base * (1 - cfg.safety_margin)
    sell_min = per["bull"][0]
    implied = implied_growth(fcf_q, market_cap, cfg)
    fcf_yield = fcf_q / market_cap * 100
    exp10 = expected_cagr(price, per["base"][2], 10)

    def add(fid: str, label: str, value: float | None, unit: str = currency) -> None:
        if value is not None:
            res.facts.append(Fact(id=fid, label=label, value=round(value, 2), unit=unit, source=SRC))

    add("val.fcf_yield", "Free-Cashflow-Rendite", fcf_yield, "%")
    add("val.growth_base", "Annahme FCF-Wachstum Basis (J. 1–5)", sc["base"].growth_pct, "%")
    add("val.implied_growth", "Vom Kurs eingepreistes FCF-Wachstum", implied, "%")
    for k, label in (("bear", "Bär"), ("base", "Basis"), ("bull", "Bulle")):
        add(f"val.fair_{k}", f"Fairer Wert heute ({label})", per[k][0])
        add(f"val.target3y_{k}", f"Wert in 3 Jahren ({label})", per[k][1])
        add(f"val.target10y_{k}", f"Wert in 10 Jahren ({label})", per[k][2])
    add("val.buy_zone_max", "Kaufzone bis", buy_max)
    add("val.sell_zone_min", "Verkaufszone ab", sell_min)
    add("val.expected_cagr_10y", "Erwartete Rendite p. a. 10 J. (Basis, ohne Dividende)", exp10, "%")
    upside = (fair_base / price - 1) * 100
    add("val.upside_base", "Abstand zum fairen Wert (Basis)", upside, "%")
    in_buy = price <= buy_max
    in_sell = price >= sell_min
    res.facts.append(Fact(id="val.in_buy_zone", label="Kurs in Kaufzone", value=in_buy, source=SRC))
    res.facts.append(Fact(id="val.in_sell_zone", label="Kurs in Verkaufszone", value=in_sell, source=SRC))
    res.score = round(m.clamp(50 + upside * 1.0), 1)
    res.findings.append(
        f"Fairer Wert Basis {fair_base:.2f} {currency} (Bär {per['bear'][0]:.2f} / Bulle {per['bull'][0]:.2f}); "
        f"Kaufzone ≤ {buy_max:.2f}, Verkaufszone ≥ {sell_min:.2f}"
    )
    if implied is not None and revenue_cagr is not None and implied > revenue_cagr + 10:
        res.risks.append(
            f"Kurs preist {implied:.0f} % FCF-Wachstum ein – deutlich über dem bisherigen Umsatzwachstum"
        )
    res.data = {
        "fair": {k: v[0] for k, v in per.items()},
        "target3y": {k: v[1] for k, v in per.items()},
        "target10y": {k: v[2] for k, v in per.items()},
        "buy_max": buy_max,
        "sell_min": sell_min,
        "in_buy_zone": in_buy,
        "in_sell_zone": in_sell,
        "currency": currency,
    }
    return res
