import os
import sys
import json
import time
import re
import io
import requests
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

# ========== CONFIGURATION ==========
TOKEN = os.environ.get('BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
FIREBASE_URL = os.environ.get('FIREBASE_URL', 'https://sbo-database-default-rtdb.firebaseio.com/')
ADMIN_ID = os.environ.get('ADMIN_ID')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
PORT = int(os.environ.get('PORT', 8080))

if not TOKEN or not GEMINI_API_KEY:
    print("ERROR: BOT_TOKEN and GEMINI_API_KEY must be set!")
    sys.exit(1)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress noisy logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# Gemini AI Setup
try:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')
    vision_model = genai.GenerativeModel('gemini-2.0-flash')
    logger.info("Gemini AI initialized")
except Exception as e:
    logger.error(f"Failed to initialize Gemini: {e}")
    sys.exit(1)

# Telegram imports
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        filters, ContextTypes
    )
except ImportError as e:
    logger.error(f"Failed to import telegram: {e}")
    sys.exit(1)

# ========== GLOBAL STATE ==========
_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300
user_stats: Dict[int, dict] = {}
conversation_history: Dict[int, List[dict]] = {}
voice_enabled: set = set()

# ========== DATABASE FUNCTIONS ==========

def fetch_firebase_data(force_refresh: bool = False) -> Optional[dict]:
    global _cache
    if not force_refresh and _cache['data'] is not None:
        if time.time() - _cache['timestamp'] < CACHE_TTL:
            return _cache['data']
    try:
        url = f"{FIREBASE_URL}.json"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _cache = {'data': data, 'timestamp': time.time()}
        logger.info(f'Firebase refreshed. Entries: {len(data) if data else 0}')
        return data
    except Exception as e:
        logger.error(f'Firebase fetch error: {e}')
        return _cache['data']


def parse_amount(amt: Any) -> float:
    if amt is None:
        return 0.0
    if isinstance(amt, (int, float)):
        return float(amt)
    clean = re.sub(r'[^\d.]', '', str(amt))
    try:
        return float(clean) if clean else 0.0
    except:
        return 0.0


def get_all_users(db_data: dict) -> List[dict]:
    users = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        wallets = data.get('💰 Wallets', {})
        reviews = data.get('📝 Content Review History', {})
        sharing = data.get('🔗 Content Sharing History', {})
        media = data.get('🚀 Media Booster History', {})
        withdrawals = data.get('📋 Withdrawal History', {})
        total_tasks = len(reviews) + len(sharing) + len(media)
        all_tasks = list(reviews.values()) + list(sharing.values()) + list(media.values())
        pending = sum(1 for t in all_tasks if 'pending' in str(t.get('Status', '')).lower())
        approved = sum(1 for t in all_tasks if any(x in str(t.get('Status', '')).lower() for x in ['approved', 'success']))
        rejected = sum(1 for t in all_tasks if any(x in str(t.get('Status', '')).lower() for x in ['reject', 'fail']))
        users.append({
            'id': uid,
            'name': profile.get('Name', uid),
            'email': profile.get('Email', 'N/A'),
            'phone': profile.get('Phone', 'N/A'),
            'whatsapp': profile.get('WhatsApp', 'N/A'),
            'buy_mote_id': profile.get('BuyMote ID', 'N/A'),
            'affiliate_balance': parse_amount(wallets.get('Affiliate Balance', 0)),
            'task_earned': parse_amount(wallets.get('Task Earned', 0)),
            'total_credited': parse_amount(wallets.get('Total Credited', 0)),
            'referral_earned': parse_amount(wallets.get('Referral Earned', 0)),
            'intro_commission': parse_amount(wallets.get('Intro Commission', 0)),
            'total_tasks': total_tasks,
            'pending': pending,
            'approved': approved,
            'rejected': rejected,
            'withdrawal_count': len(withdrawals),
            'last_login': data.get('🏷️ Metadata', {}).get('Last Login', 'N/A'),
            'device': data.get('🏷️ Metadata', {}).get('Device Model', 'N/A'),
            'raw': data
        })
    return users


def find_user_by_id(db_data: dict, sbo_id: str) -> Optional[dict]:
    if sbo_id in db_data:
        users = get_all_users({sbo_id: db_data[sbo_id]})
        return users[0] if users else None
    return None


def get_top_balances(db_data: dict, limit: int = 5) -> List[dict]:
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['affiliate_balance'], reverse=True)[:limit]


def get_top_task_earners(db_data: dict, limit: int = 5) -> List[dict]:
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['task_earned'], reverse=True)[:limit]


def get_top_credited(db_data: dict, limit: int = 5) -> List[dict]:
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['total_credited'], reverse=True)[:limit]


def get_global_stats(db_data: dict) -> dict:
    users = get_all_users(db_data)
    return {
        'total_users': len(users),
        'total_affiliate': sum(u['affiliate_balance'] for u in users),
        'total_task': sum(u['task_earned'] for u in users),
        'total_credited': sum(u['total_credited'] for u in users),
        'total_pending': sum(u['pending'] for u in users),
        'total_approved': sum(u['approved'] for u in users),
        'total_rejected': sum(u['rejected'] for u in users),
    }


