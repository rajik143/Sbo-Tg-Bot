import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN")

# Debug: Token load ஆச்சானு check
print(f"Token loaded: {TOKEN is not None}")
print(f"Token length: {len(TOKEN) if TOKEN else 0}")

if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("வணக்கம்! நான் உங்கள் Bot 🤖")

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))

print("Bot is running...")
app.run_polling()
