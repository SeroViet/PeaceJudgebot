"""Telegram-Anbindung (python-telegram-bot). Antwortet nur dem eigenen Chat."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from alpha_scout.alerts import split_message
from alpha_scout.telegram.commands import HELP, Commands

log = logging.getLogger(__name__)

Handler = Callable[[list[str]], Awaitable[str]]


def build_application(token: str, chat_id: int, commands: Commands) -> Application:
    app = Application.builder().token(token).build()
    only_me = filters.Chat(chat_id=chat_id)

    def wrap(fn: Handler, slow: bool = False):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            if slow:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ Analysiere …")
            try:
                text = await fn(list(context.args or []))
            except Exception as e:
                log.exception("Befehl fehlgeschlagen")
                text = f"❌ Fehler: {type(e).__name__}"
            for chunk in split_message(text):
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

        return handler

    async def help_(args: list[str]) -> str:
        return HELP

    routes: dict[str, tuple[Handler, bool]] = {
        "start": (help_, False),
        "help": (help_, False),
        "check": (commands.check, True),
        "top": (commands.top, False),
        "watchlist": (commands.watchlist, False),
        "holdings": (commands.holdings, False),
        "buy": (commands.buy, False),
        "sell": (commands.sell, False),
        "costs": (commands.costs, False),
        "status": (commands.status, False),
    }
    for name, (fn, slow) in routes.items():
        app.add_handler(CommandHandler(name, wrap(fn, slow), filters=only_me, block=False))
    return app


def make_sender(app: Application, chat_id: int) -> Callable[[str], Awaitable[None]]:
    async def send(text: str) -> None:
        await app.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    return send
