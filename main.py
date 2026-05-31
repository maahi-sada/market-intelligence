import os
import time
import logging
import requests
from datetime import datetime

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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Matches your Railway setup
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not GEMINI_API_KEY:
    logger.critical("❌ BOOT ERROR: GEMINI_API_KEY is completely missing in Railway variables!")
    raise ValueError("Set GEMINI_API_KEY in your Railway dashboard.")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("⚠️ TELEGRAM WARNING: Missing connection tokens. Alerts will only print to console logs.")

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
        if response.status_code == 200:
            logger.info("Alert cleanly dispatched to Telegram channel.")
        else:
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
            logger.info(f"Ignored Low-Value Noise: Found keyword '{keyword}' in feed data.")
            return False
    return True

# ==========================================
# 4. STATION 2 — LIGHTWEIGHT PERSISTENT MEMORY
# ==========================================
def load_processed_alerts() -> set:
    """Reads processed alert signatures from disk so memory persists during redeploys."""
    if os.path.exists(SEEN_ALERTS_FILE):
        with open(SEEN_ALERTS_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_processed_alert(alert_id: str):
    """Saves signature to disk to ensure an alert is never sent to your phone twice."""
    with open(SEEN_ALERTS_FILE, "a") as f:
        f.write(f"{alert_id}\n")

# ==========================================
# 5. STRATEGIC RESEARCH SYSTEM INSTRUCTION
# ==========================================
SYSTEM_INSTRUCTION = """
Act as a Bloomberg Terminal, institutional equity research desk, and an event-driven hedge fund analyst.
Analyze corporate announcements and extract ONLY high-conviction, market-moving alerts.

Enforce strict thresholds for materiality:
- Order value >10% of market cap
- Financial surprises/beats/misses >10% or OPM changes >200 basis points
- Guidance revisions >10%
- Structural Corporate Actions (Buybacks >3%, Mergers, Demergers, M&A)
- Significant ownership shifts (>1% promoter stake change, institutional accumulation)
- Strategic Capex (>10% of market cap) or major high-impact regulatory approvals (USFDA, mining, telecom spectrum).

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
    """Communicates directly with Gemini via raw HTTP requests to prevent SDK token conflicts."""
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
                logger.warning(f"Rate limit hit ({response.status_code}). Backing off for {sleep_duration}s...")
                time.sleep(sleep_duration)
                continue
                
            if response.status_code != 200:
                logger.error(f"Google Server Error ({response.status_code}): {response.text}")
                response.raise_for_status()

            result_json = response.json()
            ai_text = result_json['candidates'][0]['content']['parts'][0]['text']
            
            time.sleep(3)  # Pacing delay to remain compliant with safety bands
            return ai_text

        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"System execution failed after maximum retries: {e}")
                raise e
            time.sleep(base_backoff_seconds)

    raise RuntimeError("Unable to communicate with Gemini API backend.")

# ==========================================
# 7. MAIN DISPATCH PIPELINE
# ==========================================
def process_incoming_announcement(announcement: dict):
    """Validates, processes, caches, and routes inbound market updates."""
    company = announcement.get("company", "").strip()
    headline = announcement.get("headline", "").strip()
    raw_text = announcement.get("text", "").strip()

    if not company or not headline:
        return

    # Step 1: Create strict cryptographic unique ID to block duplicate signals
    alert_signature = f"{company}_{headline}".replace(" ", "_").lower()
    processed_cache = load_processed_alerts()

    if alert_signature in processed_cache:
        return

    # Step 2: Drop low-value admin noise before calling the API
    if not is_announcement_valuable(headline, raw_text):
        save_processed_alert(alert_signature)  # Cache it so we skip scanning it again
        return

    # Step 3: Run institutional deep analysis
    logger.info(f"🔥 Core Signal Picked Up! Processing high-value event for {company}...")
    payload = f"Company: {company}\nHeadline: {headline}\nFull Text Summary:\n{raw_text}"
    
    try:
        analysis_result = execute_ai_analysis(payload)
        
        # Step 4: Dispatch findings everywhere
        print(analysis_result)
        send_to_telegram(analysis_result)
        
        # Step 5: Log to persistent memory
        save_processed_alert(alert_signature)
        
    except Exception as e:
        logger.error(f"Failed to process high-value asset update for {company}: {e}")

# ==========================================
# 8. AUTOMATED RECURRENT DATA SCRAPER
# ==========================================
def fetch_live_market_stream():
    """
    Polls public market feeds to scrape live corporate corporate updates.
    Runs continuously without crashing or leaking memory.
    """
    logger.info("Polling live market corporate announcement boards...")
    
    # We target a reliable public API aggregator feed to scrape broad filings
    url = "https://api.bseindia.com/BseNewsPageAPI/api/NewsData/w_NewsDataSelect"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/"
    }
    
    try:
        # The BSE endpoint expects clean timestamp configurations or default initializers
        params = {"pType": "G", "pCategory": "A"}
        response = requests.get(url, headers=headers, params=params, timeout=15)
        
        if response.status_code != 200:
            logger.warning(f"Market board data stream temporarily unreachable (Status: {response.status_code})")
            return

        items = response.json()
        if not items or not isinstance(items, list):
            return

        # Scan through the live market developments
        for item in items:
            company_name = item.get("NEWSSUB", "").strip()
            headline = item.get("HEADLINE", "").strip()
            more_details = item.get("MORE", "").strip()
            
            # Combine disclosures into a standardized parsing dictionary
            announcement_packet = {
                "company": company_name,
                "headline": headline,
                "text": f"{headline}. Summary details: {more_details}"
            }
            
            # Drop into our active multi-stage analyzer
            process_incoming_announcement(announcement_packet)

    except Exception as e:
        logger.error(f"Ingestion channel error: {e}")

# ==========================================
# 9. RUNNER CHECKPOINT ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    logger.info("==================================================")
    logger.info("PRODUCTION SYSTEM ONLINE: LIVE SCANNERS ACTIVE")
    logger.info("==================================================")
    
    # Infinite background market execution architecture
    while True:
        fetch_live_market_stream()
        
        # Poll the exchange boards every 45 seconds for hot updates
        # This safe pace prevents IP blocking and handles high-frequency days cleanly
        time.sleep(45)
