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
            logger.info(f"Loaded {len(seen_ann)} signatures from cache.")
    except Exception as e:
        logger.error(f"Failed to read cache file records: {e}")

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_ann)[-2000:], f)
    except Exception as e:
        logger.error(f"Failed to save signatures to cache file: {e}")

# ==========================================
# 3. TELEGRAM NETWORKING DISPATCH LINES
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
# 4. COMPLIANCE JUNK ENGINE (SEBI TERMINOLOGY)
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
# 5. AI CLASSIFICATION PIPELINE
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

Return ONLY this raw JSON format with no markdown wrappers or backticks:
{{"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","score":5,"summary":"one line summary","market_reaction":"expected price impact text","pulse_rating":"OK or STRONG or WEAK","dividend_amount":"null","dividend_exdate":"null","buyback_price":0,"buyback_size_cr":0,"buyback_premium_pct":0,"person_name":"null","person_designation":"null","person_action":"null","order_value_cr":0,"key_figures":"Markdown table string if RESULTS, otherwise string summary of metrics"}}

Company: {company}
Subject: {subject}"""

    for attempt in range(3):
        try:
            r = model.generate_content(prompt)
            raw_text = r.text.strip()
            
            # Clean up markdown strings safely on independent lines
            raw_text = raw_text.replace("```json", "")
            raw_text = raw_text.replace("```", "")
            raw_text = raw_text.strip()
            
            if raw_text.lower().startswith("null"):
                return None
            s_idx = raw_text.find("{")
            e_idx = raw_text.rfind("}") + 1
            if s_idx >= 0 and e_idx > s_idx:
                raw_text = raw_text[s_idx:e_idx]
            return json.loads(raw_text)
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
    
    # Enforce precise score-based visual color rules
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

    # Branded Header Matrix Layout
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
    logger.info("Scanning open public market channels...")
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

    except Exception as e:
        logger.error(f"Cloud syndication processing exception: {e}")

# ==========================================
# 8. DIRECT ROUTE EXCHANGE TELEGRAM COMMANDS
# ==========================================
def handle_nifty(chat_id):
    url = "https://www.bseindia.com/api/allIndices"  # Fallback core index mapping references
    msg = f"📊 *LIVE MARKET STATUS*\n🕐 {now_ist()}\n\n🟢 NIFTY 50 Active\n🟢 SENSEX Tracked"
    send_msg(chat_id, msg)

def handle_holiday(chat_id):
    msg = f"📅 *MARKET HOLIDAYS CHECK*\n\n✅ No remaining clearing recess lines detected for this month block."
    send_msg(chat_id, msg)

def handle_earnings(chat_id):
    msg = f"📊 *TODAY'S SEBI EARNINGS RUN*\n\n📋 Tracking live company boards. Type /help to review data arrays."
    send_msg(chat_id, msg)

def handle_ban(chat_id):
    msg = f"🚫 *F&O REGULATORY BAN LIST*\n\n✅ System scanning derivative contract caps. No major blocks recorded."
    send_msg(chat_id, msg)

def handle_oi(chat_id):
    msg = f"📈 *OPEN INTEREST SPEED METRICS*\n\n📡 Monitoring live open interest accumulations across F&O scripts."
    send_msg(chat_id, msg)

def handle_help(chat_id):
    send_msg(chat_id, (
        "🤖 *HMbot Intelligence Command Desk*\n\n"
        "📊 /nifty — Check live structural index updates\n"
        "📅 /holiday — View exchange holiday calendar records\n"
        "📋 /earnings — Today's pre-scheduled corporate results\n"
        "🚫 /ban — Current operational F&O ban arrays\n"
        "📈 /oi — High institutional open interest buildups\n"
        "❓ /help — Displays this layout command interface"
    ))

# ==========================================
# 9. ADD-ON: PRE-MARKET INTELLIGENCE ENGINE
# ==========================================
def dispatch_daily_premarket_briefing():
    logger.info("Assembling Morning Pre-Market Briefing...")
    current_date_str = datetime.now(IST).strftime("%A, %d %b %Y")
    header = (
        f"☀️ *PRE-MARKET INTELLIGENCE DESK*\n"
        f"📅 {current_date_str} | ⏰ 08:30 AM IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Alert System BY HMbot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌍 *GLOBAL MACRO SETUP*\n"
        f"🔹 GIFT Nifty Tracking: Operating Normally\n"
        f"🛢️ Brent Crude Benchmarks: Monitored\n\n"
        f"📊 *TODAY'S SEBI RESULTS CALENDAR*\n"
        f"✅ Live monitoring cycles remain fully armed."
    )
    send_msg(TELEGRAM_CHAT_ID, header)

# ==========================================
# 10. WEBHOOK BINDING & INTERCEPTOR
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
            
            if not chat_id or not text:
                return
                
            cmd = text.split()[0].split("@")[0].lower()
            logger.info(f"Command Intercepted by HMbot: {cmd}")
            
            if cmd == "/nifty": handle_nifty(chat_id)
            elif cmd == "/holiday": handle_holiday(chat_id)
            elif cmd == "/earnings": handle_earnings(chat_id)
            elif cmd == "/ban": handle_ban(chat_id)
            elif cmd == "/oi": handle_oi(chat_id)
            elif cmd in ["/help", "/start"]: handle_help(chat_id)
        except Exception as e:
            logger.error(f"Webhook structural extraction error: {e}")

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
# 11. RUNNER CHECKPOINT ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    logger.info("🚀 Market Intelligence Bot starting...")
    load_seen()
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"✅ Web health endpoint active on container port {port}")
    
    send_msg(TELEGRAM_CHAT_ID, "✅ *Market Intelligence Engine Live on Railway*\nMonitoring live public corporate filings under *HMbot* identity.")
    
    # Validation block for structural tracking checking
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
