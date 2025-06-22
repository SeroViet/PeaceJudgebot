import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import os

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Neue Nachricht: {update.message.text}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="👋 Ich bin online, PeaceJudge!")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

