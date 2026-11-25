import logging
import os
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Model ka naam yahin badalna hai. AI Studio me check kar lena kaun sa available hai.
MODEL = "gemini-2.5-flash"
MAX_HISTORY = 20   # last kitne messages yaad rakhne hain
DAILY_LIMIT = 20   # ek user ke liye din ke max messages

SYSTEM_PROMPT = (
    "Tum ek friendly Telegram assistant ho. User jis language me baat kare "
    "(Hindi, English ya Hinglish), usi me jawab do. Jawab chhote aur clear rakho."
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

# Memory me store hota hai, bot restart pe reset ho jayega (baad me SQLite lagayenge)
history = defaultdict(list)   # user_id -> list of Content
usage = {}                    # user_id -> (date, count)


def within_limit(user_id: int) -> bool:
    today = date.today()
    last_day, count = usage.get(user_id, (today, 0))
    if last_day != today:
        count = 0
    if count >= DAILY_LIMIT:
        return False
    usage[user_id] = (today, count + 1)
    return True


def trim_history(user_id: int) -> None:
    history[user_id] = history[user_id][-MAX_HISTORY:]
    # History hamesha "user" message se shuru honi chahiye
    while history[user_id] and history[user_id][0].role != "user":
        history[user_id].pop(0)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! Main tumhara AI assistant hu. Kuch bhi poochho.\n"
        "/reset se purani baat bhula sakte ho."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history.pop(update.effective_user.id, None)
    await update.message.reply_text("Done, chat history clear ho gayi.")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if not within_limit(user_id):
        await update.message.reply_text(
            f"Aaj ki {DAILY_LIMIT} messages ki limit khatam. Kal phir try karna."
        )
        return

    history[user_id].append(types.Content(role="user", parts=[types.Part(text=text)]))
    trim_history(user_id)

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=history[user_id],
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        reply = response.text or "Kuch samajh nahi aaya, dobara try karo."
    except Exception:
        logger.exception("Gemini API error")
        history[user_id].pop()  # fail hua message history se hata do
        await update.message.reply_text(
            "Abhi AI busy hai ya limit lag gayi hai. Thodi der baad try karo."
        )
        return

    history[user_id].append(types.Content(role="model", parts=[types.Part(text=reply)]))

    # Telegram ek message me max 4096 characters allow karta hai
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i : i + 4000])


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, *args):
        pass  # logs shant rakho


def start_health_server():
    # Render web service ko ek open port chahiye. PORT env var Render khud deta hai.
    port = os.getenv("PORT")
    if not port:
        return  # local pe chalate waqt zaroorat nahi
    server = HTTPServer(("0.0.0.0", int(port)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server port %s pe chal raha hai", port)


def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise SystemExit(".env me TELEGRAM_TOKEN aur GEMINI_API_KEY dono set karo.")

    start_health_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot chal raha hai...")
    app.run_polling()


if __name__ == "__main__":
    main()
