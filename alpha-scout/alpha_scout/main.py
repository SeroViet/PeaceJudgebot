"""Startpunkt: Telegram-Bot + Scheduler in einer asyncio-Schleife."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from alpha_scout.alerts import Dispatcher
from alpha_scout.config import Settings
from alpha_scout.context import Context, build_context
from alpha_scout.jobs import Jobs
from alpha_scout.logging_setup import setup_logging

log = logging.getLogger("alpha_scout")


def schedule(scheduler: AsyncIOScheduler, jobs: Jobs, tz: str) -> None:
    def add(name: str, fn, trigger: CronTrigger) -> None:
        async def run() -> None:
            await jobs.guarded(name, fn)

        scheduler.add_job(run, trigger, id=name, max_instances=1, coalesce=True, misfire_grace_time=1800)

    # Intraday: News, Kurse, Bestand (Mo–Fr 07–23 Uhr alle 30 Min., Wochenende alle 3 Std. für Krypto/News)
    add(
        "intraday", jobs.intraday, CronTrigger(day_of_week="mon-fri", hour="7-23", minute="5,35", timezone=tz)
    )
    add(
        "intraday_weekend",
        jobs.intraday,
        CronTrigger(day_of_week="sat,sun", hour="8-22/3", minute=5, timezone=tz),
    )
    # Nachtlauf mit Batch-API (50 % günstiger); Ergebnisse alle 15 Min. abholen
    add("nightly", jobs.nightly_submit, CronTrigger(hour=2, minute=0, timezone=tz))
    add("batch_poll", jobs.poll_batches, CronTrigger(minute="*/15", timezone=tz))
    # Wöchentlich: Ausschreibungen, ETF-Ranking (Sonntag früh, vor dem Nachtlauf mit Universum)
    add("weekly", jobs.weekly, CronTrigger(day_of_week="sun", hour=1, minute=0, timezone=tz))
    # 13F: in der Meldesaison täglich, sonst wöchentlich
    add(
        "superinvestor_season",
        jobs.superinvestors,
        CronTrigger(month="2,5,8,11", day="1-25", hour=6, minute=0, timezone=tz),
    )
    add("superinvestor", jobs.superinvestors, CronTrigger(day_of_week="wed", hour=6, minute=30, timezone=tz))
    # Monatsbericht am 1. um 08:00
    add("monthly", jobs.monthly_report, CronTrigger(day=1, hour=8, minute=0, timezone=tz))
    # Gesammelte Meldungen nach den Ruhezeiten senden
    add("flush", jobs.dispatcher.flush_pending, CronTrigger(minute="0,30", timezone=tz))


async def bootstrap(ctx: Context, scheduler: AsyncIOScheduler, jobs: Jobs) -> None:
    """Erster Start: Datenbank leer -> sofort eine Analyse-Runde, damit /top und /watchlist gefüllt sind."""
    row = await ctx.db.fetchone("SELECT COUNT(*) AS n FROM analyses")
    if row and row["n"] > 0:
        return
    log.info("Erster Start – Analyse-Runde und 13F-Abgleich in 1 Minute")

    async def first_run() -> None:
        await jobs.guarded("superinvestor", jobs.superinvestors)
        await jobs.guarded("nightly", lambda: jobs.nightly_submit(include_universe=False))

    scheduler.add_job(first_run, "date", run_date=ctx.now() + timedelta(minutes=1), id="bootstrap")


async def run_bot(ctx: Context) -> None:
    s = ctx.settings
    ctx.flags.started_at = ctx.now().strftime("%d.%m.%Y %H:%M")
    app = None
    sender = None
    if s.telegram_bot_token and s.telegram_chat_id:
        from alpha_scout.telegram.bot import build_application, make_sender
        from alpha_scout.telegram.commands import Commands

        dispatcher = Dispatcher(ctx, None)
        jobs = Jobs(ctx, dispatcher)
        app = build_application(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id, Commands(jobs))
        sender = make_sender(app, s.telegram_chat_id)
        dispatcher.sender = sender
    else:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fehlen – Meldungen nur im Log")
        dispatcher = Dispatcher(ctx, None)
        jobs = Jobs(ctx, dispatcher)
    if not s.sec_edgar_user_agent:
        log.warning("SEC_EDGAR_USER_AGENT fehlt – SEC kann Anfragen blockieren")

    scheduler = AsyncIOScheduler(timezone=s.timezone)
    schedule(scheduler, jobs, s.timezone)
    scheduler.start()
    await bootstrap(ctx, scheduler, jobs)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    if app is not None:
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            await dispatcher.send_text("🟢 Alpha Scout gestartet. /help für Befehle.")
            log.info("Alpha Scout läuft")
            await stop.wait()
            await app.updater.stop()
            await app.stop()
    else:
        log.info("Alpha Scout läuft (ohne Telegram)")
        await stop.wait()
    scheduler.shutdown(wait=False)
    await ctx.close()


async def run_once(ctx: Context, job: str, arg: str | None) -> None:
    """Einzelnen Job manuell ausführen (Test/Debug), Ausgabe ins Log/Telegram."""
    from alpha_scout.report import format_check

    sender = None
    s = ctx.settings
    if s.telegram_bot_token and s.telegram_chat_id:
        from telegram import Bot
        from telegram.constants import ParseMode

        bot = Bot(s.telegram_bot_token.get_secret_value())

        async def sender(text: str) -> None:  # noqa: F811
            async with bot:
                await bot.send_message(
                    chat_id=s.telegram_chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

    jobs = Jobs(ctx, Dispatcher(ctx, sender))
    if job == "check":
        a = await jobs.pipeline.analyze_live(arg or "MSFT")
        print(format_check(a))
    else:
        fn = {
            "intraday": jobs.intraday,
            "nightly": jobs.nightly_submit,
            "poll": jobs.poll_batches,
            "weekly": jobs.weekly,
            "superinvestor": jobs.superinvestors,
            "monthly": jobs.monthly_report,
        }[job]
        await fn()
    await ctx.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="alpha_scout", description="Alpha Scout – Langfrist-Investment-Scout"
    )
    parser.add_argument(
        "--once",
        choices=["check", "intraday", "nightly", "poll", "weekly", "superinvestor", "monthly"],
        help="einen Job einmal ausführen und beenden",
    )
    parser.add_argument("arg", nargs="?", help="Ticker für --once check")
    args = parser.parse_args()
    settings = Settings()
    setup_logging(settings.log_level, settings.secrets())

    async def runner() -> None:
        ctx = await build_context(settings)
        if args.once:
            await run_once(ctx, args.once, args.arg)
        else:
            await run_bot(ctx)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
