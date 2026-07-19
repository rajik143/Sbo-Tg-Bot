"""SBO AI Bot v3.2 - Clean Railway Deploy"""
import os, sys, json, time, re, io, requests, logging
from datetime import datetime
from typing import Optional, Dict, Any, List

TOKEN = os.environ.get('BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
FIREBASE_URL = os.environ.get('FIREBASE_URL', 'https://sbo-database-default-rtdb.firebaseio.com/')
ADMIN_ID = os.environ.get('ADMIN_ID')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
PORT = int(os.environ.get('PORT', 8080))

if not TOKEN or not GEMINI_API_KEY:
    print("ERROR: BOT_TOKEN and GEMINI_API_KEY must be set!"); sys.exit(1)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

try:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')
    vision_model = genai.GenerativeModel('gemini-2.0-flash')
    logger.info("Gemini ready")
except Exception as e:
    logger.error(f"Gemini failed: {e}"); sys.exit(1)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

_cache = {'data': None, 'timestamp': 0}
CACHE_TTL = 300
user_stats: Dict[int, dict] = {}
conversation_history: Dict[int, List[dict]] = {}
voice_enabled: set = set()

def fetch_firebase(force=False):
    global _cache
    if not force and _cache['data'] and time.time() - _cache['timestamp'] < CACHE_TTL:
        return _cache['data']
    try:
        r = requests.get(f"{FIREBASE_URL}.json", timeout=15)
        r.raise_for_status()
        _cache = {'data': r.json(), 'timestamp': time.time()}
        logger.info(f"Firebase: {len(_cache['data']) if _cache['data'] else 0} entries")
        return _cache['data']
    except Exception as e:
        logger.error(f"Firebase error: {e}")
        return _cache['data']

def parse_amount(amt):
    if amt is None: return 0.0
    if isinstance(amt, (int, float)): return float(amt)
    c = re.sub(r'[^\d.]', '', str(amt))
    try: return float(c) if c else 0.0
    except: return 0.0

def get_users(db):
    users = []
    for uid, d in db.items():
        p = d.get('👤 Profile', {})
        w = d.get('💰 Wallets', {})
        rev = d.get('📝 Content Review History', {})
        shr = d.get('🔗 Content Sharing History', {})
        med = d.get('🚀 Media Booster History', {})
        wd = d.get('📋 Withdrawal History', {})
        all_t = list(rev.values()) + list(shr.values()) + list(med.values())
        users.append({
            'id': uid, 'name': p.get('Name', uid),
            'email': p.get('Email', 'N/A'), 'phone': p.get('Phone', 'N/A'),
            'aff': parse_amount(w.get('Affiliate Balance', 0)),
            'task': parse_amount(w.get('Task Earned', 0)),
            'cred': parse_amount(w.get('Total Credited', 0)),
            'ref': parse_amount(w.get('Referral Earned', 0)),
            'intro': parse_amount(w.get('Intro Commission', 0)),
            'total_tasks': len(all_t),
            'pending': sum(1 for t in all_t if 'pending' in str(t.get('Status','')).lower()),
            'approved': sum(1 for t in all_t if any(x in str(t.get('Status','')).lower() for x in ['approved','success'])),
            'rejected': sum(1 for t in all_t if any(x in str(t.get('Status','')).lower() for x in ['reject','fail'])),
            'wd_count': len(wd), 'device': d.get('🏷️ Metadata',{}).get('Device Model','N/A'),
            'last_login': d.get('🏷️ Metadata',{}).get('Last Login','N/A'), 'raw': d
        })
    return users

def find_user(db, sid):
    if sid in db:
        u = get_users({sid: db[sid]})
        return u[0] if u else None
    return None

def get_tasks(db, sid):
    if sid not in db: return []
    d = db[sid]; tasks = []
    for k,v in d.get('📝 Content Review History',{}).items():
        tasks.append({'type':'Review','date':v.get('Date','N/A'),'desc':v.get('Product','N/A'),'amt':v.get('Amount','₹0'),'status':v.get('Status','Unknown')})
    for k,v in d.get('🔗 Content Sharing History',{}).items():
        tasks.append({'type':'Sharing','date':v.get('Request Date','N/A'),'desc':v.get('Social Media Link','N/A'),'amt':v.get('Amount','₹0'),'status':v.get('Status','Unknown')})
    for k,v in d.get('🚀 Media Booster History',{}).items():
        tasks.append({'type':'Media','date':v.get('Request Date','N/A'),'desc':v.get('Video URL','N/A'),'amt':v.get('Amount','₹0'),'status':v.get('Status','Unknown')})
    for k,v in d.get('📋 Withdrawal History',{}).items():
        tasks.append({'type':'Withdrawal','date':v.get('Request Date','N/A'),'desc':v.get('ID','N/A'),'amt':v.get('Amount','₹0'),'status':v.get('Status','Unknown')})
    return tasks

def get_stats(db):
    u = get_users(db)
    return {'users': len(u), 'aff': sum(x['aff'] for x in u), 'task': sum(x['task'] for x in u),
            'cred': sum(x['cred'] for x in u), 'pend': sum(x['pending'] for x in u),
            'appr': sum(x['approved'] for x in u), 'rej': sum(x['rejected'] for x in u)}

def get_pending_all(db):
    out = []
    for uid, d in db.items():
        name = d.get('👤 Profile',{}).get('Name', uid)
        for t in get_tasks(db, uid):
            if 'pending' in t['status'].lower():
                t['uid'] = uid; t['uname'] = name; out.append(t)
    return out

def get_by_status(db, kw):
    out = []
    for uid, d in db.items():
        name = d.get('👤 Profile',{}).get('Name', uid)
        for t in get_tasks(db, uid):
            s = t['status'].lower()
            if kw == 'approved' and any(x in s for x in ['approved','success']):
                t['uid'] = uid; t['uname'] = name; out.append(t)
            elif kw == 'rejected' and any(x in s for x in ['reject','fail']):
                t['uid'] = uid; t['uname'] = name; out.append(t)
    return out

def build_context(db, q):
    q = q.lower(); parts = []
    if any(k in q for k in ['balance','evlo','athigam','wallet']):
        top = sorted(get_users(db), key=lambda x: x['aff'], reverse=True)[:10]
        parts.append("TOP BALANCES:"); 
        for i,u in enumerate(top,1): parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['aff']:,.0f}")
    if any(k in q for k in ['task','earn','commission']):
        top = sorted(get_users(db), key=lambda x: x['task'], reverse=True)[:10]
        parts.append("TOP TASK EARNERS:")
        for i,u in enumerate(top,1): parts.append(f"{i}. {u['name']} ({u['id']}): ₹{u['task']:,.0f}")
    if any(k in q for k in ['pending','waiting']):
        p = get_pending_all(db); parts.append(f"PENDING: {len(p)}")
        for t in p[:10]: parts.append(f"- {t['uname']} | {t['type']} | {t['desc'][:50]}... | {t['amt']}")
    if any(k in q for k in ['approved','success']):
        parts.append(f"APPROVED: {len(get_by_status(db,'approved'))}")
    if any(k in q for k in ['rejected','fail']):
        parts.append(f"REJECTED: {len(get_by_status(db,'rejected'))}")
    if any(k in q for k in ['overview','summary','total','stats']):
        s = get_stats(db); parts.append(f"STATS: Users={s['users']}, Affiliate=₹{s['aff']:,.0f}, Task=₹{s['task']:,.0f}, Pending={s['pend']}, Approved={s['appr']}, Rejected={s['rej']}")
    if not parts:
        s = get_stats(db); parts.append(f"SUMMARY: {s['users']} users, Affiliate=₹{s['aff']:,.0f}, Task=₹{s['task']:,.0f}")
        for u in get_users(db)[:5]: parts.append(f"- {u['name']} ({u['id']})")
    return '\n'.join(parts)

