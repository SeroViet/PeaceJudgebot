from datetime import date

from alpha_scout.agents.portfolio import PortfolioSnapshot, PositionValue
from alpha_scout.agents.quality import apply_moat, check_quality, to_result
from alpha_scout.agents.risk import assess_risk
from alpha_scout.config import PortfolioRules, Position
from alpha_scout.llm.prompts import Claim, MoatAssessment

from .conftest import sheet_from

GOOD = {
    "fund.net_income_ttm": 1e9,
    "fund.revenue_cagr_3y": 15.0,
    "fund.net_debt": -5e8,
    "fund.net_debt_ebitda": -0.5,
    "fund.dilution_3y": 0.5,
    "price.usd": 120.0,
    "mcap.usd": 5e10,
    "fund.operating_margin": 30.0,
}


def test_quality_good_company_passes_with_moat():
    sheet = sheet_from(GOOD)
    q = check_quality(sheet, ["Berkshire Hathaway (Buffett)"], [])
    assert q.criteria[1] and q.criteria[2] and q.criteria[4] and q.criteria[5] and q.criteria[6]
    moat = MoatAssessment(
        moat="stark",
        moat_types=["marktfuehrer"],
        evidence=[
            Claim(text="Hohe Marge", fact_ids=["fund.operating_margin"]),
            Claim(text="Wachstum", fact_ids=["fund.revenue_cagr_3y"]),
        ],
        concerns=[],
    )
    apply_moat(q, moat, set(sheet.facts))
    assert q.criteria[3] is True
    assert q.passed and q.passed_count == 6
    res = to_result("TEST", q, [], [])
    assert not res.veto and res.score == 100


def test_moat_without_valid_evidence_does_not_count():
    sheet = sheet_from(GOOD)
    q = check_quality(sheet, [], [])
    moat = MoatAssessment(
        moat="stark",
        moat_types=[],
        evidence=[Claim(text="erfunden", fact_ids=["gibt.es.nicht"])],
        concerns=[],
    )
    apply_moat(q, moat, set(sheet.facts))
    assert q.criteria[3] is False


def test_penny_stock_and_small_loss_maker_excluded():
    sheet = sheet_from(
        {"fund.net_income_ttm": -1e7, "price.usd": 3.0, "mcap.usd": 4e8, "fund.forward_eps": -0.2}
    )
    q = check_quality(sheet, [], [])
    assert any("Penny" in e for e in q.exclusions)
    assert any("1 Mrd" in e for e in q.exclusions)
    res = to_result("X", q, [], [])
    assert res.veto and res.score == 0


def test_path_to_profitability_counts():
    sheet = sheet_from(
        {"fund.net_income_ttm": -1e7, "fund.forward_eps": 0.5, "price.usd": 20, "mcap.usd": 5e9}
    )
    q = check_quality(sheet, [], [])
    assert q.criteria[1] is True


def test_missing_data_counts_as_not_fulfilled():
    q = check_quality(sheet_from({}), [], [])
    assert q.passed_count == 0 and not q.passed


def test_fraud_flag_is_exclusion():
    q = check_quality(sheet_from(GOOD), ["Laufendes SEC-Verfahren: XY"], [])
    q2 = check_quality(sheet_from(GOOD), [], ["Laufendes SEC-Verfahren: XY"])
    assert not q2.passed and q2.exclusions
    assert q.exclusions == []


def _snap(positions, cash=0.0):
    return PortfolioSnapshot(positions=positions, cash_chf=cash)


def _pv(ticker, bucket, value, sector=None):
    pos = Position(
        ticker=ticker, shares=1, buy_price=1, currency="CHF", buy_date=date(2026, 1, 1), bucket=bucket
    )
    return PositionValue(pos, 1.0, "CHF", value, sector, None, None)


def test_risk_single_stock_limit_veto():
    snap = _snap([_pv("AAA", "qualitaet", 4.5), _pv("CORE", "kern", 95.5)])
    res = assess_risk("AAA", sheet_from({}), bucket="qualitaet", snap=snap, rules=PortfolioRules())
    assert res.veto and res.data["allocation"] and not res.data["hard"]


def test_risk_balance_sheet_and_geopolitics():
    rules = PortfolioRules(veto_countries=["Russia"])
    res = assess_risk(
        "X", sheet_from({"fund.net_debt_ebitda": 3.5}), bucket="qualitaet", snap=_snap([]), rules=rules
    )
    assert res.veto and "Bilanzrisiko" in res.data["hard"][0]
    res2 = assess_risk(
        "X", sheet_from({"profile.country": "Russia"}), bucket="qualitaet", snap=_snap([]), rules=rules
    )
    assert res2.veto


def test_risk_rest_bucket_limit_and_leverage_names():
    snap = _snap([_pv("BTC", "rest", 9.5), _pv("CORE", "kern", 90.5)])
    res = assess_risk("ETH", sheet_from({}), bucket="rest", snap=snap, rules=PortfolioRules())
    assert res.veto
    lev = assess_risk(
        "X", sheet_from({}), bucket="rest", snap=_snap([]), rules=PortfolioRules(), name="Leveraged 3x Nasdaq"
    )
    assert lev.veto


def test_risk_sector_cluster_penalty():
    snap = _snap(
        [_pv("A", "qualitaet", 2, "Technology"), _pv("B", "qualitaet", 1, "Health"), _pv("C", "kern", 97)]
    )
    res = assess_risk(
        "D",
        sheet_from({"profile.sector": "Technology"}),
        bucket="qualitaet",
        snap=snap,
        rules=PortfolioRules(),
    )
    assert any("Klumpenrisiko" in r for r in res.risks)
