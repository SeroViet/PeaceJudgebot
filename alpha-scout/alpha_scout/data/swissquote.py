"""Handelbarkeit bei Swissquote (keine offizielle API -> kuratierte Liste + Börsenregel)."""

from __future__ import annotations

from alpha_scout.config import SwissquoteUniverse
from alpha_scout.models import AssetKind, SwissquoteStatus


def suffix_of(ticker: str) -> str:
    if "." not in ticker:
        return ""
    return "." + ticker.rsplit(".", 1)[1].upper()


def check_swissquote(
    ticker: str, kind: AssetKind, universe: SwissquoteUniverse, exchange_code: str | None = None
) -> tuple[SwissquoteStatus, str]:
    """Gibt (Status, Hinweis) zurück."""
    if kind == AssetKind.CRYPTO:
        if ticker.upper() in [c.upper() for c in universe.crypto_confirmed]:
            return SwissquoteStatus.CONFIRMED, "von dir bestätigt"
        return SwissquoteStatus.LIKELY, "Krypto-Angebot bei Swissquote bitte prüfen"
    if ticker.upper() in [c.upper() for c in universe.confirmed]:
        return SwissquoteStatus.CONFIRMED, "von dir bestätigt"
    sfx = suffix_of(ticker)
    exchanges = {k.upper(): v for k, v in universe.exchanges.items()}
    if (
        sfx == ""
        and exchange_code
        and exchange_code.upper() in [e.upper() for e in universe.excluded_us_exchanges]
    ):
        return SwissquoteStatus.NO, f"US-OTC ({exchange_code}) – Primärbörse oder ETF wählen"
    if sfx.upper() in exchanges:
        return (
            SwissquoteStatus.LIKELY,
            f"wahrscheinlich handelbar ({exchanges[sfx.upper()]}) – bitte bestätigen",
        )
    return SwissquoteStatus.NO, "Börse nicht im Swissquote-Raster – ADR oder ETF prüfen"
