import os
import json
import time
import re
import requests
import logging
from datetime import datetime

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

TOKEN = os.environ.get('BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
FIREBASE_URL = os.environ.get('FIREBASE_URL', 'https://sbo-database-default-rtdb.firebaseio.com/')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError('BOT_TOKEN and GEMINI_API_KEY must be set!')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.1-flash-lite')

_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300
user_stats = {}

def fetch_firebase_data(force_refresh=False):
    global _cache
    if not force_refresh and _cache['data'] is not None:
        if time.time() - _cache['timestamp'] < CACHE_TTL:
            return _cache['data']
    try:
        url = f"{FIREBASE_URL}.json"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        _cache = {'data': data, 'timestamp': time.time()}
        logger.info('Firebase data fetched successfully')
        return data
    except Exception as e:
        logger.error(f'Firebase Error: {e}')
        return _cache['data'] if _cache['data'] else None

def search_database(data, query):
    if not data or not isinstance(data, dict):
        return data
    query_lower = query.lower().strip()
    matches = {}
    id_patterns = re.findall(r'SBO[A-Z]{2,4}[0-9]+', query.upper())
    def search_nested(obj, path=''):
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_str = str(key).lower()
                if query_lower in key_str or any(pid in str(key).upper() for pid in id_patterns):
                    matches[key] = value
                    continue
                if isinstance(value, str) and query_lower in value.lower():
                    matches[key] = value
                    continue
                if isinstance(value, (dict, list)):
                    search_nested(value, f'{path}/{key}')
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                search_nested(item, f'{path}[{i}]')
    search_nested(data)
    if not matches:
        return data
    return matches

def format_data_for_ai(data, max_chars=12000):
    if not data:
        return 'No data available in database.'
    data_str = json.dumps(data, indent=2, ensure_ascii=False)
    if len(data_str) > max_chars:
        compact_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if len(compact_str) <= max_chars:
            return compact_str
        return compact_str[:max_chars] + '\n...[truncated - more data available in database]'
    return data_str

def ask_ai(question, db_data, chat_history=None):
    filtered_data = search_database(db_data, question)
    context = format_data_for_ai(filtered_data)
    history_text = ''
    if chat_history:
        history_text = '\n\nCONVERSATION HISTORY:\n' + '\n'.join(chat_history[-5:])
    prompt = f"""You are a helpful assistant for SBO (Small Business Organization).
You have access to a database with user information.

IMPORTANT INSTRUCTIONS:
1. The database contains MULTIPLE users/entries. Search through ALL entries to find the answer.
2. If the user asks about a specific person, ID, or name, find ALL matching entries - not just the first one.
3. Look at ALL wallet balances, commissions, and earnings for the requested user.
4. If multiple users match, mention all of them.
5. Answer in the SAME LANGUAGE as the user's question (Tamil or English).
6. If information is not found, say 'Sorry, I could not find that information in the database.'

DATABASE INFORMATION (filtered based on query):
{context}
{history_text}

USER QUESTION: {question}

Please provide a complete answer. Search through ALL entries in the database, not just the first one."""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f'AI Error: {e}')
        return 'Sorry, AI service is temporarily unavailable. Please try again later.'

def ask_ai_with_image(question, image_data, db_data):
    context = format_data_for_ai(db_data)
    prompt = f"""You are a helpful assistant. Analyze the image and answer based on database info.

DATABASE INFORMATION:
{context}

USER QUESTION ABOUT IMAGE: {question}

Please answer in the same language as the user's question."""
    try:
        image_part = {'mime_type': 'image/jpeg', 'data': image_data}
        response = model.generate_content([prompt, image_part])
        return response.text
    except Exception as e:
        logger.error(f'AI Image Error: {e}')
        return 'Sorry, I could not analyze the image. Please try again.'

def log_user_activity(user_id, username, action):
    if user_id not in user_stats:
        user_stats[user_id] = {
            'username': username,
            'first_seen': datetime.now().isoformat(),
            'message_count': 0,
            'actions': []
        }
    user_stats[user_id]['message_count'] += 1
    user_stats[user_id]['last_seen'] = datetime.now().isoformat()
    user_stats[user_id]['actions'].append({'action': action, 'time': datetime.now().isoformat()})

