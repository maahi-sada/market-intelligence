import requests
import google.generativeai as genai
import asyncio
import schedule
import time
import json
import os
from datetime import datetime, date
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Config ─────────────────────────────────────
TELEGRAM_TOKEN   = "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI"
TELEGRAM_CHAT_ID = "787902453"
GEMINI_API_KEY   = "AIzaSyC-xp1LY-YykJtX6gp8kNw8jNXf2q2u2Ek"

# ── Setup ───────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model    = genai.GenerativeModel("gemini-2.5-flash")
seen_ann = set()
seen_oi  = set()

# ════════════════════════════════════════════════
# NSE SESSION
# ════════════════════════════════════════════════
def get_nse_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    })
    try:
        session.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
    except Exception as e:
        print(f"⚠️ NSE session warning: {e}")
    return session

# ════════════════════════════════════════════════
# PHASE 1 — ANNOUNCEMENTS (auto alerts)
# ════════════════════════════════════════════════
def fetch_announcements(session):
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ NSE announcements: {len(data)} items")
            return data
        print(f"⚠️ Announcements status: {resp.status_code}")
    except Exception as e:
        print(f"❌ Announcements error: {e}")
    return []

def classify(company, subject):
    prompt = f"""You are a senior Indian stock market analyst.
Analyze this NSE corporate announcement.
Respond ONLY in raw JSON. No markdown. No extra text.

{{"category":"RESULTS or DIVIDEND or BUYBACK or BOARD_MEETING or MERGER or CONCALL or GUIDANCE or OTHER","impact":"HIGH or MEDIUM or LOW","score":<1-10>,"summary":"<2 sentences what this means for stock price>"}}

Company: {company}
Announcement: {subject}
Scoring: 8-10=strong mover, 5-7=moderate, 1-4=routine"""
    try:
        r = model.generate_content(prompt)
        text = r.text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        print(f"❌ Gemini error: {e}")
        return None

async def send_announcement_alert(bot, company, result, ann_time):
    ie = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(result["impact"],"⚪")
    ce = {"RESULTS":"📊","DIVIDEND":"💰","BUYBACK":"🔄","BOARD_MEETING":"📋",
          "MERGER":"🤝","CONCALL":"📞","GUIDANCE":"🎯","OTHER":"📌"}.get(result["category"],"📌")
    msg = (
        f"{ie} *{result['impact']} IMPACT ALERT*\n\n"
        f"🏢 *Company:* {company}\n"
        f"{ce} *Category:* {result['category']}\n"
        f"⚡ *Score:* {result['score']}/10\n"
        f"📝 *Summary:* {result['summary']}\n"
        f"🕐 *Time:* {ann_time}\n"
        f"🔗 *Exchange:* NSE"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

def check_announcements(bot):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking announcements...")
    session = get_nse_session()
    anns    = fetch_announcements(session)
    sent    = 0
    for ann in anns[:15]:
        ann_id = (ann.get("an_dt","") + ann.get("symbol","") + ann.get("desc","")[:20])
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)
        company  = ann.get("sm_name", ann.get("symbol","Unknown"))
        subject  = ann.get("desc","")
        ann_time = ann.get("an_dt", datetime.now().strftime("%Y-%m-%d %H:%M"))
        print(f"  🔍 {company} — {subject[:60]}")
        result = classify(company, subject)
        if not result:
            continue
        if result["score"] >= 4:
            asyncio.run(send_announcement_alert(bot, company, result, ann_time))
            print(f"  ✅ Alert: {company} | {result['category']} | {result['score']}/10")
            sent += 1
        else:
            print(f"  ⏭ Low score ({result['score']}): {company}")
        time.sleep(1)
    print(f"  Done. Sent: {sent}")

# ════════════════════════════════════════════════
# PHASE 2 — OI SPIKES (auto alerts)
# ════════════════════════════════════════════════
def fetch_oi_spikes(session):
    url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        print(f"❌ OI error: {e}")
    return []

def fetch_oi_gainers_losers(session):
    results = {}
    urls = {
        "long_buildup":  "https://www.nseindia.com/api/live-analysis-variations?index=gainers&limit=10",
        "short_buildup": "https://www.nseindia.com/api/live-analysis-variations?index=loosers&limit=10",
    }
    for key, url in urls.items():
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                results[key] = (resp.json().get("data") or [])[:5]
        except:
            results[key] = []
    return results

