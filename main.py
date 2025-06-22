from telegram.ext import Updater, CommandHandler
import os

# Hol dir den Bot-Token aus der Render-Umgebungsvariable
TOKEN = os.environ.get("TELEGRAM_TOKEN")

# /start-Befehl
def start(update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="👋 Hey! Der PeaceJudgeBot ist online.")

if __name__ == '__main__':
    # Starte den Bot
    updater = Updater(token=TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    # Reagiere auf /start
    dispatcher.add_handler(CommandHandler('start', start))

    print("Bot läuft ...")
    updater.start_polling()
    updater.idle()

