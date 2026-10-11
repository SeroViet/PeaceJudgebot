import pytest

from alpha_scout.agents.valuation import valuate
from alpha_scout.analysis import metrics as m
from alpha_scout.analysis.valuation_math import expected_cagr, implied_growth, project_fcf, scenarios
from alpha_scout.config import ValuationConfig


def test_cagr_and_series():
    assert m.cagr(100, 133.1, 3) == pytest.approx(10.0, abs=0.01)
    assert m.cagr(0, 100, 3) is None
    s = [("2022", 100.0), ("2023", 110.0), ("2024", 121.0), ("2025", 133.1)]
    g, years = m.series_cagr(s, 3)
    assert years == 3 and g == pytest.approx(10.0, abs=0.01)
    assert m.series_cagr([("2025", 1.0)]) == (None, 0)


def test_balance_and_dilution():
    assert m.net_debt(100, 30) == 70
    assert m.net_debt_to_ebitda(70, 35) == 2
    assert m.net_debt_to_ebitda(70, -1) is None
    shares = [("2022", 100.0), ("2025", 115.76)]
    assert m.dilution_per_year(shares) == pytest.approx(15.76, abs=0.01)  # nur 1 Intervall -> 1 Jahr
    shares3 = [("2022", 100.0), ("2023", 105.0), ("2024", 110.25), ("2025", 115.76)]
    assert m.dilution_per_year(shares3) == pytest.approx(5.0, abs=0.01)
    assert m.ttm([("q", 1.0)] * 3) is None
    assert m.ttm([("q", 1.0)] * 5) == 4.0


def test_interp_clamps():
    pts = [(0, 0), (10, 100)]
    assert m.interp(-5, pts) == 0
    assert m.interp(5, pts) == 50
    assert m.interp(50, pts) == 100


def test_scenarios_ordered_and_implied_growth_roundtrip():
    cfg = ValuationConfig()
    sc = scenarios(100.0, 12.0, cfg)
    assert sc["bear"].equity_value_now < sc["base"].equity_value_now < sc["bull"].equity_value_now
    assert len(project_fcf(1.0, 0.1, 10, 0.5)) == 10
    mcap = sc["base"].equity_value_now
    assert implied_growth(100.0, mcap, cfg) == pytest.approx(12.0, abs=0.05)
    assert implied_growth(-1, mcap, cfg) is None
    assert expected_cagr(100, 200, 10) == pytest.approx(7.18, abs=0.01)


def test_growth_is_capped():
    cfg = ValuationConfig(growth_cap=0.25)
    sc = scenarios(100.0, 80.0, cfg)
    assert sc["base"].growth_pct == pytest.approx(25.0)
    assert sc["bull"].growth_pct <= 30.0 + 1e-9


def test_valuate_company_level_is_adr_safe():
    cfg = ValuationConfig()
    # gleiche Firma, Kurs pro ADR doppelt so hoch -> fairer Wert pro ADR auch doppelt
    r1 = valuate(
        "X", price=50, currency="USD", market_cap=1e10, fcf=5e8, fx_fin_to_quote=1.0, revenue_cagr=10, cfg=cfg
    )
    r2 = valuate(
        "X",
        price=100,
        currency="USD",
        market_cap=1e10,
        fcf=5e8,
        fx_fin_to_quote=1.0,
        revenue_cagr=10,
        cfg=cfg,
    )
    assert r2.data["fair"]["base"] == pytest.approx(2 * r1.data["fair"]["base"])
    assert r1.score == r2.score
    assert r1.data["buy_max"] < r1.data["fair"]["base"] < r1.data["sell_min"]


def test_valuate_negative_or_missing_fcf():
    cfg = ValuationConfig()
    neg = valuate(
        "X", price=10, currency="USD", market_cap=1e9, fcf=-1e7, fx_fin_to_quote=1.0, revenue_cagr=30, cfg=cfg
    )
    assert neg.score == 25.0 and neg.risks
    miss = valuate(
        "X", price=10, currency="USD", market_cap=None, fcf=1e7, fx_fin_to_quote=1.0, revenue_cagr=30, cfg=cfg
    )
    assert miss.score is None


def test_valuate_converts_financial_currency():
    cfg = ValuationConfig()
    usd = valuate(
        "X", price=10, currency="USD", market_cap=1e9, fcf=1e8, fx_fin_to_quote=1.0, revenue_cagr=5, cfg=cfg
    )
    twd = valuate(
        "X",
        price=10,
        currency="USD",
        market_cap=1e9,
        fcf=3.2e9,
        fx_fin_to_quote=1 / 32,
        revenue_cagr=5,
        cfg=cfg,
    )
    assert twd.data["fair"]["base"] == pytest.approx(usd.data["fair"]["base"])
