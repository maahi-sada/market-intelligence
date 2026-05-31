import os
import time
import logging
import requests
import xml.etree.ElementTree as ET

# ==========================================
# 1. LOGGING & SYSTEM CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MARKET_ENGINE")

# Load Environment Variables from Railway
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not GEMINI_API_KEY:
    logger.critical("❌ BOOT ERROR: GEMINI_API_KEY is missing in Railway variables!")
    raise ValueError("Set GEMINI_API_KEY in your Railway dashboard.")

# Local cache file to maintain persistent alert memory on Railway filesystem
SEEN_ALERTS_FILE = "processed_alerts_cache.txt"

# ==========================================
# 2. TELEGRAM DISPATCH ENGINE
# ==========================================
def send_to_telegram(text_message: str):
    """Dispatches clean, formatted institutional alert sheets to your Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text_message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            logger.error(f"Telegram API Refusal: {response.text}")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram servers: {e}")

# ==========================================
# 3. FILTER #1 — HARD ADMINISTRATIVE NOISE REMOVER
# ==========================================
JUNK_KEYWORDS = [
    "trading window", "board meeting schedule", "agm notice", "egm notice",
    "newspaper publication", "compliance disclosure", "investor meeting schedule",
    "generic presentation", "address change", "share certificate", "procedural update",
    "loss of certificate", "analyst call audio", "transcript of analyst", "analyst call recording",
    "loss of share", "closure of trading window", "newspaper advertisement", "disclosure under regulation"
]

def is_announcement_valuable(headline: str, text: str) -> bool:
    """Instantly drops routine compliance updates to protect your API quota."""
    combined_content = f"{headline} {text}".lower()
    for keyword in JUNK_KEYWORDS:
        if keyword in combined_content:
            return False
    return True

# ==========================================
# 4. STATION 2 — LIGHTWEIGHT PERSISTENT MEMORY
# ==========================================
def load_processed_alerts() -> set:
    if os.path.exists(SEEN_ALERTS_FILE):
        with open(SEEN_ALERTS_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_processed_alert(alert_id: str):
    with open(SEEN_ALERTS_FILE, "a") as f:
        f.write(f"{alert_id}\n")

# ==========================================
# 5. STRATEGIC RESEARCH SYSTEM INSTRUCTION
# ==========================================
SYSTEM_INSTRUCTION = """
Act as a Bloomberg Terminal and an event-driven hedge fund analyst.
Analyze corporate announcements and extract ONLY high-conviction, market-moving alerts.

Select the dynamic presentation layout based on the market signal direction:
- Highly Bullish Events: Use 🚀, 🔥, 📈.
- Heavily Bearish Events: Use 🩸, 📉, ⚠️.
- Neutral/Structural Updates: Use 🧱, 📊.

Format your response EXACTLY like this layout structure using standard markdown bolding:

### [INSERT RELEVANT TOP EMOJIS HERE] **MARKET FLASH | HIGH-CONVICTION SIGNAL** [INSERT RELEVANT TOP EMOJIS HERE]

**📍 STOCK:** [Company Name] ([Ticker] / [Sector Name])
**💰 METRICS:** CMP: [Insert Price/Data] | M-Cap: [Insert Size] | **F&O:** [Yes/No]

**⚡ EVENT:** [Headline Summary of the Corporate Event]
**🎯 ALIGNMENT:** [Specify which filter condition threshold it cleared]

---

**[SENTIMENT EMOJI] SENTIMENT:** [HIGHLY BULLISH / HEAVILY BEARISH / NEUTRAL] (Impact Score: **X.X/10**)
**📊 POSITIONING:** [Institutional Accumulation Signal / Distribution / Neutral]

**💡 THE BOTTOM LINE:**
* **Earnings Delta:** [1-2 concise sentences outlining the direct revenue/growth implications]
* **Margin Impact:** [1 sentence summarizing EBITDA/OPM shifts]

