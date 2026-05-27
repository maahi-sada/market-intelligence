import requests
import google.generativeai as genai
import asyncio
import schedule
import time
import json
import os
from datetime import datetime, date
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

TELEGRAM_TOKEN   = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
GEMINI_API_KEY   = "YOUR_GEMINI_KEY"

genai.configure(api_key=GEMINI_API_KEY)
model    = genai.GenerativeModel("gemini-2.5-flash")
seen_ann = set()

def get_nse_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        session.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
    except:
        pass
    return session

def classify(company, subject):
    prompt = f"""You are a senior Indian stock market analyst.
Analyze this NSE announcement. Respond ONLY in raw JSON, no markdown.
{{"category":"RESULTS or DIVIDEND or BUYBACK or BOARD_MEETING or MERGER or CONCALL or GUIDANCE or OTHER","impact":"HIGH or MEDIUM or LOW","score":<1-10>,"summary":"<2 sentences>"}}
Company: {company}
Announcement: {subject}"""
    try:
        r = model.generate_content(prompt)
        text = r.text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except:
        return None

def send_msg(bot, chat_id, text):
    try:
        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"Telegram error: {e}")

def check_announcements(bot):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking announcements...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/corporate-announcements?index=equities", timeout=15)
        if resp.status_code != 200:
            print(f"NSE status: {resp.status_code}")
            return
        anns = resp.json()
        print(f"✅ Got {len(anns)} announcements")
    except Exception as e:
        print(f"NSE error: {e}")
        return

    sent = 0
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
            ie = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(result["impact"],"⚪")
            ce = {"RESULTS":"📊","DIVIDEND":"💰","BUYBACK":"🔄","BOARD_MEETING":"📋","MERGER":"🤝","CONCALL":"📞","GUIDANCE":"🎯","OTHER":"📌"}.get(result["category"],"📌")
            msg = (f"{ie} *{result['impact']} IMPACT*\n\n"
                   f"🏢 *{company}*\n"
                   f"{ce} {result['category']} | ⚡ {result['score']}/10\n"
                   f"📝 {result['summary']}\n"
                   f"🕐 {ann_time}")
            send_msg(bot, TELEGRAM_CHAT_ID, msg)
            print(f"  ✅ Sent: {company} | {result['score']}/10")
            sent += 1
        time.sleep(1)
    print(f"Done. Sent: {sent}")

def check_oi(bot):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking OI...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings", timeout=15)
        if resp.status_code != 200:
            return
        stocks = resp.json().get("data", [])
        significant = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not significant:
            print("No significant OI spikes")
            return
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
        for s in significant[:6]:
            oi  = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol","?")
            sig = "🔴" if oi>50 else "🟡" if oi>25 else "🟢"
            msg += f"{sig} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(bot, TELEGRAM_CHAT_ID, msg)
        print("✅ OI alert sent")
    except Exception as e:
        print(f"OI error: {e}")

# ── Commands ──────────────────────────────────
def cmd_start(update: Update, context: CallbackContext):
    cmd_help(update, context)

def cmd_help(update: Update, context: CallbackContext):
    msg = ("🤖 *Market Intelligence Bot*\n\n"
           "📊 /nifty — Live indices\n"
           "📅 /holiday — Market holidays\n"
           "📋 /earnings — Today's results\n"
           "🚫 /ban — F\\&O ban list\n"
           "📈 /oi — OI spike report\n"
           "❓ /help — This menu")
    update.message.reply_text(msg, parse_mode="Markdown")

