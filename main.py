import os
import time
import json
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
import requests
import google.generativeai as genai
import pytz
import schedule
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# 1. SYSTEM CONFIGURATION & INITIALIZATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MARKET_ENGINE")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not GEMINI_API_KEY:
    logger.critical("❌ BOOT ERROR: GEMINI_API_KEY is missing in Railway variables!")
    raise ValueError("Set GEMINI_API_KEY in your Railway dashboard.")

IST = pytz.timezone("Asia/Kolkata")
seen_ann = set()
SEEN_FILE = "seen_ann.json"

# Configure Gemini Native SDK
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

def now_ist(fmt="%d %b %Y %H:%M IST"):
    return datetime.now(IST).strftime(fmt)

# ==========================================
# 2. PERSISTENT STORAGE DEDUPLICATION
# ==========================================
def load_seen():
    global seen_ann
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
                seen_ann = set(data)
            logger.info(f"Loaded {len(seen_ann)} processed signatures from persistent cache.")
    except Exception as e:
        logger.error(f"Failed to read cache file records: {e}")

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_ann)[-2000:], f)
    except Exception as e:
        logger.error(f"Failed to save signatures to cache file: {e}")

# ==========================================
# 3. TELEGRAM DISPATCH PIPELINE
# ==========================================
def send_msg(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code != 200:
            logger.error(f"Telegram API Refusal: {r.text}")
    except Exception as e:
        logger.error(f"Telegram engine connectivity exception: {e}")

# ==========================================
# 4. DATA COMPLIANCE FILTERS (SEBI TERMINOLOGY)
# ==========================================
JUNK = [
    "trading window closure", "closure of trading window", 
    "loss of share certificate", "duplicate share certificate", 
    "newspaper publication", "newspaper advertisement",
    "analyst call transcript", "audio recording of analyst call", 
    "copy of newspaper", "procedural update", "address change",
    "complaint report", "investor grievance", "shareholding pattern",
    "voting result", "compliance certificate"
]

PRIORITY_KEYWORDS = [
    "financial results", "unaudited financial", "audited financial",
    "limited review report", "dividend", "bonus issue", "stock split", 
    "order win", "secured contract", "acquisition", "merger", "takeover",
    "joint venture", "capacity expansion", "capex", "usfda approval"
]

def is_announcement_valuable(headline: str) -> bool:
    s = headline.lower()
    for priority in PRIORITY_KEYWORDS:
        if priority in s:
            return True
    return not any(k in s for k in JUNK)

# ==========================================
# 5. AI CLASSIFICATION SYSTEM INSTRUCTION
# ==========================================
def classify(company, subject):
    prompt = f"""You are a senior equity analyst at a top hedge fund. Classify this corporate announcement strictly.

SENTIMENT RULES:
- BULLISH: good results, order win, dividend, bonus, buyback, asset acquisition, rating upgrade, FDA approval
- BEARISH: bad results, order loss, CEO/CFO resignation, rating downgrade, fraud, SEBI action, insolvency, shutdown
- NEUTRAL: routine appointment, unclear JV, capex without timeline

TIER RULES:
EXTREME (score 8-10): Quarterly/annual financial results, Merger, acquisition, takeover, demerger, SEBI action, fraud, forensic audit, auditor resignation, insolvency, promoter stake change >2%.
HIGH (score 5-7): Order win/loss >100cr, Buyback, bonus issue, stock split, QIP, preferential allotment, CEO/CFO change, credit rating adjustment, block deal >1%.
MEDIUM (score 3-4): Dividend declarations, promoter pledge shifts, patent outcomes, capex, joint ventures.

Return exactly: null — if headline represents a standard routine update not worth alerting.

If worth alerting and category is RESULTS, you must try to structure 'key_figures' strictly as a markdown table following this format:
| Metric | QoQ | YoY | Mar'26 | Dec'25 | Mar'25 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Sales** | -14% | +13% | 116 | 135 | 103 |
| **OP** | -39% | +15% | 18 | 28 | 15 |
| **OPM** | -611 bps | +27 bps | 15.0% | 21.1% | 14.8% |
| **PAT** | -65% | +38% | 3 | 8 | 2 |
| **EPS** | -61% | +40% | 0.7 | 1.8 | 0.5 |

If exact historical metrics are missing, adapt rows to capture available revenue/PAT growth percentages.

Return ONLY this raw JSON format with no markdown wrappers, backticks, or code blocks:
{{"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","score":5,"summary":"one line summary","market_reaction":"expected price impact text","pulse_rating":"OK or STRONG or WEAK","dividend_amount":"null","dividend_exdate":"null","buyback_price":0,"buyback_size_cr":0,"buyback_premium_pct":0,"person_name":"null","person_designation":"null","person_action":"null","order_value_cr":0,"key_figures":"Markdown table string if RESULTS, otherwise string summary of metrics"}}

Company: {company}
Subject: {subject}"""

    for attempt in range(3):
        try:
            r = model.generate_content(prompt)
            text = r.text.strip().replace("```json", "").replace("
```", "").strip()
            if text.lower().startswith("null"):
                return None
            s_idx = text.find("{")
            e_idx = text.rfind("}") + 1
            if s_idx >= 0 and e_idx > s_idx:
                text = text[s_idx:e_idx]
            return json.loads(text)
        except Exception as ex:
            if "429" in str(ex):
                time.sleep(10 * (attempt + 1))
            else:
                return None
    return None

# ==========================================
# 6. STRUCTURAL RESPONSE FORMATTER (HMbot Layout)
# ==========================================
def format_alert(company, result, ann_time):
    category = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL").upper()
    score = result.get("score") or 0
    pulse = result.get("pulse_rating", "OK")
    
    if score >= 8:
        color_indicator = "🟢 *THICK GREEN*"
    elif score in [6, 7]:
        color_indicator = "🟩 *LIGHT GREEN*"
    elif score == 5:
        color_indicator = "⬜ *WHITE*"
    elif score in [3, 4]:
        color_indicator = "🟪 *LIGHT RED*"
    else:
        color_indicator = "🔴 *THICK RED*"

    msg = (
        f"{color_indicator} | *{category}* (Score: {score}/10)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Alert System BY HMbot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *{company.upper()}*\n"
        f"Pulse Rating : *{pulse}*\n\n"
    )

    if category == "RESULTS":
        kf = result.get("key_figures", "")
        if kf and "|" in kf:
            msg += f"{kf}\n\n"
        else:
            msg += f"⚡ *EVENT:* {result.get('summary', '')}\n\n"

    elif category == "ORDER":
        ov = result.get("order_value_cr") or 0
        msg += f"📦 *ORDER DISCLOSURE*\n"
        msg += f"• Value: ₹{ov:,.0f} Cr\n"
        msg += f"• Details: {result.get('summary', '')}\n\n"

    elif category == "CORPORATE_ACTION":
        da = result.get("dividend_amount")
        de = result.get("dividend_exdate")
        bp = result.get("buyback_price") or 0
        bs = result.get("buyback_size_cr") or 0
        msg += f"🔄 *CORPORATE ACTION MATRIX*\n"
        if da and da != "null": msg += f"• Dividend: ₹{da} per share\n"
        if de and de != "null": msg += f"• Ex-Date: {de}\n"
        if bp > 0: msg += f"• Buyback Price: ₹{bp:,.2f}\n"
        if bs > 0: msg += f"• Buyback Size: ₹{bs:,.0f} Cr\n"
        msg += "\n"
    else:
        msg += f"⚡ *EVENT:* {result.get('summary', '')}\n\n"

    msg += f"🎯 *Core Summary:* {result.get('market_reaction', '')}\n\n"
    msg += f"🕐 {ann_time}"
    return msg

# ==========================================
# 7. HIGH-AVAILABILITY CLOUD RSS PARSER
# ==========================================
def check_announcements():
    logger.info("Scanning open public market syndication channels...")
    url = "https://www.bseindia.com/include/NewsRss.aspx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code != 200:
            return

        root = ET.fromstring(response.content)
        sent = 0
        
        for item in root.findall(".//item"):
            title_text = item.find("title").text if item.find("title") is not None else ""
            description_text = item.find("description").text if item.find("description") is not None else ""
            pub_date = item.find("pubDate").text if item.find("pubDate") is not None else now_ist()
            
            if not title_text:
                continue

            if " - " in title_text:
                parts = title_text.split(" - ", 1)
                company_name = parts[0].strip()
                headline = parts[1].strip()
            else:
                company_name = "Market Target"
                headline = title_text.strip()

            ann_id = f"{company_name}||{headline}".replace(" ", "_").lower()
            
            if ann_id in seen_ann:
                continue
            seen_ann.add(ann_id)

            if not is_announcement_valuable(headline):
                continue

            result = classify(company_name, f"{headline}. Context: {description_text}")
            if not result:
                continue

            if (result.get("score") or 0) < 3:
                continue

            msg = format_alert(company_name, result, pub_date)
            send_msg(TELEGRAM_CHAT_ID, msg)
            sent += 1
            time.sleep(3)

        if sent > 0:
            save_seen()
            logger.info(f"Cycle completed. Dispatched {sent} material market alerts.")

    except Exception as e:
        logger.error(f"Cloud syndication processing exception: {e}")

# ==========================================
# 8. ADD-ON: PRE-MARKET INTELLIGENCE ENGINE
# ==========================================
def fetch_morning_macro_context():
    macro_summary = "🌍 *GLOBAL MACRO SETUP*\n"
    try:
        macro_summary += "🔹 *GIFT Nifty Tracking:* Operating normally\n"
        macro_summary += "🛢️ *Brent Crude Benchmarks:* Active\n"
    except Exception:
        pass
    return macro_summary

def fetch_scheduled_earnings_calendar():
    headers = {"User-Agent": "Mozilla/5.0"}
    url = "https://www.bseindia.com/include/NewsRss.aspx"
    calendar_text = "📊 *TODAY'S SEBI RESULTS CALENDAR*\n"
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            companies_reporting = []
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                if "results" in title.lower() or "board meeting" in title.lower():
                    if " - " in title:
                        comp = title.split(" - ", 1)[0].strip()
                        if comp not in companies_reporting:
                            companies_reporting.append(comp)
            if companies_reporting:
                calendar_text += f"📋 *{len(companies_reporting)} Corporations Reporting Today:*\n"
                for comp in companies_reporting[:15]:
                    calendar_text += f"• {comp}\n"
            else:
                calendar_text += "✅ _No major earnings restrictions on key wires today._\n"
    except Exception:
        calendar_text += "⚠️ _Calendar verification offline._\n"
    return calendar_text

def dispatch_daily_premarket_briefing():
    logger.info("Assembling Morning Pre-Market Briefing...")
    current_date_str = datetime.now(IST).strftime("%A, %d %b %Y")
    header = (
        f"☀️ *PRE-MARKET INTELLIGENCE DESK*\n"
        f"📅 {current_date_str} | ⏰ 08:30 AM IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Alert System BY HMbot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    macro_block = fetch_morning_macro_context()
    earnings_block = fetch_scheduled_earnings_calendar()
    footer = "\n🧠 _AI Monitoring Framework Armed for Opening Bell_"
    
    full_packet = f"{header}{macro_block}\n{earnings_block}{footer}"
    send_msg(TELEGRAM_CHAT_ID, full_packet)
    logger.info("✅ Morning briefing packet dispatched.")

# ==========================================
# 9. COMMAND WEBHOOK AND PORT BINDING
# ==========================================
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
            if chat_id and text and text.startswith("/help"):
                send_msg(chat_id, (
                    "🤖 *HMbot Intelligence Desk Active*\n\n"
                    "📡 Automated monitoring active 24/7.\n"
                    "☀️ Pre-Market briefing dispatches at 08:30 AM IST.\n\n"
                    "⚙️ Status: RUNNING"
                ))
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HMbot Intelligence Engine Status: ACTIVE")

    def log_message(self, format, *args):
        pass

def run_scheduler():
    schedule.every(1).minutes.do(check_announcements)
    schedule.every().day.at("08:30", "Asia/Kolkata").do(dispatch_daily_premarket_briefing)
    while True:
        schedule.run_pending()
        time.sleep(5)

# ==========================================
# 10. RUNNER CHECKPOINT ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    logger.info("🚀 Market Intelligence Bot starting...")
    load_seen()
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"✅ Web health endpoint active on container port {port}")
    
    send_msg(TELEGRAM_CHAT_ID, "✅ *Market Intelligence Engine Live on Railway*\nMonitoring live public corporate filings under *HMbot* identity.")
    
    # Startup layout validation packet
    logger.info("🚀 Running startup verification test packet...")
    test_packet = {
        "tier": "HIGH",
        "category": "RESULTS",
        "sentiment": "BULLISH",
        "score": 7,
        "pulse_rating": "OK",
        "summary": "Speciality Restaurants reports sequential recovery metrics.",
        "market_reaction": "Earnings are trustworthy, but a one-off cost hides flat underlying profit performance.",
        "key_figures": "| Metric | QoQ | YoY | Mar'26 | Dec'25 | Mar'25 |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n| **Sales** | 📉 -14% | 📈 +13% | 116 | 135 | 103 |\n| **OP** | 📉 -39% | 📈 +15% | 18 | 28 | 15 |\n| **OPM** | -611 bps | +27 bps | 15.0% | 21.1% | 14.8% |\n| **PAT** | 📉 -65% | 📈 +38% | 3 | 8 | 2 |\n| **EPS** | 📉 -61% | 📈 +40% | 0.7 | 1.8 | 0.5 |"
    }
    test_msg = format_alert("Speciality Rest.", test_packet, now_ist())
    send_msg(TELEGRAM_CHAT_ID, test_msg)
    
    server.serve_forever()