def ask_ai(question, db, history=None):
    ctx = build_context(db, question)
    prompt = f"""You are SBO AI Assistant. Answer in the SAME LANGUAGE as the user (Tamil/English/Tanglish).
Be concise. Use bullet points.

DATABASE:
{ctx}

QUESTION: {question}"""
    try:
        msgs = []
        if history:
            for h in history[-10:]: msgs.append({'role': h['role'], 'parts': [h['text']]})
        chat = model.start_chat(history=msgs)
        return chat.send_message(prompt).text
    except Exception as e:
        logger.error(f"AI error: {e}")
        return "⚠️ AI unavailable. Try again."

def ask_image(question, img_bytes, db):
    ctx = build_context(db, question)
    prompt = f"SBO AI Assistant. Analyze image.\n\nDATABASE:\n{ctx}\n\nQUESTION: {question}"
    try:
        return vision_model.generate_content([prompt, {"mime_type": "image/jpeg", "data": img_bytes}]).text
    except Exception as e:
        logger.error(f"Vision error: {e}")
        return "⚠️ Image analysis failed."

def tts(text):
    try:
        from gtts import gTTS
        text = text[:500] + "..." if len(text) > 500 else text
        lang = 'ta' if any(ord(c) > 127 for c in text[:50]) else 'en'
        fp = io.BytesIO(); gTTS(text=text, lang=lang, slow=False).write_to_fp(fp); fp.seek(0)
        return fp.read()
    except: return None