def get_user_tasks(db_data: dict, sbo_id: str) -> List[dict]:
    if sbo_id not in db_data:
        return []
    user = db_data[sbo_id]
    tasks = []
    reviews = user.get('📝 Content Review History', {})
    for k, v in reviews.items():
        tasks.append({
            'key': k, 'type': 'Review', 'category': '📝 Content Review History',
            'date': v.get('Date', 'N/A'), 'description': v.get('Product', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown'),
            'rating': v.get('Rating', '-')
        })
    sharing = user.get('🔗 Content Sharing History', {})
    for k, v in sharing.items():
        tasks.append({
            'key': k, 'type': 'Sharing', 'category': '🔗 Content Sharing History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('Social Media Link', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })
    media = user.get('🚀 Media Booster History', {})
    for k, v in media.items():
        tasks.append({
            'key': k, 'type': 'Media', 'category': '🚀 Media Booster History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('Video URL', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })
    withdrawals = user.get('📋 Withdrawal History', {})
    for k, v in withdrawals.items():
        tasks.append({
            'key': k, 'type': 'Withdrawal', 'category': '📋 Withdrawal History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('ID', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })
    return tasks


def get_nominee_info(db_data: dict, sbo_id: str) -> Optional[dict]:
    if sbo_id not in db_data:
        return None
    nominee = db_data[sbo_id].get('👥 Nominee', {})
    return nominee if nominee else None


def get_bank_info(db_data: dict, sbo_id: str) -> Optional[dict]:
    if sbo_id not in db_data:
        return None
    bank = db_data[sbo_id].get('🏦 Bank & PAN', {})
    return bank if bank else None


def get_pending_tasks_global(db_data: dict) -> List[dict]:
    all_pending = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        name = profile.get('Name', uid)
        tasks = get_user_tasks(db_data, uid)
        for t in tasks:
            if 'pending' in t['status'].lower():
                t['user_id'] = uid
                t['user_name'] = name
                all_pending.append(t)
    return all_pending


def get_tasks_by_status(db_data: dict, status_keyword: str) -> List[dict]:
    result = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        name = profile.get('Name', uid)
        tasks = get_user_tasks(db_data, uid)
        for t in tasks:
            s = t['status'].lower()
            if status_keyword == 'approved' and any(x in s for x in ['approved', 'success']):
                t['user_id'] = uid; t['user_name'] = name; result.append(t)
            elif status_keyword == 'rejected' and any(x in s for x in ['reject', 'fail']):
                t['user_id'] = uid; t['user_name'] = name; result.append(t)
    return result


# ========== AI FUNCTIONS ==========

def build_smart_context(db_data: dict, question: str) -> str:
    q = question.lower()
    context_parts = []

    if any(k in q for k in ['balance', 'evlo', 'athigam', 'wallet', 'amount']):
        top = get_top_balances(db_data, 10)
        context_parts.append("TOP AFFILIATE BALANCES:")
        for i, u in enumerate(top, 1):
            context_parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['affiliate_balance']:,.0f}")
        context_parts.append(f"\nTOTAL AFFILIATE: ₹{sum(u['affiliate_balance'] for u in get_all_users(db_data)):,.0f}")

    if any(k in q for k in ['task', 'earn', 'commission', 'work']):
        top = get_top_task_earners(db_data, 10)
        context_parts.append("TOP TASK EARNERS:")
        for i, u in enumerate(top, 1):
            context_parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['task_earned']:,.0f}")

    if any(k in q for k in ['pending', 'waiting', 'review']):
        pending = get_pending_tasks_global(db_data)
        context_parts.append(f"PENDING TASKS: {len(pending)} total")
        for t in pending[:10]:
            context_parts.append(f"- {t['user_name']} | {t['type']} | {t['description'][:50]}... | {t['amount']} | {t['status']}")

    if any(k in q for k in ['approved', 'success', 'verified']):
        approved = get_tasks_by_status(db_data, 'approved')
        context_parts.append(f"APPROVED TASKS: {len(approved)} total")

    if any(k in q for k in ['rejected', 'fail', 'declined']):
        rejected = get_tasks_by_status(db_data, 'rejected')
        context_parts.append(f"REJECTED TASKS: {len(rejected)} total")

    if any(k in q for k in ['overview', 'summary', 'total', 'all users', 'statistics', 'stats']):
        stats = get_global_stats(db_data)
        context_parts.append("GLOBAL STATISTICS:")
        context_parts.append(f"Total Users: {stats['total_users']}")
        context_parts.append(f"Total Affiliate: ₹{stats['total_affiliate']:,.0f}")
        context_parts.append(f"Total Task: ₹{stats['total_task']:,.0f}")
        context_parts.append(f"Total Credited: ₹{stats['total_credited']:,.0f}")
        context_parts.append(f"Pending: {stats['total_pending']} | Approved: {stats['total_approved']} | Rejected: {stats['total_rejected']}")

    if not context_parts:
        stats = get_global_stats(db_data)
        context_parts.append("DATABASE SUMMARY:")
        context_parts.append(f"Total Users: {stats['total_users']}")
        context_parts.append(f"Total Affiliate: ₹{stats['total_affiliate']:,.0f}")
        context_parts.append(f"Total Task: ₹{stats['total_task']:,.0f}")
        context_parts.append(f"Pending: {stats['total_pending']} | Approved: {stats['total_approved']} | Rejected: {stats['total_rejected']}")
        users = get_all_users(db_data)[:5]
        context_parts.append("\nSAMPLE USERS:")
        for u in users:
            context_parts.append(f"- {u['name']} ({u['id']})")

    return '\n'.join(context_parts)


def ask_ai(question: str, db_data: dict, history: List[dict] = None) -> str:
    context = build_smart_context(db_data, question)

    system_prompt = """You are SBO AI Assistant, a smart database concierge for the SBO platform.
You have access to staff records, wallet balances, task histories, and nominee information.
Answer in the SAME LANGUAGE as the user's question (Tamil, English, or Tanglish).
Be concise but informative. Use bullet points for lists.
If data is not available, say so honestly.

DATABASE CONTEXT:
"""

    messages = []
    if history:
        for h in history[-10:]:
            messages.append({'role': h['role'], 'parts': [h['text']]})

    prompt = system_prompt + context + "\n\nUSER QUESTION: " + question
    messages.append({'role': 'user', 'parts': [prompt]})

    try:
        chat = model.start_chat(history=messages[:-1] if len(messages) > 1 else [])
        response = chat.send_message(prompt)
        return response.text
    except Exception as e:
        logger.error(f'AI Error: {e}')
        return "⚠️ AI service temporarily unavailable. Please try again."


def ask_ai_with_image(question: str, image_bytes: bytes, db_data: dict) -> str:
    context = build_smart_context(db_data, question)
    prompt = f"""You are SBO AI Assistant. Analyze this image and answer based on database context if relevant.

DATABASE CONTEXT:
{context}

USER QUESTION: {question}
"""
    try:
        image_part = {"mime_type": "image/jpeg", "data": image_bytes}
        response = vision_model.generate_content([prompt, image_part])
        return response.text
    except Exception as e:
        logger.error(f'AI Image Error: {e}')
        return "⚠️ Could not analyze the image. Please try again."


# ========== VOICE / TTS ==========

def text_to_speech(text: str) -> Optional[bytes]:
    try:
        from gtts import gTTS
        text = text[:500] + "..." if len(text) > 500 else text
        lang = 'ta' if any(ord(c) > 127 for c in text[:50]) else 'en'
        tts = gTTS(text=text, lang=lang, slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except ImportError:
        logger.warning("gTTS not installed")
        return None
    except Exception as e:
        logger.error(f'TTS Error: {e}')
        return None


# ========== USER ACTIVITY ==========

def log_user_activity(user_id: int, username: str, action: str):
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


def get_or_create_history(user_id: int) -> List[dict]:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    return conversation_history[user_id]


# ========== KEYBOARDS ==========

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('💬 Ask AI', callback_data='menu_ask'),
         InlineKeyboardButton('📊 Overview', callback_data='menu_overview')],
        [InlineKeyboardButton('💰 Balances', callback_data='menu_balances'),
         InlineKeyboardButton('📋 Tasks', callback_data='menu_tasks')],
        [InlineKeyboardButton('🏆 Top Earners', callback_data='menu_top'),
         InlineKeyboardButton('📸 Photo', callback_data='menu_image')],
        [InlineKeyboardButton('🔊 Voice: Off', callback_data='menu_voice'),
         InlineKeyboardButton('❓ Help', callback_data='menu_help')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_quick_query_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('Yaruku balance athigama?', callback_data='qq_top_balance')],
        [InlineKeyboardButton('SBOAFP3350 nominee yaaru?', callback_data='qq_nominee')],
        [InlineKeyboardButton('Review tasks summary', callback_data='qq_review_summary')],
        [InlineKeyboardButton('Brief overview of SBOAFP2209', callback_data='qq_overview')],
        [InlineKeyboardButton('Pending tasks count', callback_data='qq_pending')],
        [InlineKeyboardButton('⬅️ Back to Menu', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_tasks_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('⏳ Pending', callback_data='tasks_pending'),
         InlineKeyboardButton('✅ Approved', callback_data='tasks_approved')],
        [InlineKeyboardButton('❌ Rejected', callback_data='tasks_rejected'),
         InlineKeyboardButton('📊 Stats', callback_data='tasks_stats')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_balances_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('🏆 Top Affiliate Balances', callback_data='bal_top_affiliate')],
        [InlineKeyboardButton('💼 Top Task Earners', callback_data='bal_top_task')],
        [InlineKeyboardButton('💳 Top Total Credited', callback_data='bal_top_credited')],
        [InlineKeyboardButton('📈 Global Stats', callback_data='bal_stats')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('📢 Broadcast', callback_data='admin_broadcast'),
         InlineKeyboardButton('📊 Stats', callback_data='admin_stats')],
        [InlineKeyboardButton('🔄 Refresh Cache', callback_data='admin_refresh'),
         InlineKeyboardButton('👥 Users', callback_data='admin_users')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


# ========== FORMATTERS ==========

def format_user_card(user: dict) -> str:
    lines = [
        f"👤 <b>{user['name']}</b>",
        f"🆔 <code>{user['id']}</code>",
        f"📧 {user['email']}",
        f"📱 {user['phone']}",
        f"🛒 BuyMote ID: {user['buy_mote_id']}",
        "",
        "💰 <b>WALLETS</b>",
        f"  • Affiliate Balance: <code>₹{user['affiliate_balance']:,.0f}</code>",
        f"  • Task Earned: <code>₹{user['task_earned']:,.0f}</code>",
        f"  • Total Credited: <code>₹{user['total_credited']:,.0f}</code>",
        f"  • Referral Earned: <code>₹{user['referral_earned']:,.0f}</code>",
        f"  • Intro Commission: <code>₹{user['intro_commission']:,.0f}</code>",
        "",
        "📊 <b>TASKS</b>",
        f"  • Total: {user['total_tasks']} | ⏳ {user['pending']} | ✅ {user['approved']} | ❌ {user['rejected']}",
        f"  • Withdrawals: {user['withdrawal_count']}",
        "",
        f"📱 Device: {user['device']}",
        f"🕐 Last Login: {user['last_login']}",
    ]
    return '\n'.join(lines)


def format_task_list(tasks: List[dict], title: str, limit: int = 15) -> str:
    if not tasks:
        return f"📭 <b>{title}</b>\n\nNo tasks found."
    lines = [f"📋 <b>{title} ({len(tasks)} total)</b>\n"]
    for i, t in enumerate(tasks[:limit], 1):
        status_emoji = '⏳' if 'pending' in t['status'].lower() else '✅' if any(x in t['status'].lower() for x in ['approved', 'success']) else '❌'
        desc = str(t['description'])[:60]
        if len(str(t['description'])) > 60:
            desc += '...'
        lines.append(
            f"{i}. {status_emoji} <b>{t['type']}</b> | {t['amount']}\n"
            f"   📝 {desc}\n"
            f"   👤 {t.get('user_name', 'N/A')} | 📅 {t['date']}\n"
        )
    if len(tasks) > limit:
        lines.append(f"\n... and {len(tasks) - limit} more")
    return '\n'.join(lines)


def format_leaderboard(users: List[dict], metric: str, title: str) -> str:
    if not users:
        return f"🏆 <b>{title}</b>\n\nNo data available."
    lines = [f"🏆 <b>{title}</b>\n"]
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
    for i, u in enumerate(users, 1):
        medal = medals[i-1] if i <= 10 else f"{i}."
        val = u.get(metric, 0)
        if isinstance(val, (int, float)):
            val_str = f"₹{val:,.0f}" if metric != 'withdrawal_count' else f"{int(val)}"
        else:
            val_str = str(val)
        lines.append(f"{medal} <b>{u['name']}</b> ({u['id']})\n   └ {val_str}\n")
    return '\n'.join(lines)


# ========== HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/start')
    welcome = (
        f"🤖 <b>SBO AI Assistant</b>\n\n"
        f"Vanakkam <b>{user.first_name}</b>! 👋\n\n"
        f"Naan unga SBO smart AI assistant. Direct database records vachu "
        f"unga kitta bathil sollen. Ask me anything in <b>English, Tamil, or Tanglish</b>!\n\n"
        f"<b>Quick Actions:</b>"
    )
    await update.message.reply_text(welcome, parse_mode='HTML', reply_markup=get_main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🆘 <b>SBO AI Bot - Help</b>\n\n"
        "<b>Commands:</b>\n"
        "• /start - Bot start pannum\n"
        "• /ask &lt;question&gt; - Database-la irundhu answer kekka\n"
        "• /menu - Interactive menu kaatum\n"
        "• /voice - Voice reply on/off\n"
        "• /dbinfo - Database info kaatum\n"
        "• /status - Bot status\n"
        "• /help - Help message\n\n"
        "<b>Admin:</b>\n"
        "• /admin - Admin panel\n"
        "• /broadcast &lt;msg&gt; - All users-kku anuppum\n"
        "• /stats - Bot statistics\n\n"
        "<i>Direct message anuppinallum AI answer solllum!</i>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 <b>Main Menu</b>\n\nChoose an option:",
        parse_mode='HTML',
        reply_markup=get_main_menu_keyboard()
    )


async def voice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in voice_enabled:
        voice_enabled.discard(user_id)
        await update.message.reply_text("🔇 <b>Voice replies OFF</b>", parse_mode='HTML')
    else:
        voice_enabled.add(user_id)
        await update.message.reply_text(
            "🔊 <b>Voice replies ON</b>\n\nAI answers will now be sent as voice messages too!",
            parse_mode='HTML'
        )


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = ' '.join(context.args)
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/ask')
    if not question:
        await update.message.reply_text(
            "❓ <b>Ask a Question</b>\n\n"
            "Type your question directly or choose a quick query:",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )
        return
    await _process_ai_query(update, question)


async def _process_ai_query(update: Update, question: str, image_bytes: bytes = None):
    user = update.effective_user
    user_id = user.id
    await update.message.chat.send_action(action='typing')

    db_data = fetch_firebase_data()
    if db_data is None:
        await update.message.reply_text("⚠️ Database connection failed. Please try again.")
        return

    history = get_or_create_history(user_id)
    response_text = await _try_direct_query(db_data, question, update)

    if response_text is None:
        if image_bytes:
            response_text = ask_ai_with_image(question, image_bytes, db_data)
        else:
            response_text = ask_ai(question, db_data, history)

    history.append({'role': 'user', 'text': question})
    history.append({'role': 'model', 'text': response_text})
    if len(history) > 20:
        history[:] = history[-20:]

    # Send text (split if too long)
    if len(response_text) > 4000:
        parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
        for part in parts:
            await update.message.reply_text(part, parse_mode='HTML')
    else:
        await update.message.reply_text(response_text, parse_mode='HTML')

    # Send voice if enabled
    if user_id in voice_enabled:
        voice_bytes = text_to_speech(response_text)
        if voice_bytes:
            await update.message.reply_voice(voice=InputFile(io.BytesIO(voice_bytes), filename='reply.ogg'))


async def _try_direct_query(db_data: dict, question: str, update: Update) -> Optional[str]:
    q = question.lower().strip()

    # User overview
    overview_match = re.search(r'(?:overview|details|info|about)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not overview_match:
        overview_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:overview|details|info)', q, re.I)
    if overview_match:
        sbo_id = overview_match.group(1).upper()
        user = find_user_by_id(db_data, sbo_id)
        if user:
            return format_user_card(user)
        return f"❌ User <code>{sbo_id}</code> not found."

    # Nominee
    nominee_match = re.search(r'(?:nominee\s+(?:of\s+)?)(SBO[A-Z0-9]+)', q, re.I)
    if not nominee_match:
        nominee_match = re.search(r'(SBO[A-Z0-9]+)\s+nominee', q, re.I)
    if nominee_match:
        sbo_id = nominee_match.group(1).upper()
        nominee = get_nominee_info(db_data, sbo_id)
        if nominee:
            return (
                f"👥 <b>Nominee for {sbo_id}</b>\n"
                f"Name: <b>{nominee.get('Nominee Name', 'N/A')}</b>\n"
                f"Email: {nominee.get('Nominee Email', 'N/A')}\n"
                f"Phone: {nominee.get('Nominee Phone', 'N/A')}"
            )
        return f"❌ No nominee info for <code>{sbo_id}</code>."

    # Bank
    bank_match = re.search(r'(?:bank|pan|kyc)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not bank_match:
        bank_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:bank|pan|kyc)', q, re.I)
    if bank_match:
        sbo_id = bank_match.group(1).upper()
        bank = get_bank_info(db_data, sbo_id)
        if bank:
            return (
                f"🏦 <b>Bank & PAN for {sbo_id}</b>\n"
                f"Bank: <b>{bank.get('Bank Name', 'N/A')}</b>\n"
                f"Holder: {bank.get('Account Holder', 'N/A')}\n"
                f"Account: <code>{bank.get('Account Number', 'N/A')}</code>\n"
                f"IFSC: <code>{bank.get('IFSC Code', 'N/A')}</code>\n"
                f"Branch: {bank.get('Branch', 'N/A')}\n"
                f"PAN: <code>{bank.get('PAN Number', 'N/A')}</code>"
            )
        return f"❌ No bank info for <code>{sbo_id}</code>."

    # Tasks
    tasks_match = re.search(r'(?:tasks|works|history)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not tasks_match:
        tasks_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:tasks|works|history)', q, re.I)
    if tasks_match:
        sbo_id = tasks_match.group(1).upper()
        tasks = get_user_tasks(db_data, sbo_id)
        if tasks:
            user = find_user_by_id(db_data, sbo_id)
            name = user['name'] if user else sbo_id
            return format_task_list(tasks, f"Tasks for {name}")
        return f"❌ No tasks for <code>{sbo_id}</code>."

    # Top balance
    if any(k in q for k in ['top balance', 'athigama', 'highest balance', 'most balance']):
        top = get_top_balances(db_data, 5)
        return format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')

    # Top task earners
    if any(k in q for k in ['top task', 'task earner', 'most task']):
        top = get_top_task_earners(db_data, 5)
        return format_leaderboard(top, 'task_earned', 'Top Task Earners')

    # Pending
    if any(k in q for k in ['pending task', 'waiting task']):
        pending = get_pending_tasks_global(db_data)
        return format_task_list(pending, 'Pending Tasks')

    # Approved
    if any(k in q for k in ['approved task', 'success task']):
        approved = get_tasks_by_status(db_data, 'approved')
        return format_task_list(approved, 'Approved Tasks')

    # Rejected
    if any(k in q for k in ['rejected task', 'fail task']):
        rejected = get_tasks_by_status(db_data, 'rejected')
        return format_task_list(rejected, 'Rejected Tasks')

    # Global stats
    if any(k in q for k in ['global stat', 'total user', 'overview', 'summary', 'all stat']):
        stats = get_global_stats(db_data)
        return (
            f"📊 <b>SBO Global Statistics</b>\n\n"
            f"👥 Total Users: <b>{stats['total_users']}</b>\n"
            f"💰 Total Affiliate Balance: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
            f"💼 Total Task Earned: <code>₹{stats['total_task']:,.0f}</code>\n"
            f"💳 Total Credited: <code>₹{stats['total_credited']:,.0f}</code>\n"
            f"⏳ Pending Tasks: <b>{stats['total_pending']}</b>\n"
            f"✅ Approved Tasks: <b>{stats['total_approved']}</b>\n"
            f"❌ Rejected Tasks: <b>{stats['total_rejected']}</b>"
        )

    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    question = update.message.text
    log_user_activity(user.id, user.username or user.first_name, 'direct_message')
    await _process_ai_query(update, question)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    caption = update.message.caption or 'What is in this image?'
    log_user_activity(user.id, user.username or user.first_name, 'photo')
    await update.message.chat.send_action(action='typing')
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    await _process_ai_query(update, caption, bytes(photo_bytes))


async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action='typing')
    db_data = fetch_firebase_data(force_refresh=True)
    if db_data is None:
        await update.message.reply_text("⚠️ Database Error. Please try again later.")
        return
    stats = get_global_stats(db_data)
    users = get_all_users(db_data)
    sample = users[:5]
    text = (
        f"📊 <b>Database Info</b>\n\n"
        f"Total Entries: <b>{stats['total_users']}</b>\n"
        f"Total Affiliate: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
        f"Total Task: <code>₹{stats['total_task']:,.0f}</code>\n"
        f"Total Credited: <code>₹{stats['total_credited']:,.0f}</code>\n\n"
        f"<b>Sample IDs:</b>\n"
    )
    for u in sample:
        text += f"• <code>{u['id']}</code> - {u['name']}\n"
    await update.message.reply_text(text, parse_mode='HTML')


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_data = fetch_firebase_data()
    stats = get_global_stats(db_data) if db_data else {}
    text = (
        f"🤖 <b>Bot Status</b>\n\n"
        f"• Database: {'✅ Connected' if db_data else '❌ Error'}\n"
        f"• Entries: {stats.get('total_users', 'N/A')}\n"
        f"• Active Users: {len(user_stats)}\n"
        f"• AI Model: Gemini 2.0 Flash\n"
        f"• Version: 3.1 Railway-Fix\n"
        f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(text, parse_mode='HTML')


# ========== CALLBACK HANDLER ==========

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    if data == 'menu_voice':
        if user.id in voice_enabled:
            voice_enabled.discard(user.id)
            await query.edit_message_text("🔇 <b>Voice replies OFF</b>\n\nMain Menu:", parse_mode='HTML', reply_markup=get_main_menu_keyboard())
        else:
            voice_enabled.add(user.id)
            await query.edit_message_text("🔊 <b>Voice replies ON</b>\n\nMain Menu:", parse_mode='HTML', reply_markup=get_main_menu_keyboard())
        return

    if data == 'menu_ask':
        await query.edit_message_text(
            "💬 <b>Ask AI</b>\n\nType your question or choose quick query:\n\n"
            "<i>Examples:</i>\n"
            "• <code>SBOAFP3350 overview</code>\n"
            "• <code>Who has highest balance?</code>\n"
            "• <code>Pending tasks</code>\n"
            "• <code>Global stats</code>",
            parse_mode='HTML', reply_markup=get_quick_query_keyboard()
        )
    elif data == 'menu_overview':
        await query.edit_message_text(
            "👤 <b>My Overview</b>\n\nType your SBO ID:\n"
            "<i>Example:</i> <code>SBOAFP3350 overview</code>",
            parse_mode='HTML', reply_markup=get_quick_query_keyboard()
        )
    elif data == 'menu_balances':
        await query.edit_message_text("💰 <b>Balances</b>\n\nChoose:", parse_mode='HTML', reply_markup=get_balances_keyboard())
    elif data == 'menu_tasks':
        await query.edit_message_text("📋 <b>Tasks</b>\n\nChoose:", parse_mode='HTML', reply_markup=get_tasks_keyboard())
    elif data == 'menu_image':
        await query.edit_message_text(
            "📸 <b>Image Analysis</b>\n\nSend a photo with caption.",
            parse_mode='HTML', reply_markup=get_main_menu_keyboard()
        )
    elif data == 'menu_help':
        await query.edit_message_text(
            "🆘 <b>Help</b>\n\n"
            "• Direct message → AI answers\n"
            "• /ask &lt;question&gt; → Query\n"
            "• /voice → Toggle voice\n"
            "• /menu → Show menu\n\n"
            "<i>Supports Tamil, English, Tanglish!</i>",
            parse_mode='HTML', reply_markup=get_main_menu_keyboard()
        )
    elif data == 'menu_back':
        await query.edit_message_text("📋 <b>Main Menu</b>\n\nChoose:", parse_mode='HTML', reply_markup=get_main_menu_keyboard())

    # Quick queries
    elif data == 'qq_top_balance':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_balances(db_data, 5)
            text = format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())
    elif data == 'qq_nominee':
        await query.edit_message_text(
            "👥 <b>Nominee Lookup</b>\n\nType: <code>SBOAFP3350 nominee</code>",
            parse_mode='HTML', reply_markup=get_quick_query_keyboard()
        )
    elif data == 'qq_review_summary':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            approved = get_tasks_by_status(db_data, 'approved')
            rejected = get_tasks_by_status(db_data, 'rejected')
            text = (
                f"📋 <b>Task Summary</b>\n\n"
                f"⏳ Pending: <b>{len(pending)}</b>\n"
                f"✅ Approved: <b>{len(approved)}</b>\n"
                f"❌ Rejected: <b>{len(rejected)}</b>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())
    elif data == 'qq_overview':
        await query.edit_message_text(
            "👤 <b>User Overview</b>\n\nType: <code>SBOAFP2209 overview</code>",
            parse_mode='HTML', reply_markup=get_quick_query_keyboard()
        )
    elif data == 'qq_pending':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            text = format_task_list(pending, 'Pending Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())

    # Balances
    elif data == 'bal_top_affiliate':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_balances(db_data, 10)
            text = format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())
    elif data == 'bal_top_task':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_task_earners(db_data, 10)
            text = format_leaderboard(top, 'task_earned', 'Top Task Earners')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())
    elif data == 'bal_top_credited':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_credited(db_data, 10)
            text = format_leaderboard(top, 'total_credited', 'Top Total Credited')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())
    elif data == 'bal_stats':
        db_data = fetch_firebase_data()
        if db_data:
            stats = get_global_stats(db_data)
            text = (
                f"📈 <b>Global Stats</b>\n\n"
                f"👥 Users: <b>{stats['total_users']}</b>\n"
                f"💰 Affiliate: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
                f"💼 Task: <code>₹{stats['total_task']:,.0f}</code>\n"
                f"💳 Credited: <code>₹{stats['total_credited']:,.0f}</code>\n"
                f"⏳ Pending: <b>{stats['total_pending']}</b>\n"
                f"✅ Approved: <b>{stats['total_approved']}</b>\n"
                f"❌ Rejected: <b>{stats['total_rejected']}</b>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())

    # Tasks
    elif data == 'tasks_pending':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            text = format_task_list(pending, 'Pending Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())
    elif data == 'tasks_approved':
        db_data = fetch_firebase_data()
        if db_data:
            approved = get_tasks_by_status(db_data, 'approved')
            text = format_task_list(approved, 'Approved Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())
    elif data == 'tasks_rejected':
        db_data = fetch_firebase_data()
        if db_data:
            rejected = get_tasks_by_status(db_data, 'rejected')
            text = format_task_list(rejected, 'Rejected Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())
    elif data == 'tasks_stats':
        db_data = fetch_firebase_data()
        if db_data:
            stats = get_global_stats(db_data)
            total = stats['total_approved'] + stats['total_rejected']
            rate = (stats['total_approved'] / max(total, 1) * 100)
            text = (
                f"📊 <b>Task Statistics</b>\n\n"
                f"⏳ Pending: <b>{stats['total_pending']}</b>\n"
                f"✅ Approved: <b>{stats['total_approved']}</b>\n"
                f"❌ Rejected: <b>{stats['total_rejected']}</b>\n"
                f"📈 Approval Rate: <b>{rate:.1f}%</b>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())

    # Admin
    elif data.startswith('admin_'):
        if ADMIN_ID and str(user.id) != str(ADMIN_ID):
            await query.edit_message_text('❌ Unauthorized!')
            return
        if data == 'admin_broadcast':
            await query.edit_message_text('📢 Use /broadcast &lt;message&gt;', parse_mode='HTML', reply_markup=get_admin_keyboard())
        elif data == 'admin_stats':
            total_users = len(user_stats)
            total_msgs = sum(u.get('message_count', 0) for u in user_stats.values())
            text = f"📊 <b>Stats</b>\n\n• Users: {total_users}\n• Messages: {total_msgs}\n• Voice: {len(voice_enabled)}"
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_admin_keyboard())
        elif data == 'admin_refresh':
            fetch_firebase_data(force_refresh=True)
            await query.edit_message_text('✅ Cache refreshed!', reply_markup=get_admin_keyboard())
        elif data == 'admin_users':
            db_data = fetch_firebase_data()
            if db_data:
                users = get_all_users(db_data)
                text = f"👥 <b>Users ({len(users)})</b>\n\n"
                for u in users[:20]:
                    text += f"• <code>{u['id']}</code> - {u['name']}\n"
                if len(users) > 20:
                    text += f"\n... and {len(users) - 20} more"
                await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_admin_keyboard())


# ========== ADMIN COMMANDS ==========

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    await update.message.reply_text('🔐 <b>Admin Panel</b>', parse_mode='HTML', reply_markup=get_admin_keyboard())


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text('Usage: /broadcast &lt;message&gt;')
        return
    if not user_stats:
        await update.message.reply_text('No users to broadcast to.')
        return
    sent = 0
    failed = 0
    for user_id in list(user_stats.keys()):
        try:
            await context.bot.send_message(chat_id=user_id, text=f'📢 <b>Broadcast:</b>\n\n{message}', parse_mode='HTML')
            sent += 1
        except Exception as e:
            logger.error(f'Broadcast failed for {user_id}: {e}')
            failed += 1
    await update.message.reply_text(f'📢 Done!\n✅ Sent: {sent}\n❌ Failed: {failed}')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    total_users = len(user_stats)
    total_msgs = sum(u.get('message_count', 0) for u in user_stats.values())
    text = f"📊 <b>Stats</b>\n\n• Users: {total_users}\n• Messages: {total_msgs}\n\n<b>Recent:</b>\n"
    for uid, info in list(user_stats.items())[:10]:
        text += f"• {info.get('username', 'Unknown')} - {info.get('message_count', 0)} msgs\n"
    await update.message.reply_text(text, parse_mode='HTML')


# ========== ERROR HANDLER ==========

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update {update} caused error {context.error}')
    if update and update.effective_message:
        await update.effective_message.reply_text('⚠️ An error occurred. Please try again.')


# ========== MAIN ==========

def main():
    logger.info("🤖 SBO AI Bot v3.1 starting...")

    # Pre-fetch database to verify connection
    db_data = fetch_firebase_data(force_refresh=True)
    if db_data:
        logger.info(f"✅ Database connected. {len(db_data)} entries found.")
    else:
        logger.warning("⚠️ Could not connect to Firebase on startup. Will retry on first request.")

    # Build application
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('menu', menu_command))
    app.add_handler(CommandHandler('ask', ask_command))
    app.add_handler(CommandHandler('voice', voice_toggle))
    app.add_handler(CommandHandler('dbinfo', dbinfo_command))
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('admin', admin_command))
    app.add_handler(CommandHandler('broadcast', broadcast_command))
    app.add_handler(CommandHandler('stats', stats_command))

    # Callbacks & messages
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Errors
    app.add_error_handler(error_handler)

    logger.info(f"WEBHOOK_URL={WEBHOOK_URL}, PORT={PORT}")

    if WEBHOOK_URL:
        # WEBHOOK MODE (Railway production)
        webhook_path = f"/webhook"
        webhook_full = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"
        logger.info(f"🌐 Starting in WEBHOOK mode on port {PORT}")
        logger.info(f"🔗 Webhook URL: {webhook_full}")

        app.run_webhook(
            listen='0.0.0.0',
            port=PORT,
            webhook_url=webhook_full,
            drop_pending_updates=True
        )
    else:
        # POLLING MODE (local development)
        logger.info("🔄 Starting in POLLING mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
logger = logging.getLogger(__name__)

# Gemini AI Setup
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')
vision_model = genai.GenerativeModel('gemini-2.0-flash')

# ========== GLOBAL STATE ==========
_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300
user_stats: Dict[int, dict] = {}
conversation_history: Dict[int, List[dict]] = {}  # user_id -> list of messages
voice_enabled: set = set()  # user_ids who enabled voice replies

# ========== DATABASE FUNCTIONS ==========

def fetch_firebase_data(force_refresh: bool = False) -> Optional[dict]:
    """Fetch SBO data from Firebase with smart caching."""
    global _cache
    if not force_refresh and _cache['data'] is not None:
        if time.time() - _cache['timestamp'] < CACHE_TTL:
            return _cache['data']
    try:
        url = f"{FIREBASE_URL}.json"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _cache = {'data': data, 'timestamp': time.time()}
        logger.info('Firebase data refreshed. Entries: %d', len(data) if data else 0)
        return data
    except Exception as e:
        logger.error(f'Firebase fetch error: {e}')
        return _cache['data']  # Return stale cache if available


def parse_amount(amt: Any) -> float:
    """Parse amount strings like '₹1,500' to float."""
    if amt is None:
        return 0.0
    if isinstance(amt, (int, float)):
        return float(amt)
    clean = re.sub(r'[^\d.]', '', str(amt))
    try:
        return float(clean) if clean else 0.0
    except:
        return 0.0


def get_all_users(db_data: dict) -> List[dict]:
    """Return list of all users with computed fields."""
    users = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        wallets = data.get('💰 Wallets', {})

        # Count tasks
        reviews = data.get('📝 Content Review History', {})
        sharing = data.get('🔗 Content Sharing History', {})
        media = data.get('🚀 Media Booster History', {})
        withdrawals = data.get('📋 Withdrawal History', {})

        total_tasks = len(reviews) + len(sharing) + len(media)

        # Count statuses
        all_tasks = []
        for t in list(reviews.values()) + list(sharing.values()) + list(media.values()):
            all_tasks.append(t)

        pending = sum(1 for t in all_tasks if 'pending' in str(t.get('Status', '')).lower())
        approved = sum(1 for t in all_tasks if any(x in str(t.get('Status', '')).lower() for x in ['approved', 'success']))
        rejected = sum(1 for t in all_tasks if any(x in str(t.get('Status', '')).lower() for x in ['reject', 'fail']))

        users.append({
            'id': uid,
            'name': profile.get('Name', uid),
            'email': profile.get('Email', 'N/A'),
            'phone': profile.get('Phone', 'N/A'),
            'whatsapp': profile.get('WhatsApp', 'N/A'),
            'buy_mote_id': profile.get('BuyMote ID', 'N/A'),
            'affiliate_balance': parse_amount(wallets.get('Affiliate Balance', 0)),
            'task_earned': parse_amount(wallets.get('Task Earned', 0)),
            'total_credited': parse_amount(wallets.get('Total Credited', 0)),
            'referral_earned': parse_amount(wallets.get('Referral Earned', 0)),
            'intro_commission': parse_amount(wallets.get('Intro Commission', 0)),
            'total_tasks': total_tasks,
            'pending': pending,
            'approved': approved,
            'rejected': rejected,
            'withdrawal_count': len(withdrawals),
            'last_login': data.get('🏷️ Metadata', {}).get('Last Login', 'N/A'),
            'device': data.get('🏷️ Metadata', {}).get('Device Model', 'N/A'),
            'raw': data
        })
    return users


def find_user_by_id(db_data: dict, sbo_id: str) -> Optional[dict]:
    """Find user by exact SBO ID."""
    if sbo_id in db_data:
        users = get_all_users({sbo_id: db_data[sbo_id]})
        return users[0] if users else None
    return None


def find_user_by_name(db_data: dict, name_query: str) -> Optional[dict]:
    """Fuzzy search user by name (case-insensitive partial match)."""
    name_lower = name_query.lower()
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        name = profile.get('Name', '')
        if name_lower in name.lower():
            users = get_all_users({uid: data})
            return users[0] if users else None
    return None


def get_top_balances(db_data: dict, limit: int = 5) -> List[dict]:
    """Get top users by affiliate balance."""
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['affiliate_balance'], reverse=True)[:limit]


def get_top_task_earners(db_data: dict, limit: int = 5) -> List[dict]:
    """Get top users by task earnings."""
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['task_earned'], reverse=True)[:limit]


def get_top_credited(db_data: dict, limit: int = 5) -> List[dict]:
    """Get top users by total credited."""
    users = get_all_users(db_data)
    return sorted(users, key=lambda x: x['total_credited'], reverse=True)[:limit]


def get_global_stats(db_data: dict) -> dict:
    """Compute global statistics across all users."""
    users = get_all_users(db_data)
    total_affiliate = sum(u['affiliate_balance'] for u in users)
    total_task = sum(u['task_earned'] for u in users)
    total_credited = sum(u['total_credited'] for u in users)
    total_pending = sum(u['pending'] for u in users)
    total_approved = sum(u['approved'] for u in users)
    total_rejected = sum(u['rejected'] for u in users)
    return {
        'total_users': len(users),
        'total_affiliate': total_affiliate,
        'total_task': total_task,
        'total_credited': total_credited,
        'total_pending': total_pending,
        'total_approved': total_approved,
        'total_rejected': total_rejected,
    }


def get_user_tasks(db_data: dict, sbo_id: str) -> List[dict]:
    """Get all tasks for a specific user."""
    if sbo_id not in db_data:
        return []
    user = db_data[sbo_id]
    tasks = []

    reviews = user.get('📝 Content Review History', {})
    for k, v in reviews.items():
        tasks.append({
            'key': k, 'type': 'Review', 'category': '📝 Content Review History',
            'date': v.get('Date', 'N/A'), 'description': v.get('Product', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown'),
            'rating': v.get('Rating', '-')
        })

    sharing = user.get('🔗 Content Sharing History', {})
    for k, v in sharing.items():
        tasks.append({
            'key': k, 'type': 'Sharing', 'category': '🔗 Content Sharing History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('Social Media Link', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })

    media = user.get('🚀 Media Booster History', {})
    for k, v in media.items():
        tasks.append({
            'key': k, 'type': 'Media', 'category': '🚀 Media Booster History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('Video URL', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })

    withdrawals = user.get('📋 Withdrawal History', {})
    for k, v in withdrawals.items():
        tasks.append({
            'key': k, 'type': 'Withdrawal', 'category': '📋 Withdrawal History',
            'date': v.get('Request Date', 'N/A'), 'description': v.get('ID', 'N/A'),
            'amount': v.get('Amount', '₹0'), 'status': v.get('Status', 'Unknown')
        })

    return tasks


def get_nominee_info(db_data: dict, sbo_id: str) -> Optional[dict]:
    """Get nominee information for a user."""
    if sbo_id not in db_data:
        return None
    nominee = db_data[sbo_id].get('👥 Nominee', {})
    return nominee if nominee else None


def get_bank_info(db_data: dict, sbo_id: str) -> Optional[dict]:
    """Get bank/PAN info for a user."""
    if sbo_id not in db_data:
        return None
    bank = db_data[sbo_id].get('🏦 Bank & PAN', {})
    return bank if bank else None


def get_pending_tasks_global(db_data: dict) -> List[dict]:
    """Get all pending tasks across all users."""
    all_pending = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        name = profile.get('Name', uid)
        tasks = get_user_tasks(db_data, uid)
        for t in tasks:
            if 'pending' in t['status'].lower():
                t['user_id'] = uid
                t['user_name'] = name
                all_pending.append(t)
    return all_pending


def get_tasks_by_status(db_data: dict, status_keyword: str) -> List[dict]:
    """Get tasks filtered by status keyword."""
    result = []
    for uid, data in db_data.items():
        profile = data.get('👤 Profile', {})
        name = profile.get('Name', uid)
        tasks = get_user_tasks(db_data, uid)
        for t in tasks:
            s = t['status'].lower()
            if status_keyword == 'approved' and any(x in s for x in ['approved', 'success']):
                t['user_id'] = uid; t['user_name'] = name; result.append(t)
            elif status_keyword == 'rejected' and any(x in s for x in ['reject', 'fail']):
                t['user_id'] = uid; t['user_name'] = name; result.append(t)
    return result


# ========== AI FUNCTIONS ==========

def build_smart_context(db_data: dict, question: str) -> str:
    """Build context based on question intent instead of dumping all data."""
    q = question.lower()
    context_parts = []

    # Balance related queries
    if any(k in q for k in ['balance', 'evlo', 'athigam', 'wallet', 'amount', '₹']):
        top = get_top_balances(db_data, 10)
        context_parts.append("TOP AFFILIATE BALANCES:")
        for i, u in enumerate(top, 1):
            context_parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['affiliate_balance']:,.0f}")
        context_parts.append(f"\nTOTAL AFFILIATE BALANCE ACROSS ALL: ₹{sum(u['affiliate_balance'] for u in get_all_users(db_data)):,.0f}")

    # Task earnings
    if any(k in q for k in ['task', 'earn', 'commission', 'work']):
        top = get_top_task_earners(db_data, 10)
        context_parts.append("TOP TASK EARNERS:")
        for i, u in enumerate(top, 1):
            context_parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['task_earned']:,.0f}")

    # Pending tasks
    if any(k in q for k in ['pending', 'waiting', 'review']):
        pending = get_pending_tasks_global(db_data)
        context_parts.append(f"PENDING TASKS: {len(pending)} total")
        for t in pending[:10]:
            context_parts.append(f"- {t['user_name']} | {t['type']} | {t['description'][:50]}... | {t['amount']} | {t['status']}")

    # Approved tasks
    if any(k in q for k in ['approved', 'success', 'verified']):
        approved = get_tasks_by_status(db_data, 'approved')
        context_parts.append(f"APPROVED TASKS: {len(approved)} total")

    # Rejected tasks
    if any(k in q for k in ['rejected', 'fail', 'declined']):
        rejected = get_tasks_by_status(db_data, 'rejected')
        context_parts.append(f"REJECTED TASKS: {len(rejected)} total")

    # Stats overview
    if any(k in q for k in ['overview', 'summary', 'total', 'all users', 'statistics', 'stats']):
        stats = get_global_stats(db_data)
        context_parts.append("GLOBAL STATISTICS:")
        context_parts.append(f"Total Users: {stats['total_users']}")
        context_parts.append(f"Total Affiliate Balance: ₹{stats['total_affiliate']:,.0f}")
        context_parts.append(f"Total Task Earned: ₹{stats['total_task']:,.0f}")
        context_parts.append(f"Total Credited: ₹{stats['total_credited']:,.0f}")
        context_parts.append(f"Pending Tasks: {stats['total_pending']}")
        context_parts.append(f"Approved Tasks: {stats['total_approved']}")
        context_parts.append(f"Rejected Tasks: {stats['total_rejected']}")

    # If no specific intent detected, give a summary + sample
    if not context_parts:
        stats = get_global_stats(db_data)
        context_parts.append("DATABASE SUMMARY:")
        context_parts.append(f"Total Users: {stats['total_users']}")
        context_parts.append(f"Total Affiliate: ₹{stats['total_affiliate']:,.0f}")
        context_parts.append(f"Total Task Earned: ₹{stats['total_task']:,.0f}")
        context_parts.append(f"Pending: {stats['total_pending']} | Approved: {stats['total_approved']} | Rejected: {stats['total_rejected']}")
        # Add some user names for reference
        users = get_all_users(db_data)[:5]
        context_parts.append("\nSAMPLE USERS:")
        for u in users:
            context_parts.append(f"- {u['name']} ({u['id']})")

    return '\n'.join(context_parts)


def ask_ai(question: str, db_data: dict, history: List[dict] = None) -> str:
    """Ask Gemini AI with smart context and conversation history."""
    context = build_smart_context(db_data, question)

    system_prompt = """You are SBO AI Assistant, a smart database concierge for the SBO (Smart Business Opportunity) platform.
You have access to staff records, wallet balances, task histories, and nominee information.
Answer in the SAME LANGUAGE as the user's question (Tamil, English, or Tanglish).
Be concise but informative. Use bullet points for lists.
If data is not available, say so honestly.

DATABASE CONTEXT:
"""

    messages = []
    if history:
        for h in history[-10:]:  # Keep last 10 messages
            messages.append({'role': h['role'], 'parts': [h['text']]})

    prompt = system_prompt + context + "\n\nUSER QUESTION: " + question
    messages.append({'role': 'user', 'parts': [prompt]})

    try:
        chat = model.start_chat(history=messages[:-1] if len(messages) > 1 else [])
        response = chat.send_message(prompt)
        return response.text
    except Exception as e:
        logger.error(f'AI Error: {e}')
        return "⚠️ AI service temporarily unavailable. Please try again."


def ask_ai_with_image(question: str, image_bytes: bytes, db_data: dict) -> str:
    """Ask Gemini AI with image + database context."""
    context = build_smart_context(db_data, question)
    prompt = f"""You are SBO AI Assistant. Analyze this image and answer based on database context if relevant.

DATABASE CONTEXT:
{context}

USER QUESTION: {question}
"""
    try:
        image_part = {"mime_type": "image/jpeg", "data": image_bytes}
        response = vision_model.generate_content([prompt, image_part])
        return response.text
    except Exception as e:
        logger.error(f'AI Image Error: {e}')
        return "⚠️ Could not analyze the image. Please try again."


# ========== VOICE / TTS ==========

def text_to_speech(text: str) -> Optional[bytes]:
    """Convert text to speech using Google TTS (gTTS). Falls back gracefully."""
    try:
        from gtts import gTTS
        # Limit text length
        text = text[:500] + "..." if len(text) > 500 else text
        tts = gTTS(text=text, lang='ta' if any(ord(c) > 127 for c in text[:50]) else 'en', slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except ImportError:
        logger.warning("gTTS not installed. Voice replies disabled.")
        return None
    except Exception as e:
        logger.error(f'TTS Error: {e}')
        return None


# ========== USER ACTIVITY TRACKING ==========

def log_user_activity(user_id: int, username: str, action: str):
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


def get_or_create_history(user_id: int) -> List[dict]:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    return conversation_history[user_id]


# ========== KEYBOARD BUILDERS ==========

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('💬 Ask AI', callback_data='menu_ask'),
         InlineKeyboardButton('📊 My Overview', callback_data='menu_overview')],
        [InlineKeyboardButton('💰 Balances', callback_data='menu_balances'),
         InlineKeyboardButton('📋 Tasks', callback_data='menu_tasks')],
        [InlineKeyboardButton('🏆 Top Earners', callback_data='menu_top'),
         InlineKeyboardButton('📸 Send Photo', callback_data='menu_image')],
        [InlineKeyboardButton('🔊 Voice: Off', callback_data='menu_voice'),
         InlineKeyboardButton('❓ Help', callback_data='menu_help')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_quick_query_keyboard() -> InlineKeyboardMarkup:
    """Quick query buttons like the website's suggested queries."""
    keyboard = [
        [InlineKeyboardButton('Yaruku balance athigama?', callback_data='qq_top_balance')],
        [InlineKeyboardButton('SBOAFP3350 nominee yaaru?', callback_data='qq_nominee')],
        [InlineKeyboardButton('Review tasks summary', callback_data='qq_review_summary')],
        [InlineKeyboardButton('Brief overview of SBOAFP2209', callback_data='qq_overview')],
        [InlineKeyboardButton('Pending tasks count', callback_data='qq_pending')],
        [InlineKeyboardButton('⬅️ Back to Menu', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_tasks_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('⏳ Pending Tasks', callback_data='tasks_pending'),
         InlineKeyboardButton('✅ Approved Tasks', callback_data='tasks_approved')],
        [InlineKeyboardButton('❌ Rejected Tasks', callback_data='tasks_rejected'),
         InlineKeyboardButton('📊 Task Stats', callback_data='tasks_stats')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_balances_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('🏆 Top Affiliate Balances', callback_data='bal_top_affiliate')],
        [InlineKeyboardButton('💼 Top Task Earners', callback_data='bal_top_task')],
        [InlineKeyboardButton('💳 Top Total Credited', callback_data='bal_top_credited')],
        [InlineKeyboardButton('📈 Global Stats', callback_data='bal_stats')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('📢 Broadcast', callback_data='admin_broadcast'),
         InlineKeyboardButton('📊 Stats', callback_data='admin_stats')],
        [InlineKeyboardButton('🔄 Refresh Cache', callback_data='admin_refresh'),
         InlineKeyboardButton('👥 User List', callback_data='admin_users')],
        [InlineKeyboardButton('⬅️ Back', callback_data='menu_back')],
    ]
    return InlineKeyboardMarkup(keyboard)


# ========== FORMATTERS ==========

def format_user_card(user: dict) -> str:
    """Format user data as a rich card."""
    lines = [
        f"👤 <b>{user['name']}</b>",
        f"🆔 <code>{user['id']}</code>",
        f"📧 {user['email']}",
        f"📱 {user['phone']}",
        f"🛒 BuyMote ID: {user['buy_mote_id']}",
        "",
        "💰 <b>WALLETS</b>",
        f"  • Affiliate Balance: <code>₹{user['affiliate_balance']:,.0f}</code>",
        f"  • Task Earned: <code>₹{user['task_earned']:,.0f}</code>",
        f"  • Total Credited: <code>₹{user['total_credited']:,.0f}</code>",
        f"  • Referral Earned: <code>₹{user['referral_earned']:,.0f}</code>",
        f"  • Intro Commission: <code>₹{user['intro_commission']:,.0f}</code>",
        "",
        "📊 <b>TASKS</b>",
        f"  • Total: {user['total_tasks']} | ⏳ {user['pending']} | ✅ {user['approved']} | ❌ {user['rejected']}",
        f"  • Withdrawals: {user['withdrawal_count']}",
        "",
        f"📱 Device: {user['device']}",
        f"🕐 Last Login: {user['last_login']}",
    ]
    return '\n'.join(lines)


def format_task_list(tasks: List[dict], title: str, limit: int = 15) -> str:
    """Format a list of tasks."""
    if not tasks:
        return f"📭 <b>{title}</b>\n\nNo tasks found."

    lines = [f"📋 <b>{title} ({len(tasks)} total)</b>\n"]
    for i, t in enumerate(tasks[:limit], 1):
        status_emoji = '⏳' if 'pending' in t['status'].lower() else '✅' if any(x in t['status'].lower() for x in ['approved', 'success']) else '❌'
        lines.append(
            f"{i}. {status_emoji} <b>{t['type']}</b> | {t['amount']}\n"
            f"   📝 {t['description'][:60]}{'...' if len(str(t['description'])) > 60 else ''}\n"
            f"   👤 {t.get('user_name', 'N/A')} | 📅 {t['date']}\n"
        )
    if len(tasks) > limit:
        lines.append(f"\n... and {len(tasks) - limit} more")
    return '\n'.join(lines)


def format_leaderboard(users: List[dict], metric: str, title: str) -> str:
    """Format a leaderboard."""
    if not users:
        return f"🏆 <b>{title}</b>\n\nNo data available."

    lines = [f"🏆 <b>{title}</b>\n"]
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
    for i, u in enumerate(users, 1):
        medal = medals[i-1] if i <= 10 else f"{i}."
        val = u.get(metric, 0)
        if isinstance(val, (int, float)):
            val_str = f"₹{val:,.0f}" if metric != 'withdrawal_count' else f"{int(val)}"
        else:
            val_str = str(val)
        lines.append(f"{medal} <b>{u['name']}</b> ({u['id']})\n   └ {val_str}\n")
    return '\n'.join(lines)


# ========== HANDLERS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/start')

    welcome = (
        f"🤖 <b>SBO AI Assistant</b>\n\n"
        f"Vanakkam <b>{user.first_name}</b>! 👋\n\n"
        f"Naan unga SBO smart AI assistant. Direct database records vachu "
        f"unga kitta bathil sollen. Ask me anything in <b>English, Tamil, or Tanglish</b>!\n\n"
        f"<b>Quick Actions:</b>"
    )
    await update.message.reply_text(welcome, parse_mode='HTML', reply_markup=get_main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🆘 <b>SBO AI Bot - Help</b>\n\n"
        "<b>Commands:</b>\n"
        "• /start - Bot start pannum\n"
        "• /ask &lt;question&gt; - Database-la irundhu answer kekka\n"
        "• /menu - Interactive menu kaatum\n"
        "• /image - Photo anuppi kelvi kekka\n"
        "• /voice - Voice reply on/off\n"
        "• /dbinfo - Database info kaatum\n"
        "• /status - Bot status\n"
        "• /help - Help message\n\n"
        "<b>Admin:</b>\n"
        "• /admin - Admin panel\n"
        "• /broadcast &lt;msg&gt; - All users-kku anuppum\n"
        "• /stats - User statistics\n\n"
        "<i>Direct message anuppinallum AI answer solllum!</i>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 <b>Main Menu</b>\n\nChoose an option:",
        parse_mode='HTML',
        reply_markup=get_main_menu_keyboard()
    )


async def voice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in voice_enabled:
        voice_enabled.discard(user_id)
        await update.message.reply_text("🔇 <b>Voice replies OFF</b>", parse_mode='HTML')
    else:
        voice_enabled.add(user_id)
        await update.message.reply_text(
            "🔊 <b>Voice replies ON</b>\n\nAI answers will now be sent as voice messages too!",
            parse_mode='HTML'
        )


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = ' '.join(context.args)
    user = update.effective_user
    log_user_activity(user.id, user.username or user.first_name, '/ask')

    if not question:
        await update.message.reply_text(
            "❓ <b>Ask a Question</b>\n\n"
            "Type your question directly or use:\n"
            "<code>/ask your question here</code>\n\n"
            "Or try these quick queries:",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )
        return

    await _process_ai_query(update, question)


async def _process_ai_query(update: Update, question: str, image_bytes: bytes = None):
    """Core AI query processor."""
    user = update.effective_user
    user_id = user.id

    await update.message.chat.send_action(action='typing')

    db_data = fetch_firebase_data()
    if db_data is None:
        await update.message.reply_text("⚠️ Database connection failed. Please try again.")
        return

    # Manage conversation history
    history = get_or_create_history(user_id)

    # Check for direct database queries first (fast path)
    response_text = await _try_direct_query(db_data, question, update)

    if response_text is None:
        # Fall back to AI
        if image_bytes:
            response_text = ask_ai_with_image(question, image_bytes, db_data)
        else:
            response_text = ask_ai(question, db_data, history)

    # Save to history
    history.append({'role': 'user', 'text': question})
    history.append({'role': 'model', 'text': response_text})
    if len(history) > 20:
        history[:] = history[-20:]

    # Send text response
    # Split long messages
    if len(response_text) > 4000:
        parts = [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]
        for part in parts:
            await update.message.reply_text(part, parse_mode='HTML')
    else:
        await update.message.reply_text(response_text, parse_mode='HTML')

    # Send voice if enabled
    if user_id in voice_enabled:
        voice_bytes = text_to_speech(response_text)
        if voice_bytes:
            await update.message.reply_voice(voice=InputFile(io.BytesIO(voice_bytes), filename='reply.ogg'))


async def _try_direct_query(db_data: dict, question: str, update: Update) -> Optional[str]:
    """Try to answer directly from database without AI for common queries."""
    q = question.lower().strip()

    # Pattern: "SBOXXXX overview" or "brief overview of SBOXXXX"
    overview_match = re.search(r'(?:overview|details|info|about)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not overview_match:
        overview_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:overview|details|info)', q, re.I)
    if overview_match:
        sbo_id = overview_match.group(1).upper()
        user = find_user_by_id(db_data, sbo_id)
        if user:
            return format_user_card(user)
        return f"❌ User <code>{sbo_id}</code> not found in database."

    # Pattern: "SBOXXXX nominee" or "nominee of SBOXXXX"
    nominee_match = re.search(r'(?:nominee\s+(?:of\s+)?)(SBO[A-Z0-9]+)', q, re.I)
    if not nominee_match:
        nominee_match = re.search(r'(SBO[A-Z0-9]+)\s+nominee', q, re.I)
    if nominee_match:
        sbo_id = nominee_match.group(1).upper()
        nominee = get_nominee_info(db_data, sbo_id)
        if nominee:
            lines = [
                f"👥 <b>Nominee for {sbo_id}</b>",
                f"Name: <b>{nominee.get('Nominee Name', 'N/A')}</b>",
                f"Email: {nominee.get('Nominee Email', 'N/A')}",
                f"Phone: {nominee.get('Nominee Phone', 'N/A')}",
            ]
            return '\n'.join(lines)
        return f"❌ No nominee info found for <code>{sbo_id}</code>."

    # Pattern: "SBOXXXX bank" or "bank details of SBOXXXX"
    bank_match = re.search(r'(?:bank|pan|kyc)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not bank_match:
        bank_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:bank|pan|kyc)', q, re.I)
    if bank_match:
        sbo_id = bank_match.group(1).upper()
        bank = get_bank_info(db_data, sbo_id)
        if bank:
            lines = [
                f"🏦 <b>Bank & PAN for {sbo_id}</b>",
                f"Bank: <b>{bank.get('Bank Name', 'N/A')}</b>",
                f"Holder: {bank.get('Account Holder', 'N/A')}",
                f"Account: <code>{bank.get('Account Number', 'N/A')}</code>",
                f"IFSC: <code>{bank.get('IFSC Code', 'N/A')}</code>",
                f"Branch: {bank.get('Branch', 'N/A')}",
                f"PAN: <code>{bank.get('PAN Number', 'N/A')}</code>",
            ]
            return '\n'.join(lines)
        return f"❌ No bank info found for <code>{sbo_id}</code>."

    # Pattern: "SBOXXXX tasks" or "tasks of SBOXXXX"
    tasks_match = re.search(r'(?:tasks|works|history)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not tasks_match:
        tasks_match = re.search(r'(SBO[A-Z0-9]+)\s+(?:tasks|works|history)', q, re.I)
    if tasks_match:
        sbo_id = tasks_match.group(1).upper()
        tasks = get_user_tasks(db_data, sbo_id)
        if tasks:
            user = find_user_by_id(db_data, sbo_id)
            name = user['name'] if user else sbo_id
            return format_task_list(tasks, f"Tasks for {name}")
        return f"❌ No tasks found for <code>{sbo_id}</code>."

    # Pattern: "top balance" / "yaruku balance athigama"
    if any(k in q for k in ['top balance', 'athigama', 'highest balance', 'most balance']):
        top = get_top_balances(db_data, 5)
        return format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')

    # Pattern: "top task earners"
    if any(k in q for k in ['top task', 'task earner', 'most task']):
        top = get_top_task_earners(db_data, 5)
        return format_leaderboard(top, 'task_earned', 'Top Task Earners')

    # Pattern: "pending tasks"
    if any(k in q for k in ['pending task', 'waiting task']):
        pending = get_pending_tasks_global(db_data)
        return format_task_list(pending, 'Pending Tasks')

    # Pattern: "approved tasks"
    if any(k in q for k in ['approved task', 'success task']):
        approved = get_tasks_by_status(db_data, 'approved')
        return format_task_list(approved, 'Approved Tasks')

    # Pattern: "rejected tasks"
    if any(k in q for k in ['rejected task', 'fail task']):
        rejected = get_tasks_by_status(db_data, 'rejected')
        return format_task_list(rejected, 'Rejected Tasks')

    # Pattern: "global stats" / "overview" / "summary"
    if any(k in q for k in ['global stat', 'total user', 'overview', 'summary', 'all stat']):
        stats = get_global_stats(db_data)
        return (
            f"📊 <b>SBO Global Statistics</b>\n\n"
            f"👥 Total Users: <b>{stats['total_users']}</b>\n"
            f"💰 Total Affiliate Balance: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
            f"💼 Total Task Earned: <code>₹{stats['total_task']:,.0f}</code>\n"
            f"💳 Total Credited: <code>₹{stats['total_credited']:,.0f}</code>\n"
            f"⏳ Pending Tasks: <b>{stats['total_pending']}</b>\n"
            f"✅ Approved Tasks: <b>{stats['total_approved']}</b>\n"
            f"❌ Rejected Tasks: <b>{stats['total_rejected']}</b>"
        )

    return None  # Fall back to AI


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct text messages."""
    user = update.effective_user
    question = update.message.text
    log_user_activity(user.id, user.username or user.first_name, 'direct_message')
    await _process_ai_query(update, question)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages with caption."""
    user = update.effective_user
    caption = update.message.caption or 'What is in this image?'
    log_user_activity(user.id, user.username or user.first_name, 'photo')

    await update.message.chat.send_action(action='typing')

    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    await _process_ai_query(update, caption, bytes(photo_bytes))


async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action='typing')
    db_data = fetch_firebase_data(force_refresh=True)
    if db_data is None:
        await update.message.reply_text("⚠️ Database Error. Please try again later.")
        return

    stats = get_global_stats(db_data)
    users = get_all_users(db_data)
    sample = users[:5]

    text = (
        f"📊 <b>Database Info</b>\n\n"
        f"Total Entries: <b>{stats['total_users']}</b>\n"
        f"Total Affiliate: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
        f"Total Task: <code>₹{stats['total_task']:,.0f}</code>\n"
        f"Total Credited: <code>₹{stats['total_credited']:,.0f}</code>\n\n"
        f"<b>Sample IDs:</b>\n"
    )
    for u in sample:
        text += f"• <code>{u['id']}</code> - {u['name']}\n"

    await update.message.reply_text(text, parse_mode='HTML')


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_data = fetch_firebase_data()
    stats = get_global_stats(db_data) if db_data else {}

    text = (
        f"🤖 <b>Bot Status</b>\n\n"
        f"• Database: {'✅ Connected' if db_data else '❌ Error'}\n"
        f"• Entries: {stats.get('total_users', 'N/A')}\n"
        f"• Active Users: {len(user_stats)}\n"
        f"• AI Model: Gemini 2.0 Flash\n"
        f"• Version: 3.0 Enhanced\n"
        f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(text, parse_mode='HTML')


# ========== CALLBACK QUERY HANDLER ==========

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    # Voice toggle
    if data == 'menu_voice':
        if user.id in voice_enabled:
            voice_enabled.discard(user.id)
            await query.edit_message_text(
                "🔇 <b>Voice replies OFF</b>\n\nMain Menu:",
                parse_mode='HTML',
                reply_markup=get_main_menu_keyboard()
            )
        else:
            voice_enabled.add(user.id)
            await query.edit_message_text(
                "🔊 <b>Voice replies ON</b>\n\nAI answers will now be sent as voice messages too!\n\nMain Menu:",
                parse_mode='HTML',
                reply_markup=get_main_menu_keyboard()
            )
        return

    if data == 'menu_ask':
        await query.edit_message_text(
            "💬 <b>Ask AI</b>\n\n"
            "Type your question directly or choose a quick query:\n\n"
            "<i>Examples:</i>\n"
            "• <code>SBOAFP3350 overview</code>\n"
            "• <code>Who has the highest balance?</code>\n"
            "• <code>Pending tasks</code>\n"
            "• <code>Global stats</code>",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )

    elif data == 'menu_overview':
        await query.edit_message_text(
            "👤 <b>My Overview</b>\n\n"
            "Type your SBO ID to get your full profile overview:\n"
            "<i>Example:</i> <code>SBOAFP3350 overview</code>",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )

    elif data == 'menu_balances':
        await query.edit_message_text(
            "💰 <b>Balances</b>\n\nChoose a view:",
            parse_mode='HTML',
            reply_markup=get_balances_keyboard()
        )

    elif data == 'menu_tasks':
        await query.edit_message_text(
            "📋 <b>Tasks</b>\n\nChoose a view:",
            parse_mode='HTML',
            reply_markup=get_tasks_keyboard()
        )

    elif data == 'menu_image':
        await query.edit_message_text(
            "📸 <b>Image Analysis</b>\n\n"
            "Send a photo with a caption (your question) or reply to a photo.",
            parse_mode='HTML',
            reply_markup=get_main_menu_keyboard()
        )

    elif data == 'menu_help':
        await query.edit_message_text(
            "🆘 <b>Help</b>\n\n"
            "• Direct message → AI answers\n"
            "• /ask &lt;question&gt; → Specific query\n"
            "• /voice → Toggle voice replies\n"
            "• /menu → Show this menu\n\n"
            "<i>Supports Tamil, English, and Tanglish!</i>",
            parse_mode='HTML',
            reply_markup=get_main_menu_keyboard()
        )

    elif data == 'menu_back':
        await query.edit_message_text(
            "📋 <b>Main Menu</b>\n\nChoose an option:",
            parse_mode='HTML',
            reply_markup=get_main_menu_keyboard()
        )

    # Quick Queries
    elif data == 'qq_top_balance':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_balances(db_data, 5)
            text = format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())

    elif data == 'qq_nominee':
        await query.edit_message_text(
            "👥 <b>Nominee Lookup</b>\n\n"
            "Type: <code>SBOAFP3350 nominee</code>\n"
            "Or: <code>nominee of SBOAFP3350</code>",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )

    elif data == 'qq_review_summary':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            approved = get_tasks_by_status(db_data, 'approved')
            rejected = get_tasks_by_status(db_data, 'rejected')
            text = (
                f"📋 <b>Task Summary</b>\n\n"
                f"⏳ Pending: <b>{len(pending)}</b>\n"
                f"✅ Approved: <b>{len(approved)}</b>\n"
                f"❌ Rejected: <b>{len(rejected)}</b>\n\n"
                f"<i>Use /ask for detailed breakdowns</i>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())

    elif data == 'qq_overview':
        await query.edit_message_text(
            "👤 <b>User Overview</b>\n\n"
            "Type: <code>SBOAFP2209 overview</code>\n"
            "Or: <code>brief overview of SBOAFP2209</code>",
            parse_mode='HTML',
            reply_markup=get_quick_query_keyboard()
        )

    elif data == 'qq_pending':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            text = format_task_list(pending, 'Pending Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_quick_query_keyboard())

    # Balance sub-menu
    elif data == 'bal_top_affiliate':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_balances(db_data, 10)
            text = format_leaderboard(top, 'affiliate_balance', 'Top Affiliate Balances')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())

    elif data == 'bal_top_task':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_task_earners(db_data, 10)
            text = format_leaderboard(top, 'task_earned', 'Top Task Earners')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())

    elif data == 'bal_top_credited':
        db_data = fetch_firebase_data()
        if db_data:
            top = get_top_credited(db_data, 10)
            text = format_leaderboard(top, 'total_credited', 'Top Total Credited')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())

    elif data == 'bal_stats':
        db_data = fetch_firebase_data()
        if db_data:
            stats = get_global_stats(db_data)
            text = (
                f"📈 <b>Global Financial Stats</b>\n\n"
                f"👥 Users: <b>{stats['total_users']}</b>\n"
                f"💰 Total Affiliate: <code>₹{stats['total_affiliate']:,.0f}</code>\n"
                f"💼 Total Task: <code>₹{stats['total_task']:,.0f}</code>\n"
                f"💳 Total Credited: <code>₹{stats['total_credited']:,.0f}</code>\n"
                f"⏳ Pending: <b>{stats['total_pending']}</b>\n"
                f"✅ Approved: <b>{stats['total_approved']}</b>\n"
                f"❌ Rejected: <b>{stats['total_rejected']}</b>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_balances_keyboard())

    # Tasks sub-menu
    elif data == 'tasks_pending':
        db_data = fetch_firebase_data()
        if db_data:
            pending = get_pending_tasks_global(db_data)
            text = format_task_list(pending, 'Pending Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())

    elif data == 'tasks_approved':
        db_data = fetch_firebase_data()
        if db_data:
            approved = get_tasks_by_status(db_data, 'approved')
            text = format_task_list(approved, 'Approved Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())

    elif data == 'tasks_rejected':
        db_data = fetch_firebase_data()
        if db_data:
            rejected = get_tasks_by_status(db_data, 'rejected')
            text = format_task_list(rejected, 'Rejected Tasks')
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())

    elif data == 'tasks_stats':
        db_data = fetch_firebase_data()
        if db_data:
            stats = get_global_stats(db_data)
            text = (
                f"📊 <b>Task Statistics</b>\n\n"
                f"⏳ Pending: <b>{stats['total_pending']}</b>\n"
                f"✅ Approved: <b>{stats['total_approved']}</b>\n"
                f"❌ Rejected: <b>{stats['total_rejected']}</b>\n"
                f"📈 Approval Rate: <b>{(stats['total_approved'] / max(stats['total_approved'] + stats['total_rejected'], 1) * 100):.1f}%</b>"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_tasks_keyboard())

    # Admin
    elif data.startswith('admin_'):
        if ADMIN_ID and str(user.id) != str(ADMIN_ID):
            await query.edit_message_text('❌ You are not authorized!')
            return
        if data == 'admin_broadcast':
            await query.edit_message_text(
                '📢 Use /broadcast &lt;message&gt; to send to all users.',
                parse_mode='HTML',
                reply_markup=get_admin_keyboard()
            )
        elif data == 'admin_stats':
            total_users = len(user_stats)
            total_msgs = sum(u.get('message_count', 0) for u in user_stats.values())
            text = (
                f"📊 <b>Bot Statistics</b>\n\n"
                f"• Users: {total_users}\n"
                f"• Messages: {total_msgs}\n"
                f"• Voice Enabled: {len(voice_enabled)}"
            )
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_admin_keyboard())
        elif data == 'admin_refresh':
            fetch_firebase_data(force_refresh=True)
            await query.edit_message_text('✅ Cache refreshed!', reply_markup=get_admin_keyboard())
        elif data == 'admin_users':
            db_data = fetch_firebase_data()
            if db_data:
                users = get_all_users(db_data)
                text = f"👥 <b>All Users ({len(users)})</b>\n\n"
                for u in users[:20]:
                    text += f"• <code>{u['id']}</code> - {u['name']}\n"
                if len(users) > 20:
                    text += f"\n... and {len(users) - 20} more"
                await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_admin_keyboard())


# ========== ADMIN COMMANDS ==========

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    await update.message.reply_text(
        '🔐 <b>Admin Panel</b>',
        parse_mode='HTML',
        reply_markup=get_admin_keyboard()
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text('Usage: /broadcast &lt;message&gt;')
        return
    if not user_stats:
        await update.message.reply_text('No users to broadcast to.')
        return
    sent = 0
    failed = 0
    for user_id in list(user_stats.keys()):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f'📢 <b>Broadcast:</b>\n\n{message}',
                parse_mode='HTML'
            )
            sent += 1
        except Exception as e:
            logger.error(f'Broadcast failed for {user_id}: {e}')
            failed += 1
    await update.message.reply_text(f'📢 Done!\n✅ Sent: {sent}\n❌ Failed: {failed}')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_ID and str(user.id) != str(ADMIN_ID):
        await update.message.reply_text('❌ Unauthorized!')
        return
    total_users = len(user_stats)
    total_msgs = sum(u.get('message_count', 0) for u in user_stats.values())
    text = (
        f"📊 <b>Detailed Statistics</b>\n\n"
        f"• Users: {total_users}\n"
        f"• Messages: {total_msgs}\n\n"
        f"<b>Recent Users:</b>\n"
    )
    for uid, info in list(user_stats.items())[:10]:
        text += f"• {info.get('username', 'Unknown')} - {info.get('message_count', 0)} msgs\n"
    await update.message.reply_text(text, parse_mode='HTML')


# ========== ERROR HANDLER ==========

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update {update} caused error {context.error}')
    if update and update.effective_message:
        await update.effective_message.reply_text('⚠️ An error occurred. Please try again later.')


# ========== MAIN ==========

def create_application() -> Application:
    """Create and configure the bot application."""
    app = Application.builder().token(TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('menu', menu_command))
    app.add_handler(CommandHandler('ask', ask_command))
    app.add_handler(CommandHandler('voice', voice_toggle))
    app.add_handler(CommandHandler('dbinfo', dbinfo_command))
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('admin', admin_command))
    app.add_handler(CommandHandler('broadcast', broadcast_command))
    app.add_handler(CommandHandler('stats', stats_command))

    # Callback queries
    app.add_handler(CallbackQueryHandler(button_callback))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    app.add_error_handler(error_handler)

    return app


async def run_polling():
    """Run bot in polling mode (local development)."""
    app = create_application()
    logger.info('🤖 SBO AI Bot v3.0 starting in POLLING mode...')
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info('✅ Bot is running! Press Ctrl+C to stop.')
    await __import__('asyncio').Event().wait()


async def run_webhook():
    """Run bot in webhook mode (production/Railway)."""
    from telegram.ext import Application

    app = create_application()

    # Set webhook
    webhook_path = f"/webhook/{TOKEN}"
    webhook_full_url = f"{WEBHOOK_URL}{webhook_path}"

    logger.info(f'🤖 SBO AI Bot v3.0 starting in WEBHOOK mode...')
    logger.info(f'🔗 Webhook URL: {webhook_full_url}')

    await app.initialize()
    await app.start()

    # Use built-in webhook server
    await app.updater.start_webhook(
        listen='0.0.0.0',
        port=PORT,
        webhook_url=webhook_full_url,
        drop_pending_updates=True
    )

    logger.info(f'✅ Webhook server running on port {PORT}')
    await __import__('asyncio').Event().wait()


if __name__ == '__main__':
    import asyncio

    # Choose mode based on WEBHOOK_URL env var
    if WEBHOOK_URL:
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())
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
