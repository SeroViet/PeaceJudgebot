from alpha_scout.agents.insider import analyze_trades
from alpha_scout.agents.superinvestor import diff_holdings, find_clusters, holders_score
from alpha_scout.config import SuperinvestorConfig
from alpha_scout.data.sec_edgar import (
    Holding13F,
    InsiderTrade,
    normalize_name,
    parse_13f_infotable,
    parse_form4,
)

from .conftest import FIXTURES


def test_parse_form4():
    trades = parse_form4((FIXTURES / "form4_buy.xml").read_text(), "2026-09-17", "https://sec/x.xml")
    assert len(trades) == 2
    buy = trades[0]
    assert buy.code == "P" and buy.owner == "MUSTER ANNA" and buy.issuer_ticker == "BSP"
    assert buy.value == 50250.0 and buy.title == "Chief Executive Officer"
    assert trades[1].price is None  # Preis 0 = Zuteilung, kein Kauf


def test_parse_13f_aggregates_and_skips_options():
    rows = parse_13f_infotable((FIXTURES / "infotable.xml").read_text())
    by = {h.cusip: h for h in rows}
    assert by["000000AAA"].value_usd == 800_000_000 and by["000000AAA"].shares == 1_400_000
    assert by["000000BBB"].value_usd == 200_000_000  # Call-Option ignoriert


def _trade(owner, code="P", shares=1000, price=50.0):
    return InsiderTrade("BSP", owner, "1", "CEO", code, shares, price, "2026-09-15", "2026-09-17", "u")


def test_insider_two_buyers_strong_signal():
    res = analyze_trades("BSP", [_trade("A"), _trade("B"), _trade("A")])
    assert res.score == 85.0 and len(res.data["buyers"]) == 2


def test_insider_small_buys_and_non_us():
    assert analyze_trades("BSP", [_trade("A", shares=10)]).score is None  # < 10'000 USD
    assert analyze_trades("NESN.SW", None).score is None
    one = analyze_trades("BSP", [_trade("A"), _trade("B", code="S")])
    assert one.score == 65.0


def test_13f_diff_new_and_increase():
    cfg = SuperinvestorConfig(big_new_position_pct=3, big_increase_pct=50, big_increase_min_weight=1)
    prev = [Holding13F("OLD", "C1", 50.0, 100), Holding13F("KEEP", "C2", 50.0, 100)]
    cur = [
        Holding13F("OLD", "C1", 40.0, 200),
        Holding13F("KEEP", "C2", 50.0, 100),
        Holding13F("NEW", "C3", 10.0, 10),
        Holding13F("TINY", "C4", 0.5, 1),
    ]
    changes = diff_holdings("Inv", "1", cur, prev, "2026-06-30", "2026-08-14", cfg)
    kinds = {(c.issuer, c.action) for c in changes}
    assert ("NEW", "new") in kinds and ("OLD", "increase") in kinds
    assert all(c.issuer != "TINY" for c in changes)  # < 3 % Gewicht
    assert all(c.issuer != "KEEP" for c in changes)
    # Erste Meldung ohne Vorquartal: keine "neuen" Positionen
    assert diff_holdings("Inv", "1", cur, None, "p", "f", cfg) == []


def test_clusters_and_score():
    cfg = SuperinvestorConfig()
    a = diff_holdings("A", "1", [Holding13F("X", "CX", 10, 1)], [Holding13F("Y", "CY", 1, 1)], "p", "f", cfg)
    b = diff_holdings("B", "2", [Holding13F("X", "CX", 10, 1)], [Holding13F("Y", "CY", 1, 1)], "p", "f", cfg)
    c = diff_holdings("C", "3", [Holding13F("X", "CX", 10, 1)], [Holding13F("Y", "CY", 1, 1)], "p", "f", cfg)
    assert "CX" in find_clusters(a + b + c, 3)
    assert find_clusters(a + b, 3) == {}
    assert holders_score(0, 0) is None
    assert holders_score(3, 1) == 95


def test_normalize_name():
    assert normalize_name("Microsoft Corp.") == normalize_name("MICROSOFT CORPORATION")
    assert normalize_name("Amazon.com, Inc.") == "AMAZON"