def log_user(uid, uname, action):
    if uid not in user_stats: user_stats[uid] = {'username': uname, 'count': 0, 'actions': []}
    user_stats[uid]['count'] += 1
    user_stats[uid]['actions'].append({'a': action, 't': datetime.now().isoformat()})

def get_hist(uid):
    if uid not in conversation_history: conversation_history[uid] = []
    return conversation_history[uid]

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('💬 Ask AI', callback_data='ask'), InlineKeyboardButton('📊 Overview', callback_data='over')],
        [InlineKeyboardButton('💰 Balances', callback_data='bal'), InlineKeyboardButton('📋 Tasks', callback_data='task')],
        [InlineKeyboardButton('🏆 Top', callback_data='top'), InlineKeyboardButton('📸 Photo', callback_data='img')],
        [InlineKeyboardButton('🔊 Voice', callback_data='voice'), InlineKeyboardButton('❓ Help', callback_data='help')],
    ])

def kb_quick():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Yaruku balance athigama?', callback_data='q_bal')],
        [InlineKeyboardButton('SBOAFP3350 nominee yaaru?', callback_data='q_nom')],
        [InlineKeyboardButton('Review tasks summary', callback_data='q_rev')],
        [InlineKeyboardButton('Brief overview SBOAFP2209', callback_data='q_over')],
        [InlineKeyboardButton('Pending tasks', callback_data='q_pend')],
        [InlineKeyboardButton('⬅️ Back', callback_data='back')],
    ])

def kb_tasks():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⏳ Pending', callback_data='t_pend'), InlineKeyboardButton('✅ Approved', callback_data='t_appr')],
        [InlineKeyboardButton('❌ Rejected', callback_data='t_rej'), InlineKeyboardButton('📊 Stats', callback_data='t_stat')],
        [InlineKeyboardButton('⬅️ Back', callback_data='back')],
    ])

def kb_bal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🏆 Top Affiliate', callback_data='b_aff')],
        [InlineKeyboardButton('💼 Top Task Earners', callback_data='b_task')],
        [InlineKeyboardButton('💳 Top Credited', callback_data='b_cred')],
        [InlineKeyboardButton('📈 Global Stats', callback_data='b_stat')],
        [InlineKeyboardButton('⬅️ Back', callback_data='back')],
    ])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📢 Broadcast', callback_data='a_bc'), InlineKeyboardButton('📊 Stats', callback_data='a_stat')],
        [InlineKeyboardButton('🔄 Refresh', callback_data='a_ref'), InlineKeyboardButton('👥 Users', callback_data='a_users')],
        [InlineKeyboardButton('⬅️ Back', callback_data='back')],
    ])

