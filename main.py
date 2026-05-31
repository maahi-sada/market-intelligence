import os
import time
import logging
import requests

# ==========================================
# 1. LOGGING & INITIALIZATION CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MARKET_ENGINE")

# Load Environment Variables from Railway (UPDATED TO MATCH YOUR DASHBOARD)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Changed from TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not GEMINI_API_KEY:
    logger.critical("❌ BOOT ERROR: GEMINI_API_KEY env variable is completely empty or undetected by Railway!")
    raise ValueError("Please check your Railway Dashboard -> Variables tab.")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("⚠️ TELEGRAM NOTICE: Bot tokens or Chat IDs are empty. Printing to logs only.")

# Local cache file for Railway storage memory
SEEN_ALERTS_FILE = "processed_alerts_cache.txt"

# ==========================================
# 2. TELEGRAM DISPATCHER FUNCTION
# ==========================================
def send_to_telegram(text_message: str):
    """Sends the engaging, structured alert straight to your Telegram Chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text_message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"Telegram Delivery Failed: {response.text}")
    except Exception as e:
        logger.error(f"Failed to connect to Telegram API: {e}")

# ==========================================
# 3. FILTER #1 — PROGRAMMATIC JUNK FILTER
# ==========================================
JUNK_KEYWORDS = [
    "trading window closure", "board meeting schedule", "agm notice", 
    "egm notice", "newspaper publication", "compliance disclosure", 
    "investor meeting schedule", "generic presentation", "address change", 
    "share certificate", "procedural update", "loss of certificate",
    "analyst call audio", "transcript of analyst"
]

def is_announcement_valuable(headline: str, text: str) -> bool:
    combined_content = f"{headline} {text}".lower()
    for keyword in JUNK_KEYWORDS:
        if keyword in combined_content:
            logger.info(f"Dropped Junk Traffic programmatically: [{keyword}] found.")
            return False
    return True

# ==========================================
# 4. STATION 2 — DEDUPLICATION MEMORY ENGINE
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
# 5. NEW ENGAGING SYSTEM PROMPT WITH EMOTICONS
# ==========================================
SYSTEM_INSTRUCTION = """
Act as a Bloomberg Terminal and an institutional event-driven hedge fund analyst.
Analyze corporate announcements and extract ONLY high-conviction, market-moving elements.

You must choose the dynamic presentation layout based on the market signal direction:
- Highly Bullish Events (e.g., major orders, margin expansion, earnings surprise): Use 🚀, 🔥, 📈.
- Heavily Bearish Events (e.g., severe margin compression, regulatory failure, guidance cuts): Use 🩸, 📉, ⚠️.
- Neutral/Structural updates: Use 🧱, 📊.

Format your response EXACTLY like this layout structure using standard markdown bolding:

### [INSERT RELEVANT TOP EMOJIS HERE] **MARKET FLASH | HIGH-CONVICTION SIGNAL** [INSERT RELEVANT TOP EMOJIS HERE]

**📍 STOCK:** [Company Name] ([Ticker] / [Sector Name])
**💰 METRICS:** CMP: [Insert Price or Data] | M-Cap: [Insert Size] | **F&O:** [Yes/No]

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
# 6. STATION 3 — DIRECT HTTP REST EXECUTION ENGINE
# ==========================================
def execute_ai_analysis(announcement_text: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": announcement_text}
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {"text": SYSTEM_INSTRUCTION}
            ]
        },
        "generationConfig": {
            "temperature": 0.1
        }
    }

    max_retries = 5
    base_backoff_seconds = 6

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code in [429, 503]:
                sleep_duration = base_backoff_seconds * (2 ** attempt)
                logger.warning(f"Rate limit hit ({response.status_code}). Sleeping {sleep_duration}s...")
                time.sleep(sleep_duration)
                continue
                
            if response.status_code != 200:
                logger.error(f"Google Server Error ({response.status_code}): {response.text}")
                response.raise_for_status()

            result_json = response.json()
            ai_text = result_json['candidates'][0]['content']['parts'][0]['text']
            
            time.sleep(5)
            return ai_text

        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"System error after max retries: {e}")
                raise e
            time.sleep(base_backoff_seconds)

    raise RuntimeError("Continuous connectivity issues with API backend.")

# ==========================================
# 7. CENTRAL DISPATCH / ALERT PIPELINE
# ==========================================
def process_incoming_announcement(announcement: dict):
    company = announcement.get("company", "").strip()
    headline = announcement.get("headline", "").strip()
    raw_text = announcement.get("text", "").strip()

    alert_signature = f"{company}_{headline}".replace(" ", "_").lower()
    processed_cache = load_processed_alerts()

    if alert_signature in processed_cache:
        logger.info(f"Duplicate Alert Blocked for {company}.")
        return

    if not is_announcement_valuable(headline, raw_text):
        return

    logger.info(f"Processing high-value event for {company}...")
    payload = f"Company: {company}\nHeadline: {headline}\nText:\n{raw_text}"
    
    analysis_result = execute_ai_analysis(payload)

    print(analysis_result)
    send_to_telegram(analysis_result)
    
    save_processed_alert(alert_signature)

# ==========================================
# 8. ENVIRONMENT RUNNER LOOP
# ==========================================
if __name__ == "__main__":
    logger.info("==================================================")
    logger.info("SYSTEM ENGINE READY: REST + Custom Variable Map")
    logger.info("==================================================")
    
    mock_incoming_stream = [
        {
            "company": "Larsen & Toubro LLC",
            "headline": "Secured Mega Order Win from Middle East Value Exceeding 12000 Crores",
            "text": "The infrastructure business segment of L&T has won an ultra-large international contract for building major renewable energy grids valued at over 12,500 Crores."
        }
    ]

    for data_packet in mock_incoming_stream:
        process_incoming_announcement(data_packet)

    while True:
        time.sleep(3600)
