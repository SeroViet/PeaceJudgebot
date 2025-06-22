from fastapi import FastAPI, Request
import httpx
import os

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

@app.post("/webhook")
async def webhook_listener(request: Request):
    data = await request.json()
    message = "🚨 Neuer Buy erkannt!\n"

    if "type" in data:
        message += f"Typ: {data['type']}\n"
    if "signature" in data:
        message += f"Signature: {data['signature'][:8]}..."

    await send_telegram_message(message)
    return {"status": "ok"}

async def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    async with httpx.AsyncClient() as client:
        await client.post(url, data=payload)