def fmt_user(u):
    return (f"👤 <b>{u['name']}</b>\n🆔 <code>{u['id']}</code>\n📧 {u['email']}\n📱 {u['phone']}\n\n"
            f"💰 <b>Wallets</b>\n  • Affiliate: <code>₹{u['aff']:,.0f}</code>\n  • Task: <code>₹{u['task']:,.0f}</code>\n"
            f"  • Credited: <code>₹{u['cred']:,.0f}</code>\n  • Referral: <code>₹{u['ref']:,.0f}</code>\n"
            f"  • Intro: <code>₹{u['intro']:,.0f}</code>\n\n"
            f"📊 Tasks: {u['total_tasks']} | ⏳{u['pending']} | ✅{u['approved']} | ❌{u['rejected']}\n"
            f"📱 {u['device']} | 🕐 {u['last_login']}")

def fmt_tasks(tasks, title, limit=15):
    if not tasks: return f"📭 <b>{title}</b>\n\nNo tasks."
    lines = [f"📋 <b>{title} ({len(tasks)})</b>\n"]
    for i,t in enumerate(tasks[:limit],1):
        em = '⏳' if 'pending' in t['status'].lower() else '✅' if any(x in t['status'].lower() for x in ['approved','success']) else '❌'
        d = str(t['desc'])[:60]; 
        if len(str(t['desc'])) > 60: d += '...'
        lines.append(f"{i}. {em} <b>{t['type']}</b> | {t['amt']}\n   📝 {d}\n   👤 {t.get('uname','N/A')} | 📅 {t['date']}\n")
    if len(tasks) > limit: lines.append(f"\n... +{len(tasks)-limit} more")
    return '\n'.join(lines)

def fmt_leader(users, metric, title):
    if not users: return f"🏆 <b>{title}</b>\n\nNo data."
    lines = [f"🏆 <b>{title}</b>\n"]
    medals = ['🥇','🥈','🥉','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟']
    for i,u in enumerate(users,1):
        m = medals[i-1] if i <= 10 else f"{i}."
        v = u.get(metric, 0)
        vs = f"₹{v:,.0f}" if isinstance(v, (int,float)) and metric != 'wd_count' else str(v)
        lines.append(f"{m} <b>{u['name']}</b> ({u['id']})\n   └ {vs}\n")
    return '\n'.join(lines)