📈 **FUTURE EARNINGS:** [Low / Medium / High]
🔄 **PE RERATING POTENTIAL:** [Low / Medium / High] [0-100 Prob: **XX**]
⏳ **HORIZON:** [Intraday / Days / Weeks / Months]
🎯 **CONFIDENCE:** [XX]% 

⚠️ **KEY RISKS:** [Primary downside threat or risk factor]
"""

# ==========================================
# 6. STATION 3 — ROBUST REST API PIPELINE
# ==========================================
def execute_ai_analysis(announcement_text: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "contents": [{"parts": [{"text": announcement_text}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "generationConfig": {"temperature": 0.1}
    }

    max_retries = 5
    base_backoff_seconds = 6

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code in [429, 503]:
                sleep_duration = base_backoff_seconds * (2 ** attempt)
                time.sleep(sleep_duration)
                continue
                
            if response.status_code != 200:
                response.raise_for_status()

            result_json = response.json()
            return result_json['candidates'][0]['content']['parts'][0]['text']
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_backoff_seconds)
    raise RuntimeError("Unable to communicate with Gemini API backend.")

# ==========================================
# 7. MAIN DISPATCH PIPELINE
# ==========================================
def process_incoming_announcement(announcement: dict):
    company = announcement.get("company", "").strip()
    headline = announcement.get("headline", "").strip()
    raw_text = announcement.get("text", "").strip()

    if not company or not headline:
        return

    alert_signature = f"{company}_{headline}".replace(" ", "_").lower()
    processed_cache = load_processed_alerts()

    if alert_signature in processed_cache:
        return

    if not is_announcement_valuable(headline, raw_text):
        save_processed_alert(alert_signature)
        return

    logger.info(f"🔥 Core Signal Picked Up! Analyzing {company}...")
    payload = f"Company: {company}\nHeadline: {headline}\nFull Text Summary:\n{raw_text}"
    
    try:
        analysis_result = execute_ai_analysis(payload)
        print(analysis_result)
        send_to_telegram(analysis_result)
        save_processed_alert(alert_signature)
    except Exception as e:
        logger.error(f"Failed to execute analysis for {company}: {e}")

# ==========================================
# 8. HIGH-AVAILABILITY CLOUD RSS FEED STREAM
# ==========================================
def fetch_live_market_stream():
    """Parses open public RSS syndication channels to stream clean corporate event feeds."""
    logger.info("Scanning open public market syndication feeds...")
    url = "https://www.bseindia.com/include/NewsRss.aspx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code != 200:
            return

        root = ET.fromstring(response.content)
        for item in root.findall(".//item"):
            title_text = item.find("title").text if item.find("title") is not None else ""
            description_text = item.find("description").text if item.find("description") is not None else ""
            
            if not title_text:
                continue

            if " - " in title_text:
                parts = title_text.split(" - ", 1)
                company_name = parts[0].strip()
                headline = parts[1].strip()
            else:
                company_name = "Market Alert Target"
                headline = title_text.strip()

            process_incoming_announcement({
                "company": company_name,
                "headline": headline,
                "text": f"{headline}. Full disclosure context: {description_text}"
            })
    except Exception as e:
        logger.error(f"Cloud-stream tracking exception encountered: {e}")

# ==========================================
# 9. RUNNER CHECKPOINT ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    logger.info("==================================================")
    logger.info("PRODUCTION SYSTEM ONLINE: RSS COMPATIBILITY LAYER")
    logger.info("==================================================")
    
    # 1. RUN ONCE TEST AT BOOT
    logger.info("🚀 Triggering immediate pipeline diagnostic test...")
    test_packet = {
        "company": "Tata Motors Ltd",
        "headline": "Secured Electric Bus Supply Contract Valued at Over 5000 Crores",
        "text": "Tata Motors has won a landmark tender to supply 2,500 advanced electric buses to major metropolitan transport corporations over the next 18 months."
    }
    process_incoming_announcement(test_packet)
    
    # 2. CONTINUOUS CLEAN PIPELINE SCRIPT LOOP
    while True:
        fetch_live_market_stream()
        time.sleep(45)