def cmd_nifty(update: Update, context: CallbackContext):
    update.message.reply_text("⏳ Fetching live data...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            targets = ["NIFTY 50","NIFTY BANK","NIFTY IT","INDIA VIX"]
            msg = f"📊 *LIVE INDICES*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
            for item in data:
                if item.get("index") in targets:
                    ltp = item.get("last",0)
                    chg = item.get("change",0)
                    pct = item.get("percentChange",0)
                    e   = "🟢" if chg>=0 else "🔴"
                    msg += f"{e} *{item['index']}*\n₹{ltp:,.2f}  {'+' if chg>=0 else ''}{chg:.2f} ({pct:+.2f}%)\n\n"
            update.message.reply_text(msg, parse_mode="Markdown")
        else:
            update.message.reply_text("⚠️ Could not fetch data right now.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")

def cmd_holiday(update: Update, context: CallbackContext):
    update.message.reply_text("⏳ Checking holidays...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=15)
        if resp.status_code == 200:
            holidays = resp.json().get("CM", [])
            today    = date.today()
            month    = today.strftime("%b").upper()
            month_h  = [h for h in holidays if month in h.get("tradingDate","").upper()]
            msg = f"📅 *MARKET HOLIDAYS*\n\n"
            if month_h:
                msg += f"*{today.strftime('%B %Y')}:*\n"
                for h in month_h:
                    msg += f"📌 {h.get('tradingDate','')} — {h.get('description','')}\n"
            else:
                msg += "✅ No holidays this month."
            update.message.reply_text(msg, parse_mode="Markdown")
        else:
            update.message.reply_text("⚠️ Could not fetch holiday data.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")

def cmd_earnings(update: Update, context: CallbackContext):
    update.message.reply_text("⏳ Fetching today's earnings...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code == 200:
            events    = resp.json()
            today_fmt = datetime.now().strftime("%Y-%m-%d")
            results   = [e for e in events if "Financial Results" in e.get("purpose","") and today_fmt in e.get("date","")]
            msg = f"📊 *TODAY'S EARNINGS — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if results:
                msg += f"*{len(results)} companies reporting:*\n\n"
                for r in results[:20]:
                    msg += f"📋 *{r.get('symbol','?')}* — {r.get('companyName','')}\n"
            else:
                msg += "No results scheduled today."
            update.message.reply_text(msg, parse_mode="Markdown")
        else:
            update.message.reply_text("⚠️ Could not fetch earnings data.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")

def cmd_ban(update: Update, context: CallbackContext):
    update.message.reply_text("⏳ Fetching F&O ban list...")
    try:
        resp = requests.get("https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
                           headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        if resp.status_code == 200:
            lines  = resp.text.strip().split("\n")
            stocks = [l.strip() for l in lines[1:] if l.strip()]
            msg = f"🚫 *F&O BAN LIST — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if stocks:
                msg += f"*{len(stocks)} stocks banned:*\n"
                for i,s in enumerate(stocks,1):
                    msg += f"{i}. {s}\n"
                msg += "\n⚠️ _No fresh F&O positions allowed._"
            else:
                msg += "✅ No stocks in ban period today."
            update.message.reply_text(msg, parse_mode="Markdown")
        else:
            update.message.reply_text("⚠️ Could not fetch ban list.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")

def cmd_oi(update: Update, context: CallbackContext):
    update.message.reply_text("⏳ Fetching OI data...")
    check_oi(context.bot)

# ── Main ──────────────────────────────────────
def run_scheduler(bot):
    schedule.every(3).minutes.do(check_announcements, bot=bot)
    schedule.every(30).minutes.do(check_oi, bot=bot)
    while True:
        schedule.run_pending()
        time.sleep(10)

def main():
    print("🚀 Starting Market Intelligence Bot...")
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp      = updater.dispatcher

    dp.add_handler(CommandHandler("start",    cmd_start))
    dp.add_handler(CommandHandler("help",     cmd_help))
    dp.add_handler(CommandHandler("nifty",    cmd_nifty))
    dp.add_handler(CommandHandler("holiday",  cmd_holiday))
    dp.add_handler(CommandHandler("earnings", cmd_earnings))
    dp.add_handler(CommandHandler("ban",      cmd_ban))
    dp.add_handler(CommandHandler("oi",       cmd_oi))

    # Send startup message
    updater.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="✅ *Market Intelligence Bot LIVE*\n\n📡 NSE announcements — every 3 min\n📈 OI spikes — every 30 min\n💬 Commands active — type /help",
        parse_mode="Markdown"
    )

    # Start scheduler in background
    import threading
    t = threading.Thread(target=run_scheduler, args=(updater.bot,), daemon=True)
    t.start()

    print("✅ Bot started — polling for commands...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