async def try_direct(db, q, update):
    q = q.lower().strip()
    # overview
    m = re.search(r'(?:overview|details|info|about)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not m: m = re.search(r'(SBO[A-Z0-9]+)\s+(?:overview|details|info)', q, re.I)
    if m:
        u = find_user(db, m.group(1).upper())
        return fmt_user(u) if u else f"❌ User <code>{m.group(1).upper()}</code> not found."
    # nominee
    m = re.search(r'(?:nominee\s+(?:of\s+)?)(SBO[A-Z0-9]+)', q, re.I)
    if not m: m = re.search(r'(SBO[A-Z0-9]+)\s+nominee', q, re.I)
    if m:
        sid = m.group(1).upper()
        if sid in db:
            n = db[sid].get('👥 Nominee', {})
            return f"👥 <b>Nominee for {sid}</b>\nName: <b>{n.get('Nominee Name','N/A')}</b>\nEmail: {n.get('Nominee Email','N/A')}\nPhone: {n.get('Nominee Phone','N/A')}"
        return f"❌ No nominee for <code>{sid}</code>."
    # bank
    m = re.search(r'(?:bank|pan|kyc)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not m: m = re.search(r'(SBO[A-Z0-9]+)\s+(?:bank|pan|kyc)', q, re.I)
    if m:
        sid = m.group(1).upper()
        if sid in db:
            b = db[sid].get('🏦 Bank & PAN', {})
            return (f"🏦 <b>Bank for {sid}</b>\nBank: <b>{b.get('Bank Name','N/A')}</b>\n"
                    f"Holder: {b.get('Account Holder','N/A')}\nAccount: <code>{b.get('Account Number','N/A')}</code>\n"
                    f"IFSC: <code>{b.get('IFSC Code','N/A')}</code>\nBranch: {b.get('Branch','N/A')}\n"
                    f"PAN: <code>{b.get('PAN Number','N/A')}</code>")
        return f"❌ No bank for <code>{sid}</code>."
    # tasks
    m = re.search(r'(?:tasks|works|history)\s+(?:of\s+)?(SBO[A-Z0-9]+)', q, re.I)
    if not m: m = re.search(r'(SBO[A-Z0-9]+)\s+(?:tasks|works|history)', q, re.I)
    if m:
        sid = m.group(1).upper(); t = get_tasks(db, sid)
        if t: return fmt_tasks(t, f"Tasks for {sid}")
        return f"❌ No tasks for <code>{sid}</code>."
    # top balance
    if any(k in q for k in ['top balance','athigama','highest balance']):
        return fmt_leader(sorted(get_users(db), key=lambda x: x['aff'], reverse=True)[:5], 'aff', 'Top Affiliate Balances')
    # top task
    if any(k in q for k in ['top task','task earner']):
        return fmt_leader(sorted(get_users(db), key=lambda x: x['task'], reverse=True)[:5], 'task', 'Top Task Earners')
    # pending
    if any(k in q for k in ['pending task','waiting task']):
        return fmt_tasks(get_pending_all(db), 'Pending Tasks')
    # approved
    if any(k in q for k in ['approved task','success task']):
        return fmt_tasks(get_by_status(db, 'approved'), 'Approved Tasks')
    # rejected
    if any(k in q for k in ['rejected task','fail task']):
        return fmt_tasks(get_by_status(db, 'rejected'), 'Rejected Tasks')
    # stats
    if any(k in q for k in ['global stat','total user','overview','summary','all stat']):
        s = get_stats(db)
        return (f"📊 <b>Global Statistics</b>\n\n👥 Users: <b>{s['users']}</b>\n"
                f"💰 Affiliate: <code>₹{s['aff']:,.0f}</code>\n💼 Task: <code>₹{s['task']:,.0f}</code>\n"
                f"💳 Credited: <code>₹{s['cred']:,.0f}</code>\n⏳ Pending: <b>{s['pend']}</b>\n"
                f"✅ Approved: <b>{s['appr']}</b>\n❌ Rejected: <b>{s['rej']}</b>")
    return None

async def process(update, question, img_bytes=None):
    user = update.effective_user
    uid = user.id
    await update.message.chat.send_action(action='typing')
    db = fetch_firebase()
    if db is None:
        await update.message.reply_text("⚠️ Database error. Try again.")
        return
    hist = get_hist(uid)
    resp = await try_direct(db, question, update)
    if resp is None:
        resp = ask_image(question, img_bytes, db) if img_bytes else ask_ai(question, db, hist)
    hist.append({'role': 'user', 'text': question})
    hist.append({'role': 'model', 'text': resp})
    if len(hist) > 20: hist[:] = hist[-20:]
    if len(resp) > 4000:
        for i in range(0, len(resp), 4000):
            await update.message.reply_text(resp[i:i+4000], parse_mode='HTML')
    else:
        await update.message.reply_text(resp, parse_mode='HTML')
    if uid in voice_enabled:
        vb = tts(resp)
        if vb: await update.message.reply_voice(voice=InputFile(io.BytesIO(vb), filename='reply.ogg'))

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    log_user(u.id, u.username or u.first_name, '/start')
    await update.message.reply_text(
        f"🤖 <b>SBO AI Assistant</b>\n\nVanakkam <b>{u.first_name}</b>! 👋\n\n"
        f"Naan unga SBO smart AI assistant. Ask me anything in <b>English, Tamil, or Tanglish</b>!\n\n<b>Quick Actions:</b>",
        parse_mode='HTML', reply_markup=kb_main())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 <b>Help</b>\n\n<b>Commands:</b>\n"
        "• /start - Start bot\n• /ask &lt;q&gt; - Ask AI\n• /menu - Show menu\n"
        "• /voice - Toggle voice\n• /dbinfo - DB info\n• /status - Bot status\n"
        "• /help - This message\n\n<b>Admin:</b>\n• /admin - Panel\n• /broadcast &lt;msg&gt;\n• /stats",
        parse_mode='HTML')

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 <b>Main Menu</b>\n\nChoose:", parse_mode='HTML', reply_markup=kb_main())

async def voice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in voice_enabled:
        voice_enabled.discard(uid); await update.message.reply_text("🔇 <b>Voice OFF</b>", parse_mode='HTML')
    else:
        voice_enabled.add(uid); await update.message.reply_text("🔊 <b>Voice ON</b>\n\nAI answers as voice too!", parse_mode='HTML')

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = ' '.join(ctx.args)
    u = update.effective_user
    log_user(u.id, u.username or u.first_name, '/ask')
    if not q:
        await update.message.reply_text("❓ <b>Ask AI</b>\n\nType question or choose:", parse_mode='HTML', reply_markup=kb_quick())
        return
    await process(update, q)

async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    log_user(u.id, u.username or u.first_name, 'msg')
    await process(update, update.message.text)

async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    cap = update.message.caption or 'What is in this image?'
    log_user(u.id, u.username or u.first_name, 'photo')
    await update.message.chat.send_action(action='typing')
    pf = await update.message.photo[-1].get_file()
    pb = await pf.download_as_bytearray()
    await process(update, cap, bytes(pb))

async def dbinfo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action='typing')
    db = fetch_firebase(force=True)
    if not db: await update.message.reply_text("⚠️ DB error."); return
    s = get_stats(db); u = get_users(db)[:5]
    text = f"📊 <b>DB Info</b>\n\nEntries: <b>{s['users']}</b>\nAffiliate: <code>₹{s['aff']:,.0f}</code>\nTask: <code>₹{s['task']:,.0f}</code>\nCredited: <code>₹{s['cred']:,.0f}</code>\n\n<b>Sample:</b>\n"
    for x in u: text += f"• <code>{x['id']}</code> - {x['name']}\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = fetch_firebase(); s = get_stats(db) if db else {}
    await update.message.reply_text(
        f"🤖 <b>Status</b>\n\n• DB: {'✅' if db else '❌'}\n• Entries: {s.get('users','N/A')}\n"
        f"• Users: {len(user_stats)}\n• AI: Gemini 2.0 Flash\n• Ver: 3.2\n"
        f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", parse_mode='HTML')

async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); d = q.data; u = update.effective_user

    if d == 'voice':
        if u.id in voice_enabled:
            voice_enabled.discard(u.id); await q.edit_message_text("🔇 <b>Voice OFF</b>\n\nMain Menu:", parse_mode='HTML', reply_markup=kb_main())
        else:
            voice_enabled.add(u.id); await q.edit_message_text("🔊 <b>Voice ON</b>\n\nMain Menu:", parse_mode='HTML', reply_markup=kb_main())
        return
    if d == 'ask':
        await q.edit_message_text("💬 <b>Ask AI</b>\n\nType question or choose:\n\n<i>Ex:</i> <code>SBOAFP3350 overview</code>", parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'over':
        await q.edit_message_text("👤 <b>Overview</b>\n\nType: <code>SBOAFP3350 overview</code>", parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'bal':
        await q.edit_message_text("💰 <b>Balances</b>\n\nChoose:", parse_mode='HTML', reply_markup=kb_bal())
    elif d == 'task':
        await q.edit_message_text("📋 <b>Tasks</b>\n\nChoose:", parse_mode='HTML', reply_markup=kb_tasks())
    elif d == 'img':
        await q.edit_message_text("📸 <b>Photo</b>\n\nSend photo + caption.", parse_mode='HTML', reply_markup=kb_main())
    elif d == 'help':
        await q.edit_message_text("🆘 <b>Help</b>\n\n• Direct msg → AI\n• /ask &lt;q&gt; → Query\n• /voice → Toggle\n• /menu → Menu\n\n<i>Tamil/English/Tanglish!</i>", parse_mode='HTML', reply_markup=kb_main())
    elif d == 'back':
        await q.edit_message_text("📋 <b>Main Menu</b>\n\nChoose:", parse_mode='HTML', reply_markup=kb_main())
    elif d == 'q_bal':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_leader(sorted(get_users(db), key=lambda x: x['aff'], reverse=True)[:5], 'aff', 'Top Affiliate Balances'), parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'q_nom':
        await q.edit_message_text("👥 <b>Nominee</b>\n\nType: <code>SBOAFP3350 nominee</code>", parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'q_rev':
        db = fetch_firebase()
        if db:
            p = len(get_pending_all(db)); a = len(get_by_status(db,'approved')); r = len(get_by_status(db,'rejected'))
            await q.edit_message_text(f"📋 <b>Task Summary</b>\n\n⏳ Pending: <b>{p}</b>\n✅ Approved: <b>{a}</b>\n❌ Rejected: <b>{r}</b>", parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'q_over':
        await q.edit_message_text("👤 <b>Overview</b>\n\nType: <code>SBOAFP2209 overview</code>", parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'q_pend':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_tasks(get_pending_all(db), 'Pending Tasks'), parse_mode='HTML', reply_markup=kb_quick())
    elif d == 'b_aff':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_leader(sorted(get_users(db), key=lambda x: x['aff'], reverse=True)[:10], 'aff', 'Top Affiliate Balances'), parse_mode='HTML', reply_markup=kb_bal())
    elif d == 'b_task':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_leader(sorted(get_users(db), key=lambda x: x['task'], reverse=True)[:10], 'task', 'Top Task Earners'), parse_mode='HTML', reply_markup=kb_bal())
    elif d == 'b_cred':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_leader(sorted(get_users(db), key=lambda x: x['cred'], reverse=True)[:10], 'cred', 'Top Total Credited'), parse_mode='HTML', reply_markup=kb_bal())
    elif d == 'b_stat':
        db = fetch_firebase()
        if db:
            s = get_stats(db)
            await q.edit_message_text(f"📈 <b>Global Stats</b>\n\n👥 Users: <b>{s['users']}</b>\n💰 Affiliate: <code>₹{s['aff']:,.0f}</code>\n💼 Task: <code>₹{s['task']:,.0f}</code>\n💳 Credited: <code>₹{s['cred']:,.0f}</code>\n⏳ Pending: <b>{s['pend']}</b>\n✅ Approved: <b>{s['appr']}</b>\n❌ Rejected: <b>{s['rej']}</b>", parse_mode='HTML', reply_markup=kb_bal())
    elif d == 't_pend':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_tasks(get_pending_all(db), 'Pending Tasks'), parse_mode='HTML', reply_markup=kb_tasks())
    elif d == 't_appr':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_tasks(get_by_status(db,'approved'), 'Approved Tasks'), parse_mode='HTML', reply_markup=kb_tasks())
    elif d == 't_rej':
        db = fetch_firebase()
        if db: await q.edit_message_text(fmt_tasks(get_by_status(db,'rejected'), 'Rejected Tasks'), parse_mode='HTML', reply_markup=kb_tasks())
    elif d == 't_stat':
        db = fetch_firebase()
        if db:
            s = get_stats(db); total = s['appr'] + s['rej']; rate = s['appr'] / max(total, 1) * 100
            await q.edit_message_text(f"📊 <b>Task Stats</b>\n\n⏳ Pending: <b>{s['pend']}</b>\n✅ Approved: <b>{s['appr']}</b>\n❌ Rejected: <b>{s['rej']}</b>\n📈 Rate: <b>{rate:.1f}%</b>", parse_mode='HTML', reply_markup=kb_tasks())
    elif d.startswith('a_'):
        if ADMIN_ID and str(u.id) != str(ADMIN_ID):
            await q.edit_message_text('❌ Unauthorized!'); return
        if d == 'a_bc':
            await q.edit_message_text('📢 Use /broadcast &lt;msg&gt;', parse_mode='HTML', reply_markup=kb_admin())
        elif d == 'a_stat':
            await q.edit_message_text(f"📊 <b>Stats</b>\n\n• Users: {len(user_stats)}\n• Msgs: {sum(x.get('count',0) for x in user_stats.values())}\n• Voice: {len(voice_enabled)}", parse_mode='HTML', reply_markup=kb_admin())
        elif d == 'a_ref':
            fetch_firebase(force=True); await q.edit_message_text('✅ Cache refreshed!', reply_markup=kb_admin())
        elif d == 'a_users':
            db = fetch_firebase()
            if db:
                users = get_users(db)
                text = f"👥 <b>Users ({len(users)})</b>\n\n"
                for x in users[:20]: text += f"• <code>{x['id']}</code> - {x['name']}\n"
                if len(users) > 20: text += f"\n... +{len(users)-20} more"
                await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb_admin())

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if ADMIN_ID and str(u.id) != str(ADMIN_ID): await update.message.reply_text('❌ Unauthorized!'); return
    await update.message.reply_text('🔐 <b>Admin Panel</b>', parse_mode='HTML', reply_markup=kb_admin())

