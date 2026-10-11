"""Telegram-Befehle als reine Funktionen (Text rein, Text raus) – gut testbar."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from alpha_scout.agents.portfolio_keeper import add_buy, apply_sell, sell_lock_info
from alpha_scout.alerts import esc
from alpha_scout.jobs import Jobs
from alpha_scout.models import AssetKind
from alpha_scout.pipeline import detect_kind, stock_bucket
from alpha_scout.report import format_check, format_holdings

log = logging.getLogger(__name__)

HELP = """<b>Alpha Scout</b> – Langfrist-Scout für Aktien, ETFs und Krypto

/check TICKER – Vollanalyse (z. B. /check MSFT, /check NESN.SW, /check BTC)
/top – beste Titel der letzten Analysen
/watchlist – Watchlist mit Scores
/holdings – dein Depot
/buy TICKER STÜCK PREIS [WÄHRUNG] [kern|qualitaet|rest] [TT.MM.JJJJ] [These]
/sell TICKER STÜCK [PREIS]
/costs – LLM-Kosten
/status – Systemstatus

Keine Anlageberatung – du entscheidest selbst."""

BUCKETS = ("kern", "qualitaet", "rest")


class Commands:
    def __init__(self, jobs: Jobs):
        self.jobs = jobs
        self.ctx = jobs.ctx

    async def check(self, args: list[str]) -> str:
        if not args:
            return "Bitte Ticker angeben, z. B. /check MSFT"
        a = await self.jobs.pipeline.analyze_live(args[0])
        return format_check(a)

    async def top(self, args: list[str]) -> str:
        rows = await self.ctx.db.top_analyses(10)
        if not rows:
            return "Noch keine Analysen. Der erste Nachtlauf füllt die Liste (oder /check TICKER)."
        lines = ["🏆 <b>Top-Titel</b> (letzte Analysen)"]
        for r in rows:
            p = json.loads(r["payload"])
            veto = " ❌" if p.get("vetoes") else ""
            lines.append(
                f"• <b>{esc(r['subject'])}</b> {esc(p.get('name') or '')[:30]} – {r['score']:.0f}{veto} "
                f"<i>({r['ts'][:10]})</i>"
            )
        return "\n".join(lines)

    async def watchlist(self, args: list[str]) -> str:
        wl = self.ctx.cfg.watchlist
        lines = ["👀 <b>Watchlist</b>"]
        groups = dict(wl.stock_groups())
        groups["krypto"] = [c.upper() for c in wl.krypto]
        for group, tickers in groups.items():
            if not tickers:
                continue
            items = []
            for t in tickers:
                resolved = wl.resolve(t).upper()
                an = await self.ctx.db.get_analysis(resolved)
                score = f" {an['score']:.0f}" if an and an["score"] is not None else ""
                stop = ""
                if group == "kaufstopp":
                    stop = f" ⏸ {wl.kaufstopp.get(t, '')}"
                items.append(f"{esc(t)}{score}{esc(stop)}")
            lines.append(f"<b>{esc(group)}</b>: " + ", ".join(items))
        if wl.etf_kern:
            lines.append(f"<b>etf_kern</b>: {esc(wl.etf_kern)}")
        return "\n".join(lines)

    async def holdings(self, args: list[str]) -> str:
        snap = await self.jobs.pipeline.portfolio(refresh=True)
        return format_holdings(
            snap,
            self.ctx.cfg.scoring.portfolio,
            self.ctx.cfg.scoring.min_holding_days_for_sell,
            self.ctx.settings.base_currency,
        )

    async def buy(self, args: list[str]) -> str:
        if len(args) < 3:
            return "Format: /buy TICKER STÜCK PREIS [WÄHRUNG] [kern|qualitaet|rest] [TT.MM.JJJJ] [These]"
        ticker = self.ctx.cfg.watchlist.resolve(args[0]).upper()
        try:
            shares = float(args[1].replace(",", "."))
            price = float(args[2].replace(",", ".").replace("'", ""))
        except ValueError:
            return "STÜCK und PREIS müssen Zahlen sein."
        if shares <= 0 or price <= 0:
            return "STÜCK und PREIS müssen positiv sein."
        rest = args[3:]
        currency, bucket, buy_date = None, None, self.ctx.today()
        while rest:
            tok = rest[0]
            if re.fullmatch(r"[A-Z]{3}|GBp", tok):
                currency = tok
            elif tok.lower() in BUCKETS:
                bucket = tok.lower()
            elif re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", tok):
                d, m, y = (int(x) for x in tok.split("."))
                buy_date = date(y, m, d)
            else:
                break
            rest = rest[1:]
        thesis = " ".join(rest)
        quote = None
        if ticker not in self.ctx.cfg.crypto.allowlist:
            quote = await self.ctx.market.quote(ticker)
        kind = detect_kind(ticker, self.ctx, quote)
        if currency is None:
            currency = (quote or {}).get("currency") or self.ctx.settings.base_currency
        if bucket is None:
            if kind == AssetKind.CRYPTO:
                bucket = "rest"
            elif kind == AssetKind.ETF:
                cat, _ = self.jobs.etf.category_of(ticker)
                bucket = "kern" if cat in ("msci_world", "schweiz", "usa", "schwellenlaender") else "rest"
            else:
                bucket = stock_bucket(ticker, self.ctx)
        h = self.ctx.cfg.holdings
        pos = add_buy(h, ticker, shares, price, currency, buy_date, bucket, thesis)
        self.ctx.cfg.save_holdings(h)
        lock_until = pos.buy_date + timedelta(days=self.ctx.cfg.scoring.min_holding_days_for_sell)
        msg = (
            f"✅ Kauf erfasst: {shares:g} {esc(ticker)} zu {price:g} {esc(currency)} ({bucket}).\n"
            f"Bestand: {pos.shares:g} Stk, Ø {pos.buy_price:.2f} {esc(pos.currency)}. "
            f"Verkaufssperre bis {lock_until.strftime('%d.%m.%Y')}."
        )
        if not thesis and not pos.thesis:
            msg += "\nTipp: Kaufthese mitgeben, damit der PortfolioKeeper sie überwachen kann."
        return msg

    async def sell(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Format: /sell TICKER STÜCK [PREIS]"
        ticker = self.ctx.cfg.watchlist.resolve(args[0]).upper()
        try:
            shares = float(args[1].replace(",", "."))
        except ValueError:
            return "STÜCK muss eine Zahl sein."
        h = self.ctx.cfg.holdings
        pos = next((p for p in h.positions if p.ticker == ticker), None)
        if pos is None:
            return f"{esc(ticker)} ist nicht im Bestand."
        lock = sell_lock_info(pos, self.ctx.today(), self.ctx.cfg.scoring.min_holding_days_for_sell)
        buy_price = pos.buy_price
        try:
            remaining = apply_sell(h, ticker, shares)
        except ValueError as e:
            return f"❌ {esc(e)}"
        self.ctx.cfg.save_holdings(h)
        msg = f"✅ Verkauf erfasst: {shares:g} {esc(ticker)}."
        if len(args) >= 3:
            try:
                price = float(args[2].replace(",", "."))
                msg += f" Ergebnis ggü. Ø-Kaufkurs: {(price / buy_price - 1) * 100:+.1f} %."
            except ValueError:
                pass
        msg += f" Rest: {remaining.shares:g} Stk." if remaining else " Position geschlossen."
        if lock:
            msg += f"\n⚠️ {esc(lock)} – Achtung Schweizer Steuerregel (gewerbsmässiger Handel)."
        return msg

    async def costs(self, args: list[str]) -> str:
        today = self.ctx.today()
        spent = await self.ctx.db.cost_for_day(today)
        month = await self.ctx.db.costs_by_day(today.replace(day=1))
        by_agent = await self.ctx.db.costs_by_agent(today.replace(day=1))
        lines = [
            "💸 <b>LLM-Kosten</b>",
            f"Heute: ${spent:.3f} von ${self.ctx.settings.daily_llm_budget_usd:.2f} (inkl. Reservierungen)",
            f"Monat: ${sum(r['cost'] for r in month):.2f}",
        ]
        if by_agent:
            lines.append("Nach Agent (Monat):")
            lines += [
                f"• {esc(r['agent'])} ({esc(r['model'])}): ${r['cost']:.3f} / {r['calls']} Aufrufe"
                for r in by_agent[:8]
            ]
        return "\n".join(lines)

    async def status(self, args: list[str]) -> str:
        f = self.ctx.flags
        remaining = await self.ctx.llm.budget.remaining()
        batches = await self.ctx.db.open_batches()
        lines = [
            "🩺 <b>Status</b>",
            f"Läuft seit: {esc(f.started_at)}",
            f"LLM: {'aktiv' if self.ctx.llm.enabled else 'AUS (kein ANTHROPIC_API_KEY)'} – Restbudget heute ${remaining:.2f}",
            f"SEC User-Agent: {'gesetzt' if self.ctx.settings.sec_edgar_user_agent else 'FEHLT'}",
            f"Offene Batches: {len(batches)}",
        ]
        if f.last_runs:
            lines.append(
                "Letzte Läufe: " + ", ".join(f"{esc(k)} {esc(v)}" for k, v in sorted(f.last_runs.items()))
            )
        if f.last_errors:
            lines.append("⚠️ Fehler:")
            lines += [f"• {esc(k)}: {esc(v)}" for k, v in f.last_errors.items()]
        return "\n".join(lines)


def parse_args(text: str) -> list[str]:
    parts = (text or "").split()
    return parts[1:] if parts else []


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")
