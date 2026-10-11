"""Textberichte für Telegram: /check, /holdings, /watchlist, /top, /costs, Monatsbericht."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alpha_scout.alerts import esc
from alpha_scout.models import AssetKind

if TYPE_CHECKING:
    from alpha_scout.agents.portfolio import PortfolioSnapshot
    from alpha_scout.config import PortfolioRules
    from alpha_scout.pipeline import Analysis


def verdict(a: Analysis) -> str:
    held = bool(a.sheet.value("port.held"))
    if a.vetoes:
        reasons = "; ".join(a.vetoes)[:300]
        return f"❌ {'Nicht nachkaufen' if held else 'Keine Empfehlung'}: {esc(reasons)}"
    if a.sheet.value("port.buy_stop"):
        return f"⏸ Kaufstopp: {esc(a.sheet.value('port.buy_stop'))}"
    s = a.score or 0
    if held:
        if a.sheet.value("val.in_sell_zone"):
            return "💰 Halten, Verkauf prüfen (Kurs in Verkaufszone)"
        return "✋ Halten" + (" – Nachkauf möglich" if s >= 70 and a.sheet.value("val.in_buy_zone") else "")
    if s >= 80:
        return "💎 Kaufkandidat – in Tranchen kaufen"
    if s >= 65:
        return "👀 Beobachten (Watchlist)"
    return "⏸ Derzeit kein Kauf"


def _line(a: Analysis, fid: str) -> str | None:
    f = a.sheet.get(fid)
    if f is None or f.value is None:
        return None
    if fid.startswith(("news.", "proj.")):
        date_txt = f" {f.as_of.strftime('%d.%m.')}" if f.as_of else ""
        return f"• {esc(f.label)} <i>({esc(f.source)}{date_txt})</i>"
    return f"• {esc(f.display())}"


def _section(title: str, lines: list[str | None]) -> str | None:
    rows = [line for line in lines if line]
    return f"<b>{title}</b>\n" + "\n".join(rows) if rows else None


def format_check(a: Analysis) -> str:
    if a.error:
        return f"❓ {esc(a.subject)}: {esc(a.error)}"
    head = f"🔎 <b>{esc(a.name)} ({esc(a.subject)})</b>"
    if a.score is not None:
        head += f" – Score <b>{a.score:.0f}</b>/100"
    parts: list[str | None] = [head, verdict(a)]
    parts.append(
        _section(
            "Kurs",
            [
                _line(a, "price.last"),
                _line(a, "price.drawdown_52w"),
                _line(a, "mcap.value"),
                f"• Swissquote {a.swissquote.value} {esc(a.swissquote_note)}",
            ],
        )
    )
    if a.kind == AssetKind.STOCK:
        q = a.results.get("quality")
        if q:
            parts.append(
                _section(
                    f"Qualitätsfilter ({a.sheet.value('quality.count')}/6)", [esc(x) for x in q.findings[:8]]
                )
            )
        parts.append(
            _section(
                "Fundamentaldaten",
                [
                    _line(a, k)
                    for k in (
                        "fund.revenue_cagr_3y",
                        "fund.operating_margin",
                        "fund.fcf_margin",
                        "fund.net_debt_ebitda",
                        "fund.dilution_3y",
                        "fund.backlog",
                    )
                ],
            )
        )
        val_lines = [
            _line(a, k)
            for k in (
                "val.fair_bear",
                "val.fair_base",
                "val.fair_bull",
                "val.buy_zone_max",
                "val.sell_zone_min",
                "val.target3y_base",
                "val.target10y_base",
                "val.implied_growth",
                "val.expected_cagr_10y",
            )
        ]
        if not a.sheet.get("val.fair_base") and a.results.get("valuation"):
            val_lines += [esc(x) for x in a.results["valuation"].findings[:1]]
        parts.append(_section("Bewertung", val_lines))
        parts.append(
            _section(
                "Signale",
                [
                    _line(a, "si.holders"),
                    _line(a, "si.recent_buys"),
                    _line(a, "ins.buyers_30d"),
                    _line(a, "ins.latest_buy"),
                ]
                + [_line(a, f"proj.{k}") for k in range(2)],
            )
        )
    elif a.kind == AssetKind.ETF:
        etf_lines = [_line(a, k) for k in ("etf.ter", "etf.aum", "etf.domicile", "etf.return_1y", "etf.rank")]
        if "etf" in a.results:
            etf_lines += [esc(x) for x in a.results["etf"].findings[:2]]
        parts.append(_section("ETF", etf_lines))
    else:
        parts.append(
            _section(
                "Krypto",
                [
                    _line(a, k)
                    for k in (
                        "crypto.mcap_usd",
                        "crypto.drawdown_ath",
                        "crypto.change_1y",
                        "crypto.fees_30d",
                        "crypto.fees_trend",
                        "crypto.eligible",
                    )
                ],
            )
        )
    news = [_line(a, f"news.{k}") for k in range(4)]
    parts.append(_section("News (14 Tage)", news))
    risks: list[str] = []
    for key in ("risk", "valuation", "quality", "etf"):
        r = a.results.get(key)
        if r:
            risks += r.risks
    parts.append(_section("Risiken", [f"• {esc(x)}" for x in dict.fromkeys(risks)][:6]))
    dv = a.results.get("devils_advocate")
    if dv:
        parts.append(_section("Gegenargumente (Advocatus Diaboli)", [f"• {esc(x)}" for x in dv.risks[:5]]))
    held = a.sheet.value("port.held")
    if held:
        parts.append(
            _section(
                "Dein Bestand",
                [
                    _line(a, "port.buy_price"),
                    _line(a, "port.buy_date"),
                    _line(a, "port.current_pct"),
                    _line(a, "port.thesis"),
                ],
            )
        )
    sources = sorted(
        {f.source for f in a.sheet.facts.values() if not f.source.startswith(("Alpha Scout", "Orchestrator"))}
    )
    parts.append("<b>Quellen</b>\n" + esc(", ".join(sources)))
    parts.append("<i>Keine Anlageberatung.</i>")
    return "\n\n".join(p for p in parts if p)


def format_holdings(snap: PortfolioSnapshot, rules: PortfolioRules, min_days: int, base: str = "CHF") -> str:
    from datetime import date, timedelta

    if not snap.positions:
        return "📂 Noch keine Positionen. Erfassen mit /buy TICKER STÜCK PREIS [WÄHRUNG] [kern|qualitaet|rest] [These]"
    lines = [f"📂 <b>Depot</b> – {snap.total_chf:,.0f} {base}".replace(",", "'")]
    for p in sorted(snap.positions, key=lambda x: -(x.value_chf or 0)):
        pos = p.position
        val = f"{p.value_chf:,.0f} {base}".replace(",", "'") if p.value_chf is not None else "Kurs fehlt"
        pnl = f" ({p.pnl_pct:+.1f} %)" if p.pnl_pct is not None else ""
        lock = ""
        if p.holding_days < min_days:
            lock = f" 🔒 bis {(pos.buy_date + timedelta(days=min_days)).strftime('%d.%m.%Y')}"
        lines.append(
            f"• <b>{esc(pos.ticker)}</b> {pos.shares:g} Stk – {val}{pnl}, {snap.pct(p.value_chf or 0):.1f} %{lock}"
        )
    lines.append("")
    lines.append(
        f"Kern {snap.bucket_pct('kern'):.0f} % (Ziel {rules.core_target_pct:.0f}) · "
        f"Qualität {snap.bucket_pct('qualitaet'):.0f} % (max {rules.quality_max_pct:.0f}) · "
        f"Rest {snap.bucket_pct('rest'):.0f} % (max {rules.rest_max_pct:.0f})"
    )
    if snap.cash_chf:
        lines.append(f"Cash: {snap.cash_chf:,.0f} {base}".replace(",", "'"))
    if snap.missing_prices:
        lines.append("Ohne Kurs: " + ", ".join(snap.missing_prices))
    _ = date
    return "\n".join(lines)