async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if ADMIN_ID and str(u.id) != str(ADMIN_ID): await update.message.reply_text('❌ Unauthorized!'); return
    msg = ' '.join(ctx.args)
    if not msg: await update.message.reply_text('Usage: /broadcast &lt;msg&gt;'); return
    if not user_stats: await update.message.reply_text('No users.'); return
    sent = failed = 0
    for uid in list(user_stats.keys()):
        try: await ctx.bot.send_message(chat_id=uid, text=f'📢 <b>Broadcast:</b>\n\n{msg}', parse_mode='HTML'); sent += 1
        except Exception as e: logger.error(f"BC fail {uid}: {e}"); failed += 1
    await update.message.reply_text(f'📢 Done!\n✅ {sent}\n❌ {failed}')

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if ADMIN_ID and str(u.id) != str(ADMIN_ID): await update.message.reply_text('❌ Unauthorized!'); return
    total = len(user_stats); msgs = sum(x.get('count',0) for x in user_stats.values())
    text = f"📊 <b>Stats</b>\n\n• Users: {total}\n• Messages: {msgs}\n\n<b>Recent:</b>\n"
    for uid, info in list(user_stats.items())[:10]:
        text += f"• {info.get('username','Unknown')} - {info.get('count',0)}\n"
    await update.message.reply_text(text, parse_mode='HTML')

