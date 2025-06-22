import asyncio
import websockets
import json
import os
from telegram import Bot

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PUMP_COIN_ID = "DA3pMLg1X3HWfeBb6kzsxqLKh5XMwsVUY5wCTEsqpump"

bot = Bot(token=TOKEN)

async def listen_to_buys():
    url = f"wss://pump.fun/ws/{PUMP_COIN_ID}"

    try:
        async with websockets.connect(url) as ws:
            print("✅ Verbunden mit Pump.fun WebSocket")

            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)

                    if "buyer" in data and "amount" in data:
                        buyer = data["buyer"]
                        amount = round(float(data["amount"]), 4)

                        text = f"🚀 Neuer BUY: {amount} SOL von {buyer}"
                        await bot.send_message(chat_id=CHAT_ID, text=text)
                        print("📩 Nachricht gesendet:", text)

                except Exception as e:
                    print("⚠️ Fehler beim Lesen der Nachricht:", e)
    except Exception as e:
        print("❌ Verbindung fehlgeschlagen:", e)

if __name__ == "__main__":
    asyncio.run(listen_to_buys())

