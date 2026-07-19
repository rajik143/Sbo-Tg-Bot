from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

# Railway-ல Environment Variable-ஆ Token வைக்கணும்
TOKEN = os.environ.get("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("வணக்கம்! நான் உங்கள் Bot 🤖")

async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! எப்படி இருக்கீங்க?")

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("hello", hello))

print("Bot is running...")
app.run_polling()
