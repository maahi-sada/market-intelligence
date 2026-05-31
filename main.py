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

# Configure Gemini Native SDK safely
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
# Pure administrative noise keywords (Safe to drop)
JUNK = [
    "trading window closure", "closure of trading window", 
    "loss of share certificate", "duplicate share certificate", 
    "newspaper publication", "newspaper advertisement",
    "analyst call transcript", "audio recording of analyst call", 
    "copy of newspaper", "procedural update", "address change",
    "complaint report", "investor grievance", "shareholding pattern",
    "voting result", "compliance certificate"
]

# Market-moving indicators (Always force analyze)
PRIORITY_KEYWORDS = [
    "financial results", "unaudited financial", "audited financial",
    "limited review report", "dividend", "bonus issue", "stock split", 
    "order win", "secured contract", "acquisition", "merger", "takeover",
    "joint venture", "capacity expansion", "capex", "usfda approval"
]

def is_announcement_valuable(headline: str) -> bool:
    """Evaluates filings using strict SEBI LODR terminology matrices."""
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
If worth alerting, return ONLY this raw JSON format with no markdown wrappers or backticks:
{{"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","score":5,"summary":"one line summary with metrics","market_reaction":"expected price impact text","dividend_amount":"null","dividend_exdate":"null","buyback_price":0,"buyback_size_cr":0,"buyback_premium_pct":0,"person_name":"null","person_designation":"null","person_action":"null","order_value_cr":0,"key_figures":"N/A"}}

Company: {company}
Subject: {subject}"""

    for attempt in range(3):
        try:
            r = model.generate_content(prompt)
            text = r.text.strip().replace("```json", "").replace("```", "").strip()
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
# 6. STRUCTURAL RESPONSE FORMATTER
# ==========================================
def format_alert(company, result, ann_time):
    category = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL").upper()
    score = result.get("score") or 0
    
    # Enforce your precise score-based indicator visual rules
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

    # Unified category emoji assignment matrix
    ce = {
        "RESULTS": "📊", "ORDER": "📦", "PROMOTER": "👤", "CORPORATE_ACTION": "🔄",
        "MA": "🤝", "FUNDRAISE": "💰", "REGULATORY": "⚖️", "PHARMA": "💊",
        "MANAGEMENT": "👔", "CREDIT": "🏦", "OTHER": "📌"
    }.get(category, "📌")

    # Clean, institutional layout style sheet
    msg = (
        f"{color_indicator} | {sentiment} (Score: {score}/10)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *{company}*\n"
        f"{ce} *{category}*\n\n"
        f"⚡ *EVENT:* {result.get('summary', '')}\n\n"
    )

    if category == "CORPORATE_ACTION":
        da = result.get("dividend_amount")
        de = result.get("dividend_exdate")
        bp = result.get("buyback_price") or 0
        bs = result.get("buyback_size_cr") or 0
        if da and da != "null": msg += f"💵 *Dividend:* {da} per share\n"
        if de and de != "null": msg += f"📅 *Ex-date:* {de}\n"
        if bp > 0: msg += f"💰 *Buyback Price:* ₹{bp:,.2f}\n"
        if bs > 0: msg += f"📦 *Buyback Size:* ₹{bs:,.0f} Cr\n"

    elif category == "MANAGEMENT":
        pn = result.get("person_name")
        pd = result.get("person_designation")
        pa = result.get("person_action")
        if pn and pn != "null": msg += f"👤 *Person:* {pn}\n"
        if pd and pd != "null": msg += f"🎯 *Role:* {pd}\n"
        if pa and pa != "null": msg += f"🔄 *Action:* {pa.upper()}\n"

    elif category == "ORDER":
        ov = result.get("order_value_cr") or 0
        if ov > 0: msg += f"📦 *Order Value:* ₹{ov:,.0f} Cr\n"

    kf = result.get("key_figures", "")
    if kf and kf not in ["null", "None", "N/A", ""]:
        msg += f"🔢 *Key Figures:* {kf}\n"

    msg += f"\n💡 _{result.get('market_reaction', '')}_\n\n"
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

            result = classify(company_name, f"{headline}. Summary context: {description_text}")
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
# 8. COMMAND WEBHOOK AND PORT BINDING
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
                    "🤖 *Market Intelligence Bot Commands*\n\n"
                    "📡 Automated monitoring is active 24/7.\n"
                    "📊 Material events are filtered and ranked instantly.\n\n"
                    "⚙️ Status: RUNNING"
                ))
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Market Intelligence Engine Status: ACTIVE")

    def log_message(self, format, *args):
        pass

def run_scheduler():
    # Polls exchange updates continuously every minute
    schedule.every(1).minutes.do(check_announcements)
    while True:
        schedule.run_pending()
        time.sleep(5)

# ==========================================
# 9. RUNNER CHECKPOINT ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    logger.info("🚀 Market Intelligence Bot starting...")
    load_seen()
    
    # Run the background monitoring engine loop
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    # Bind server to satisfy Railway web health check requirements permanently
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"✅ Web health endpoint active on container port {port}")
    
    send_msg(TELEGRAM_CHAT_ID, "✅ *Market Intelligence Engine Live on Railway*\nMonitoring live public corporate filings.")
    
    # Forced diagnostic system boot check
    logger.info("🚀 Running startup verification test packet...")
    test_packet = {
        "tier": "EXTREME",
        "category": "ORDER",
        "sentiment": "BULLISH",
        "score": 9,
        "summary": "Secured Electric Bus Supply Contract Valued Over INR 5000 Crores",
        "market_reaction": "Significant contract win adding massive revenue visibility over 18 months.",
        "key_figures": "₹5,000 Cr order value"
    }
    test_msg = format_alert("Tata Motors Ltd", test_packet, now_ist())
    send_msg(TELEGRAM_CHAT_ID, test_msg)
    
    server.serve_forever()