async def send_oi_report(bot, chat_id, spikes, gainers_losers):
    now = datetime.now().strftime("%H:%M IST")
    significant = [s for s in spikes if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
    if not significant:
        await bot.send_message(chat_id=chat_id, text="📈 *OI Report* — No significant spikes right now.", parse_mode="Markdown")
        return

    msg = f"📈 *OI INTELLIGENCE REPORT*\n🕐 {now}\n\n"
    msg += "*Top OI spurts:*\n"
    for s in significant[:6]:
        oi  = s.get("oiChange") or s.get("perOIChange") or 0
        ltp = s.get("lastPrice") or s.get("ltp") or 0
        sym = s.get("symbol","?")
        sig = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
        msg += f"{sig} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"

    long_b  = gainers_losers.get("long_buildup",[])
    short_b = gainers_losers.get("short_buildup",[])
    if long_b:
        msg += f"\n📗 *Long buildup:* " + " | ".join([s.get("symbol","?") for s in long_b[:4]])
    if short_b:
        msg += f"\n📕 *Short buildup:* " + " | ".join([s.get("symbol","?") for s in short_b[:4]])
    msg += "\n\n_Data: NSE live F&O_"

    await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

def check_oi(bot):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking OI...")
    session        = get_nse_session()
    spikes         = fetch_oi_spikes(session)
    gainers_losers = fetch_oi_gainers_losers(session)
    if not spikes:
        print("  No OI data")
        return
    asyncio.run(send_oi_report(bot, TELEGRAM_CHAT_ID, spikes, gainers_losers))
    print("  ✅ OI report sent")

# ════════════════════════════════════════════════
# PHASE 2.5 — Q&A COMMANDS
# ════════════════════════════════════════════════

# /nifty — Live indices
async def cmd_nifty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching live data...")
    session = get_nse_session()
    indices = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX"]
    msg = "📊 *LIVE MARKET SNAPSHOT*\n"
    msg += f"🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
    try:
        url  = "https://www.nseindia.com/api/allIndices"
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            for item in data:
                name = item.get("index","")
                if name in indices:
                    ltp    = item.get("last", 0)
                    chg    = item.get("change", 0)
                    pct    = item.get("percentChange", 0)
                    emoji  = "🟢" if chg >= 0 else "🔴"
                    msg   += f"{emoji} *{name}*\n"
                    msg   += f"   ₹{ltp:,.2f}  {'+' if chg>=0 else ''}{chg:.2f} ({pct:+.2f}%)\n\n"
        else:
            msg += "⚠️ Could not fetch live data right now."
    except Exception as e:
        msg += f"❌ Error: {e}"
    await update.message.reply_text(msg, parse_mode="Markdown")

# /holiday — Market holidays
async def cmd_holiday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Checking market holidays...")
    session = get_nse_session()
    try:
        url  = "https://www.nseindia.com/api/holiday-master?type=trading"
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data     = resp.json()
            holidays = data.get("CM", [])
            today    = date.today()
            today_str = today.strftime("%d-%b-%Y").upper()

            # Check if today is holiday
            today_holiday = next((h for h in holidays if today_str in h.get("tradingDate","").upper()), None)

            msg = "📅 *MARKET HOLIDAYS*\n\n"
            if today_holiday:
                msg += f"🔴 *TODAY IS A HOLIDAY*\n"
                msg += f"Reason: {today_holiday.get('description','')}\n\n"
            else:
                msg += f"🟢 *Market is open today*\n\n"

            # Show upcoming holidays this month
            current_month = today.strftime("%b").upper()
            month_holidays = [h for h in holidays if current_month in h.get("tradingDate","").upper()]

            if month_holidays:
                msg += f"*Holidays this month ({today.strftime('%B %Y')}):*\n"
                for h in month_holidays:
                    msg += f"📌 {h.get('tradingDate','')} — {h.get('description','')}\n"
            else:
                msg += f"✅ No more holidays this month."
        else:
            msg = "⚠️ Could not fetch holiday data. Check: nseindia.com"
    except Exception as e:
        msg = f"❌ Error fetching holidays: {e}"
    await update.message.reply_text(msg, parse_mode="Markdown")

# /earnings — Today's results
async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching today's earnings...")
    session = get_nse_session()
    try:
        today = datetime.now().strftime("%d-%m-%Y")
        url   = f"https://www.nseindia.com/api/event-calendar?index=equities"
        resp  = session.get(url, timeout=15)
        if resp.status_code == 200:
            events = resp.json()
            today_fmt = datetime.now().strftime("%Y-%m-%d")
            results = [
                e for e in events
                if "Financial Results" in e.get("purpose", "")
                and today_fmt in e.get("date", "")
            ]
            msg = f"📊 *TODAY'S EARNINGS — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if results:
                msg += f"*{len(results)} companies reporting today:*\n\n"
                for r in results[:20]:
                    sym  = r.get("symbol","?")
                    name = r.get("companyName", sym)
                    msg += f"📋 *{sym}* — {name}\n"
                if len(results) > 20:
                    msg += f"\n_...and {len(results)-20} more_"
            else:
                msg += "No companies scheduled to report results today."
        else:
            msg = "⚠️ Could not fetch earnings calendar right now."
    except Exception as e:
        msg = f"❌ Error: {e}"
    await update.message.reply_text(msg, parse_mode="Markdown")

# /ban — F&O ban list
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching F&O ban list...")
    try:
        url  = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
        resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        if resp.status_code == 200:
            lines  = resp.text.strip().split("\n")
            stocks = [l.strip() for l in lines[1:] if l.strip()]
            msg = f"🚫 *F&O BAN LIST — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if stocks:
                msg += f"*{len(stocks)} stocks banned:*\n"
                for i, s in enumerate(stocks, 1):
                    msg += f"{i}. {s}\n"
                msg += "\n⚠️ _No fresh F&O positions allowed._"
            else:
                msg += "✅ No stocks in ban period today."
        else:
            msg = "⚠️ Could not fetch ban list right now."
    except Exception as e:
        msg = f"❌ Error: {e}"
    await update.message.reply_text(msg, parse_mode="Markdown")

# /oi — On-demand OI report
async def cmd_oi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching OI data...")
    session        = get_nse_session()
    spikes         = fetch_oi_spikes(session)
    gainers_losers = fetch_oi_gainers_losers(session)
    await send_oi_report(context.bot, update.effective_chat.id, spikes, gainers_losers)

# /help — Command list
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Market Intelligence Bot — Commands*\n\n"
        "📊 /nifty — Live Nifty, Bank Nifty, IT, VIX\n"
        "📅 /holiday — Today's status + month holidays\n"
        "📋 /earnings — Companies reporting results today\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI spike report on demand\n"
        "❓ /help — This menu\n\n"
        "_Auto alerts fire every 3 min for announcements_\n"
        "_OI intelligence report fires every 30 min_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# /start
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_help(update, context)

# ════════════════════════════════════════════════
# SCHEDULER — runs in background thread
# ════════════════════════════════════════════════
def run_scheduler(bot):
    schedule.every(3).minutes.do(check_announcements, bot=bot)
    schedule.every(30).minutes.do(check_oi, bot=bot)
    print("📡 Scheduler started")
    while True:
        schedule.run_pending()
        time.sleep(10)

# ════════════════════════════════════════════════
# MAIN — runs bot + scheduler together
# ════════════════════════════════════════════════
async def startup_message(app):
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "✅ *Market Intelligence Bot LIVE*\n\n"
            "📡 Phase 1: NSE announcements — every 3 min\n"
            "📈 Phase 2: OI intelligence — every 30 min\n"
            "💬 Phase 2.5: Q&A commands active\n\n"
            "Type /help to see all commands"
        ),
        parse_mode="Markdown"
    )

def main():
    print("🚀 Starting Market Intelligence Bot...")

    # Build Telegram application
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("nifty",    cmd_nifty))
    app.add_handler(CommandHandler("holiday",  cmd_holiday))
    app.add_handler(CommandHandler("earnings", cmd_earnings))
    app.add_handler(CommandHandler("ban",      cmd_ban))
    app.add_handler(CommandHandler("oi",       cmd_oi))

    # Run scheduler in background thread
    import threading
    bot = app.bot
    t = threading.Thread(target=run_scheduler, args=(bot,), daemon=True)
    t.start()

    # Send startup message then start polling
    async def post_init(app):
        await startup_message(app)

    app.post_init = post_init

    print("✅ Bot polling started — waiting for commands...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
