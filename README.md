# 🤖 SBO AI Bot - Enhanced Version

Tamil/English bilingual Telegram Bot powered by Google Gemini AI + Firebase Realtime Database.

---

## ✨ New Features (Enhanced Version)

### 🎨 User Experience
- **Interactive Inline Keyboard Menu** (`/menu`) - Button-based navigation
- **Image Analysis** - Send photos with questions, AI will analyze them
- **Conversation Memory** - Bot remembers last 10 messages for context
- **Typing Indicator** - Shows "typing..." while processing

### ⚡ Performance
- **Smart Caching** - Firebase data cached for 5 minutes to reduce API calls
- **Force Refresh** - `/dbinfo` fetches fresh data on demand
- **Error Recovery** - Falls back to cached data if Firebase fails

### 🔐 Admin Features
- **Admin Panel** (`/admin`) - Inline keyboard admin dashboard
- **Broadcast Messages** (`/broadcast`) - Send messages to all users
- **User Statistics** (`/stats`) - Track active users and message counts
- **Cache Refresh** - Force refresh database cache from admin panel

### 🛠️ Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message with menu |
| `/menu` | Interactive inline keyboard |
| `/ask <question>` | Ask AI about database |
| `/image` | Send photo for AI analysis |
| `/dbinfo` | View database contents |
| `/status` | Check bot health status |
| `/help` | Show help message |
| `/admin` | Admin panel (admin only) |
| `/broadcast <msg>` | Broadcast to all (admin only) |
| `/stats` | View statistics (admin only) |

---

## 🚀 Railway-ல Deploy பண்ணுவது எப்படி?

### Step 1: Prerequisites
1. [Railway](https://railway.app) account create பண்ணுங்க
2. [BotFather](https://t.me/BotFather)-ல போய் New Bot create பண்ணி **BOT_TOKEN** வாங்குங்க
3. [Google AI Studio](https://aistudio.google.com)-ல போய் **GEMINI_API_KEY** generate பண்ணுங்க
4. [Firebase](https://console.firebase.google.com)-ல Realtime Database create பண்ணி URL copy பண்ணுங்க

### Step 2: Deploy to Railway

#### Method 1: GitHub Integration (Recommended)
1. இந்த repo-அ உங்கள் GitHub-ல fork பண்ணுங்க
2. Railway Dashboard → New Project → Deploy from GitHub repo
3. உங்கள் repo-அ select பண்ணுங்க
4. **Variables** tab-ல கீழே உள்ள values add பண்ணுங்க:

```
BOT_TOKEN = your_telegram_bot_token
GEMINI_API_KEY = your_gemini_api_key
FIREBASE_URL = https://your-db.firebaseio.com/
ADMIN_ID = your_telegram_user_id (optional)
WEBHOOK_URL = https://your-app.up.railway.app/webhook
```

5. Deploy! 🎉

#### Method 2: Railway CLI
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Init project
railway init

# Deploy
railway up
```

### Step 3: Set Webhook URL
Railway-ல deploy ஆனதும், app-உடன் URL-அ copy பண்ணி:
```
WEBHOOK_URL = https://your-app-name.up.railway.app/webhook
```

---

## 🔧 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ Yes | Telegram Bot Token |
| `GEMINI_API_KEY` | ✅ Yes | Google Gemini API Key |
| `FIREBASE_URL` | ✅ Yes | Firebase RTDB URL |
| `ADMIN_ID` | ❌ No | Your Telegram numeric ID |
| `WEBHOOK_URL` | ❌ No | Railway app URL + /webhook |
| `PORT` | ❌ No | Auto-set by Railway |

---

## 📁 File Structure

```
Sbo-Tg-Bot/
├── bot.py              # Original bot (backup)
├── bot_enhanced.py     # Enhanced bot ⭐
├── requirements.txt    # Python dependencies
├── Procfile            # Railway process config
├── railway.toml        # Railway deployment config
├── runtime.txt         # Python version
├── .env.example        # Environment variables template
└── README.md           # This file
```

---

## 🔄 Webhook vs Polling

| Mode | When to Use | Config |
|------|-------------|--------|
| **Webhook** | Railway/Heroku/Production | Set `WEBHOOK_URL` |
| **Polling** | Local development | Don't set `WEBHOOK_URL` |

---

## 🐛 Troubleshooting

**Bot not responding?**
- Check `BOT_TOKEN` is correct
- Check `GEMINI_API_KEY` is valid
- Check Railway logs: `railway logs`

**Database not connecting?**
- Verify `FIREBASE_URL` ends with `/`
- Check Firebase rules allow read access

**Webhook errors?**
- Ensure `WEBHOOK_URL` matches your Railway domain
- Must end with `/webhook`

---

## 📝 License

Open Source - Feel free to modify and enhance!

---

**Developer:** SBO Team  
**Version:** 2.0 Enhanced  
**Last Updated:** 2026

