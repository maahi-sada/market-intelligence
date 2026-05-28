import requests
import google.generativeai as genai
import schedule
import time
import json
import os
import threading
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN   = "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI"
TELEGRAM_CHAT_ID = "787902453"
GEMINI_API_KEY   = "AIzaSyC-xp1LY-YykJtX6gp8kNw8jNXf2q2u2Ek"

genai.configure(api_key=GEMINI_API_KEY)
model    = genai.GenerativeModel("gemini-2.5-flash")
seen_ann = set()

def send_msg(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

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
        text = r.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except:
        return None
print(f" Gemini error: {e}")

def handle_nifty(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            targets = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX"]
            msg = f"📊 *LIVE INDICES*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
            for item in data:
                if item.get("index") in targets:
                    ltp = item.get("last", 0)
                    chg = item.get("change", 0)
                    pct = item.get("percentChange", 0)
                    e = "🟢" if chg >= 0 else "🔴"
                    msg += f"{e} *{item['index']}*\n₹{ltp:,.2f}  {'+' if chg>=0 else ''}{chg:.2f} ({pct:+.2f}%)\n\n"
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch data right now.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def handle_holiday(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=15)
        if resp.status_code == 200:
            holidays = resp.json().get("CM", [])
            today = date.today()
            month = today.strftime("%b").upper()
            month_h = [h for h in holidays if month in h.get("tradingDate", "").upper()]
            msg = f"📅 *MARKET HOLIDAYS — {today.strftime('%B %Y')}*\n\n"
            if month_h:
                for h in month_h:
                    msg += f"📌 {h.get('tradingDate','')} — {h.get('description','')}\n"
            else:
                msg += "✅ No holidays this month."
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch holiday data.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def handle_earnings(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code == 200:
            events = resp.json()
            today_fmt = datetime.now().strftime("%Y-%m-%d")
            results = [e for e in events if "Financial Results" in e.get("purpose", "") and today_fmt in e.get("date", "")]
            msg = f"📊 *TODAY'S EARNINGS — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if results:
                msg += f"*{len(results)} companies reporting:*\n\n"
                for r in results[:20]:
                    msg += f"📋 *{r.get('symbol','?')}* — {r.get('companyName','')}\n"
            else:
                msg += "No results scheduled today."
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch earnings data.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def handle_ban(chat_id):
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        if resp.status_code == 200:
            lines = resp.text.strip().split("\n")
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
            msg = "⚠️ Could not fetch ban list."
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def handle_oi(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings", timeout=15)
        stocks = resp.json().get("data", []) if resp.status_code == 200 else []
        sig = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            send_msg(chat_id, "📈 *OI Report* — No significant spikes right now.")
            return
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
        for s in sig[:6]:
            oi = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol", "?")
            e = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def handle_help(chat_id):
    send_msg(chat_id, (
        "🤖 *Market Intelligence Bot*\n\n"
        "📊 /nifty — Live indices\n"
        "📅 /holiday — Market holidays\n"
        "📋 /earnings — Today's results\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI spike report\n"
        "❓ /help — This menu\n\n"
        "_Auto alerts every 3 min_\n"
        "_OI report every 30 min_"
    ))

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        try:
            data = json.loads(body)
            message = data.get("message", {})
            text = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if not chat_id or not text:
                return
            print(f"Command: {text} from {chat_id}")
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/nifty":
                handle_nifty(chat_id)
            elif cmd == "/holiday":
                handle_holiday(chat_id)
            elif cmd == "/earnings":
                handle_earnings(chat_id)
            elif cmd == "/ban":
                handle_ban(chat_id)
            elif cmd == "/oi":
                handle_oi(chat_id)
            elif cmd in ["/help", "/start"]:
                handle_help(chat_id)
        except Exception as e:
            print(f"Webhook error: {e}")

    def log_message(self, format, *args):
        pass

def check_announcements():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking announcements...")
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=15
        )
        if resp.status_code != 200:
            print(f"NSE status: {resp.status_code}")
            return
        anns = resp.json()
        print(f"Got {len(anns)} announcements")
    except Exception as e:
        print(f"NSE error: {e}")
        return

    sent = 0
    for ann in anns[:15]:
        ann_id = ann.get("an_dt", "") + ann.get("symbol", "") + ann.get("desc", "")[:20]
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)
        company = ann.get("sm_name", ann.get("symbol", "Unknown"))
        subject = ann.get("desc", "")
        ann_time = ann.get("an_dt", datetime.now().strftime("%Y-%m-%d %H:%M"))
        result = classify(company, subject)
        score = result["score"] if result else 0
        print(f"  {score}/10 | {company[:35]}")
        if not result or score < 3:
            continue
        ie = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(result["impact"], "⚪")
        ce = {
            "RESULTS": "📊", "DIVIDEND": "💰", "BUYBACK": "🔄",
            "BOARD_MEETING": "📋", "MERGER": "🤝", "CONCALL": "📞",
            "GUIDANCE": "🎯", "OTHER": "📌"
        }.get(result["category"], "📌")
        msg = (
            f"{ie} *{result['impact']} IMPACT*\n\n"
            f"🏢 *{company}*\n"
            f"{ce} {result['category']} | ⚡ {result['score']}/10\n"
            f"📝 {result['summary']}\n"
            f"🕐 {ann_time}"
        )
        send_msg(TELEGRAM_CHAT_ID, msg)
        print(f"  ✅ Alert sent: {company} | {result['score']}/10")
        sent += 1
        time.sleep(1)
    print(f"Done. Sent: {sent}")

def check_oi_auto():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking OI...")
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            timeout=15
        )
        stocks = resp.json().get("data", []) if resp.status_code == 200 else []
        sig = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            print("No significant OI spikes")
            return
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
        for s in sig[:6]:
            oi = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol", "?")
            e = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(TELEGRAM_CHAT_ID, msg)
        print("✅ OI alert sent")
    except Exception as e:
        print(f"OI error: {e}")

def run_scheduler():
    schedule.every(3).minutes.do(check_announcements)
    schedule.every(30).minutes.do(check_oi_auto)
    while True:
        schedule.run_pending()
        time.sleep(10)

if __name__ == "__main__":
    print("🚀 Starting Market Intelligence Bot...")
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"✅ Webhook server on port {port}")
    send_msg(TELEGRAM_CHAT_ID,
        "✅ *Market Intelligence Bot LIVE*\n\n"
        "📡 NSE announcements — every 3 min\n"
        "📈 OI spikes — every 30 min\n"
        "💬 Type /help for commands"
    )
    threading.Thread(target=check_announcements, daemon=True).start()
    server.serve_forever()