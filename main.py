from telegram.ext import Updater, CommandHandler
import os

TOKEN = os.environ.get("TELEGRAM_TOKEN")

def start(update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="Hello from PeaceJudgeBot!")

updater = Updater(token=TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler('start', start))
updater.start_polling()