def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton('Ask a Question', callback_data='menu_ask')],
        [InlineKeyboardButton('View Database', callback_data='menu_db')],
        [InlineKeyboardButton('About Bot', callback_data='menu_about')],
        [InlineKeyboardButton('Help', callback_data='menu_help')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton('Broadcast', callback_data='admin_broadcast')],
        [InlineKeyboardButton('Stats', callback_data='admin_stats')],
        [InlineKeyboardButton('Refresh Cache', callback_data='admin_refresh')],
        [InlineKeyboardButton('Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/start')
    welcome_text = (
        '🤖 *SBO AI Bot*\n\n'
        'வணக்கம் ' + user.first_name + '! 👋\n\n'
        'நான் உங்கள் SBO AI Assistant. Database-ல இருக்குற Information-அ வச்சு '
        'உங்கள் கேள்விகளுக்கு Answer சொல்வேன்!\n\n'
        '✨ *Features:*\n'
        '• Direct message-அ அனுப்பினால் AI Answer சொல்லும்\n'
        '• /ask - கேள்வி கேட்க\n'
        '• /menu - Main Menu காட்டும்\n'
        '• /image - Photo அனுப்பி கேள்வி கேட்க\n'
        '• /help - Help காட்டும்\n\n'
        'கீழே உள்ள Buttons-அ Use பண்ணுங்க 👇'
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode='Markdown',
        reply_markup=get_main_menu_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        '🆘 *SBO AI Bot - Help*\n\n'
        '*Available Commands:*\n'
        '• /start - Bot-அ Start பண்ணும்\n'
        '• /ask <question> - Database-ல இருந்து Answer கேட்க\n'
        '• /menu - Interactive Menu காட்டும்\n'
        '• /image - Photo அனுப்பி கேள்வி கேட்க\n'
        '• /dbinfo - Database-ல இருக்குற Data-அ காட்டும்\n'
        '• /status - Bot Status காட்டும்\n'
        '• /help - இந்த Help Message-அ காட்டும்\n\n'
        '*Admin Commands:*\n'
        '• /admin - Admin Panel\n'
        '• /broadcast <message> - All Users-க்கு Message அனுப்பும்\n'
        '• /stats - User Statistics காட்டும்\n\n'
        '*Tips:*\n'
        '• Direct-ஆ Message அனுப்பினாலும் AI Answer சொல்லும்\n'
        '• கேள்வி Tamil-லயோ English-லயோ கேட்கலாம்'
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '📋 *Main Menu*\n\nChoose an option:',
        parse_mode='Markdown',
        reply_markup=get_main_menu_keyboard()
    )

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = ' '.join(context.args)
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/ask')
    if not question:
        await update.message.reply_text(
            '❓ கேள்வி கேட்க /ask command-அ Use பண்ணுங்க\n'
            'Example: `/ask database-ல என்ன details இருக்கு?`',
            parse_mode='Markdown'
        )
        return
    await update.message.chat.send_action(action='typing')
    db_data = fetch_firebase_data()
    if db_data is None:
        await update.message.reply_text('⚠️ Database-ல இருந்து Data எடுக்க முடியல. பிறகு முயற்சிக்கவும்.')
        return
    answer = ask_ai(question, db_data)
    await update.message.reply_text('💡 *Answer:*\n\n' + answer, parse_mode='Markdown')

async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action='typing')
    db_data = fetch_firebase_data(force_refresh=True)
    if db_data is None:
        await update.message.reply_text('⚠️ Database Error. Please try again later.')
        return
    def summarize_data(obj, depth=0):
        if isinstance(obj, dict):
            keys = list(obj.keys())
            if depth == 0:
                return 'Root keys: ' + ', '.join(keys[:20]) + ('...' if len(keys) > 20 else '') + ' (Total: ' + str(len(keys)) + ' entries)'
            return '{' + ', '.join(keys[:10]) + ('...' if len(keys) > 10 else '') + '}'
        elif isinstance(obj, list):
            return '[' + str(len(obj)) + ' items]'
        return str(obj)[:100]
    summary = summarize_data(db_data)
    data_str = json.dumps(db_data, indent=2, ensure_ascii=False)
    if len(data_str) > 3500:
        data_str = data_str[:3500] + '\n... (truncated)'
    await update.message.reply_text(
        '📊 *Database Summary:*\n' + summary + '\n\n```\n' + data_str + '\n```',
        parse_mode='Markdown'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_data = fetch_firebase_data()
    cache_status = '✅ Fresh' if db_data else '❌ Error'
    user_count = len(user_stats)
    total_entries = 0
    if isinstance(db_data, dict):
        total_entries = len(db_data)
    status_text = (
        '🤖 *Bot Status*\n\n'
        '• Database Connection: ' + cache_status + '\n'
        '• Database Entries: ' + str(total_entries) + '\n'
        '• Active Users (session): ' + str(user_count) + '\n'
        '• AI Model: Gemini 3.1 Flash Lite\n'
        '• Bot Version: 2.1 Enhanced\n'
        '• Time: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '📸 *Image Analysis*\n\n'
        'Please send a photo with caption (your question) or reply to a photo with your question.',
        parse_mode='Markdown'
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, 'photo')
    caption = update.message.caption or 'What is in this image?'
    await update.message.chat.send_action(action='typing')
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    db_data = fetch_firebase_data()
    if db_data is None:
        await update.message.reply_text('⚠️ Database Error. Please try again later.')
        return
    answer = ask_ai_with_image(caption, bytes(photo_bytes), db_data)
    await update.message.reply_text('📸 *Image Analysis:*\n\n' + answer, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    question = update.message.text
    log_user_activity(user.id, user.username or user.first_name, 'direct_message')
    await update.message.chat.send_action(action='typing')
    db_data = fetch_firebase_data()
    if db_data is None:
        await update.message.reply_text('⚠️ Database Error. Please try again later.')
        return
    if 'history' not in context.user_data:
        context.user_data['history'] = []
    answer = ask_ai(question, db_data, context.user_data['history'])
    context.user_data['history'].append('User: ' + question)
    context.user_data['history'].append('AI: ' + answer)
    if len(context.user_data['history']) > 20:
        context.user_data['history'] = context.user_data['history'][-20:]
    await update.message.reply_text(answer)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    if data == 'menu_ask':
        await query.edit_message_text(
            '❓ *Ask a Question*\n\n'
            'Type your question directly or use:\n'
            '`/ask your question here`',
            parse_mode='Markdown'
        )
    elif data == 'menu_db':
        await query.edit_message_text('⏳ Fetching database info...')
        db_data = fetch_firebase_data(force_refresh=True)
        if db_data:
            total = len(db_data) if isinstance(db_data, dict) else 'N/A'
            keys = list(db_data.keys())[:15] if isinstance(db_data, dict) else []
            preview = '\n'.join(['• ' + str(k) for k in keys])
            suffix = '\n...' if len(keys) == 15 else ''
            await query.edit_message_text(
                '📊 *Database Preview*\n'
                'Total entries: ' + str(total) + '\n\n'
                '*Sample IDs:*\n' + preview + suffix,
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text('⚠️ Failed to fetch database info.')
    elif data == 'menu_about':
        about_text = (
            '🤖 *About SBO AI Bot*\n\n'
            'Version: 2.1 Enhanced\n'
            'Powered by: Google Gemini AI\n'
            'Database: Firebase Realtime DB\n'
            'Developer: SBO Team\n\n'
            'This bot helps you query your database using natural language!'
        )
        await query.edit_message_text(about_text, parse_mode='Markdown')
    elif data == 'menu_help':
        await query.edit_message_text(
            '🆘 Use /help command for detailed help!',
            reply_markup=get_main_menu_keyboard()
        )
    elif data == 'menu_back':
        await query.edit_message_text(
            '📋 *Main Menu*',
            parse_mode='Markdown',
            reply_markup=get_main_menu_keyboard()
        )
    elif data.startswith('admin_'):
        if ADMIN_ID and str(user.id) != str(ADMIN_ID):
            await query.edit_message_text('❌ You are not authorized!')
            return
        if data == 'admin_broadcast':
            await query.edit_message_text(
                '📢 Use /broadcast <message> to send message to all users.\n'
                'Example: `/broadcast Hello everyone!`',
                parse_mode='Markdown'
            )
        elif data == 'admin_stats':
            total_users = len(user_stats)
            total_messages = sum(u.get('message_count', 0) for u in user_stats.values())
            stats_text = (
                '📊 *Bot Statistics*\n\n'
                '• Total Users (session): ' + str(total_users) + '\n'
                '• Total Messages: ' + str(total_messages) + '\n'
                '• Active Now: ' + str(total_users)
            )
            await query.edit_message_text(stats_text, parse_mode='Markdown')
        elif data == 'admin_refresh':
            fetch_firebase_data(force_refresh=True)
            await query.edit_message_text('✅ Cache refreshed successfully!')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ You are not authorized to access admin panel!')
        return
    await update.message.reply_text(
        '🔐 *Admin Panel*',
        parse_mode='Markdown',
        reply_markup=get_admin_keyboard()
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text('Usage: /broadcast <message>')
        return
    if not user_stats:
        await update.message.reply_text('No users to broadcast to.')
        return
    sent = 0
    failed = 0
    for user_id in user_stats.keys():
        try:
            await context.bot.send_message(chat_id=user_id, text='📢 *Broadcast:*\n\n' + message, parse_mode='Markdown')
            sent += 1
        except Exception as e:
            logger.error(f'Broadcast failed for {user_id}: {e}')
            failed += 1
    await update.message.reply_text('📢 Broadcast complete!\n✅ Sent: ' + str(sent) + '\n❌ Failed: ' + str(failed))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    total_users = len(user_stats)
    total_messages = sum(u.get('message_count', 0) for u in user_stats.values())
    stats_text = (
        '📊 *Detailed Statistics*\n\n'
        '• Total Users (session): ' + str(total_users) + '\n'
        '• Total Messages: ' + str(total_messages) + '\n\n'
        '*Recent Users:*\n'
    )
    for uid, info in list(user_stats.items())[:10]:
        stats_text += '• ' + info.get('username', 'Unknown') + ' - ' + str(info.get('message_count', 0)) + ' msgs\n'
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update {update} caused error {context.error}')
    if update and update.effective_message:
        await update.effective_message.reply_text('⚠️ An error occurred. Please try again later.')

async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('menu', menu_command))
    app.add_handler(CommandHandler('ask', ask_command))
    app.add_handler(CommandHandler('dbinfo', dbinfo_command))
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('image', image_command))
    app.add_handler(CommandHandler('admin', admin_command))
    app.add_handler(CommandHandler('broadcast', broadcast_command))
    app.add_handler(CommandHandler('stats', stats_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info('🤖 SBO AI Bot Enhanced is starting...')
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info('🔄 Bot running in polling mode...')
    await __import__('asyncio').Event().wait()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
