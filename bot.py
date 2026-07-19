import os
import json
import requests
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========== CONFIG ==========
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_URL = os.environ.get("FIREBASE_URL", "https://sbo-database-default-rtdb.firebaseio.com/")

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError("BOT_TOKEN and GEMINI_API_KEY must be set!")

# Gemini AI Setup
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# ========== FIREBASE FUNCTIONS ==========
def fetch_firebase_data():
    """Firebase RTDB-ல இருக்குற Data-அ Fetch பண்ணும்"""
    try:
        url = f"{FIREBASE_URL}.json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data
    except Exception as e:
        print(f"Firebase Error: {e}")
        return None

def format_data_for_ai(data):
    """AI-க்கு அனுப்ப Data-அ Readable Format-ஆ மாற்றும்"""
    if not data:
        return "No data available in database."
    
    # JSON-அ String-ஆ மாற்றும் (Limit: 10000 chars to avoid token limit)
    data_str = json.dumps(data, indent=2, ensure_ascii=False)
    if len(data_str) > 10000:
        data_str = data_str[:10000] + "\n... (truncated)"
    return data_str

# ========== AI FUNCTION ==========
def ask_ai(question, db_data):
    """Gemini AI-க்கு Question + Database Data கொடுத்து Answer வாங்கும்"""
    context = format_data_for_ai(db_data)
    
    prompt = f"""You are a helpful assistant. Answer the user's question based ONLY on the following database information.

DATABASE INFORMATION:
{context}

USER QUESTION: {question}

Please provide a concise and accurate answer in the same language as the user's question. If the answer is not in the database, say "Sorry, I don't have that information in the database."""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"AI Error: {e}")
        return "Sorry, AI service is temporarily unavailable. Please try again later."

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *SBO AI Bot*\n\n"
        "வணக்கம்! என்னிடம் எந்த கேள்வி கேட்டாலும், "
        "Database-ல இருக்குற Information-அ வச்சு Answer சொல்வேன்!\n\n"
        "கேள்வி கேட்க /ask command-அ Use பண்ணுங்க\n"
        "Example: `/ask என்ன products இருக்கு?`",
        parse_mode='Markdown'
    )

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User /ask command use பண்ணினா இது trigger ஆகும்"""
    # /ask க்கு அப்புறம் என்ன type பண்ணியிருக்காங்களோ அது question
    question = ' '.join(context.args)
    
    if not question:
        await update.message.reply_text(
            "❓ கேள்வி கேட்க /ask command-அ Use பண்ணுங்க\n"
            "Example: `/ask database-ல என்ன details இருக்கு?`"
        )
        return
    
    # Typing indicator காட்டும்
    await update.message.chat.send_action(action="typing")
    
    # Firebase-ல இருந்து Data Fetch
    db_data = fetch_firebase_data()
    
    if db_data is None:
        await update.message.reply_text("⚠️ Database-ல இருந்து Data எடுக்க முடியல. பிறகு முயற்சிக்கவும்.")
        return
    
    # AI-க்கு அனுப்பி Answer வாங்கும்
    answer = ask_ai(question, db_data)
    
    await update.message.reply_text(answer)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct-ஆ message அனுப்பினாலும் AI Answer சொல்லும்"""
    question = update.message.text
    
    await update.message.chat.send_action(action="typing")
    
    db_data = fetch_firebase_data()
    
    if db_data is None:
        await update.message.reply_text("⚠️ Database Error. Please try again later.")
        return
    
    answer = ask_ai(question, db_data)
    await update.message.reply_text(answer)

# ========== MAIN ==========
async def main():
    app = Application.builder().token(TOKEN).build()
    
    # Command Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ask", ask_command))
    
    # Direct Message Handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 SBO AI Bot is running with Firebase + Gemini...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await __import__('asyncio').Event().wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