async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Error: {ctx.error}')
    if update and update.effective_message: await update.effective_message.reply_text('⚠️ Error. Try again.')

def main():
    logger.info("🤖 SBO Bot v3.2 starting...")
    db = fetch_firebase(force=True)
    logger.info(f"✅ DB: {len(db) if db else 'N/A'} entries")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('menu', menu_cmd))
    app.add_handler(CommandHandler('ask', ask_cmd))
    app.add_handler(CommandHandler('voice', voice_cmd))
    app.add_handler(CommandHandler('dbinfo', dbinfo_cmd))
    app.add_handler(CommandHandler('status', status_cmd))
    app.add_handler(CommandHandler('admin', admin_cmd))
    app.add_handler(CommandHandler('broadcast', broadcast_cmd))
    app.add_handler(CommandHandler('stats', stats_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_error_handler(error_handler)

    logger.info(f"WEBHOOK_URL={WEBHOOK_URL}, PORT={PORT}")
    if WEBHOOK_URL:
        wu = f"{WEBHOOK_URL.rstrip('/')}/webhook"
        logger.info(f"🌐 WEBHOOK: {wu}")
        app.run_webhook(listen='0.0.0.0', port=PORT, webhook_url=wu, drop_pending_updates=True)
    else:
        logger.info("🔄 POLLING")
        app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
