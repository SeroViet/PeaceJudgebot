from datetime import date

from alpha_scout.agents.crypto import crypto_score, eligibility
from alpha_scout.agents.etf import EtfInfo, domicile_from_name, etf_score, rank
from alpha_scout.agents.news import has_fraud_keyword, search_term
from alpha_scout.agents.performance import add_months, agent_hit, factor_from_hit_rate
from alpha_scout.agents.project_scout import map_contractor, match_theme
from alpha_scout.config import SwissquoteUniverse, Watchlist
from alpha_scout.data.news_sources import build_or_query
from alpha_scout.data.swissquote import check_swissquote
from alpha_scout.logging_setup import SecretMaskingFilter
from alpha_scout.models import AssetKind, SwissquoteStatus


def test_watchlist_aliases_and_buy_stop():
    wl = Watchlist(
        qualitaet=["MSFT"],
        klein=["Gold"],
        kaufstopp={"R3NK": "bis Q3-Bericht 05.11.2026"},
        aliases={"Gold": "ZGLD.SW", "R3NK": "R3NK.DE"},
    )
    assert wl.resolve("gold") == "ZGLD.SW"
    assert "ZGLD.SW" in wl.all_securities() and "R3NK.DE" in wl.all_securities()
    assert wl.is_buy_stopped("R3NK.DE", date(2026, 10, 11))
    assert not wl.is_buy_stopped("R3NK.DE", date(2026, 11, 6))
    assert wl.group_of("ZGLD.SW") == "klein"


def test_swissquote_rules():
    u = SwissquoteUniverse(
        confirmed=["MSFT"], exchanges={"": "US", ".SW": "SIX"}, excluded_us_exchanges=["PNK"]
    )
    assert check_swissquote("MSFT", AssetKind.STOCK, u)[0] == SwissquoteStatus.CONFIRMED
    assert check_swissquote("NESN.SW", AssetKind.STOCK, u)[0] == SwissquoteStatus.LIKELY
    assert check_swissquote("ABCD", AssetKind.STOCK, u, "PNK")[0] == SwissquoteStatus.NO
    assert check_swissquote("X.KS", AssetKind.STOCK, u)[0] == SwissquoteStatus.NO


def test_crypto_rules():
    assert eligibility(
        "BTC",
        allowlisted=True,
        blocklisted=False,
        history_years=None,
        fees_30d=None,
        min_years=3,
        min_fees=1e6,
    )[0]
    assert not eligibility(
        "DOGE", allowlisted=True, blocklisted=True, history_years=10, fees_30d=1e9, min_years=3, min_fees=1e6
    )[0]
    ok, reasons = eligibility(
        "NEW", allowlisted=False, blocklisted=False, history_years=1, fees_30d=5e6, min_years=3, min_fees=1e6
    )
    assert not ok and "Historie" in reasons[0]
    assert crypto_score(-60, 10, True) > crypto_score(-5, 10, True)
    assert crypto_score(-60, 10, False) == 0


def test_etf_ranking_prefers_cheap_big_ch_ie():
    cheap = EtfInfo("A", "w", "A", "CHF", 1, 0.06, 5e9, "IE", None, None)
    pricey = EtfInfo("B", "w", "B", "CHF", 1, 0.5, 5e8, "LU", None, None)
    assert rank([pricey, cheap])[0][0].ticker == "A"
    assert etf_score(cheap) > 90
    assert domicile_from_name("Swisscanto (CH) Gold ETF") == "CH"
    assert domicile_from_name("iShares Core MSCI World") is None


def test_news_helpers():
    assert search_term("Microsoft Corporation") == "microsoft"
    assert search_term("Kratos Defense & Security Solutions, Inc.") == "kratos"
    assert search_term("Red Cat Holdings, Inc.") == "red cat"
    assert search_term(None) is None
    qs = build_or_query(["alpha", "beta gamma", "delta"], max_len=30)
    assert all(len(q) <= 34 for q in qs) and any('"beta gamma"' in q for q in qs)
    assert has_fraud_keyword("SEC Investigation into X", ["sec investigation"])


def test_project_helpers():
    themes = {"stromnetz": ["substation", "grid"], "ruestung": ["missile"]}
    assert match_theme("New 380kV substation and grid upgrade", themes) == "stromnetz"
    assert match_theme("School renovation", themes) is None
    assert map_contractor("LOCKHEED MARTIN CORP", {"LOCKHEED MARTIN": "LMT"}) == "LMT"


def test_performance_helpers():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert agent_hit(80, "NEW_CHANCE", 5.0) is True
    assert agent_hit(80, "NEW_CHANCE", -5.0) is False
    assert agent_hit(30, "SELL_CHECK", -1.0) is True
    assert agent_hit(None, "NEW_CHANCE", 1.0) is None
    assert factor_from_hit_rate(1.0) == 1.5 and factor_from_hit_rate(0.0) == 0.5


def test_secret_masking():
    f = SecretMaskingFilter(["supersecret123"])
    assert "***" in f.mask("key=supersecret123")
    assert "sk-ant-" not in f.mask("sk-ant-api03-abcdef")
    assert "***" in f.mask("https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/send")
