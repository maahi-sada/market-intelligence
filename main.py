import os
import time
import logging
from datetime import datetime
from google import genai
from google.genai import types
from google.genai.errors import APIError

# ==========================================
# 1. LOGGING & INITIALIZATION CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MARKET_ENGINE")

# Load API Key from Railway Environment Variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.critical("CRITICAL ERROR: GEMINI_API_KEY environment variable is missing!")
    raise ValueError("Please set GEMINI_API_KEY in your Railway dashboard variables.")

# Initialize the official Google GenAI Client
client = genai.Client(api_key=GEMINI_API_KEY)

# Local files to maintain persistent memory on Railway filesystem
SEEN_ALERTS_FILE = "processed_alerts_cache.txt"

# ==========================================
# 2. FILTER #1 — PROGRAMMATIC JUNK FILTER
# ==========================================
# Instantly drops low-value administrative noise to save API quotas
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
            logger.info(f"Dropped Junk Traffic programmatically: [{keyword}] found in headline.")
            return False
    return True

# ==========================================
# 3. STATION 2 — DEDUPLICATION MEMORY ENGINE
# ==========================================
def load_processed_alerts() -> set:
    """Loads previously processed alert unique signatures from disk."""
    if os.path.exists(SEEN_ALERTS_FILE):
        with open(SEEN_ALERTS_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_processed_alert(alert_id: str):
    """Appends a new unique alert signature to disk memory."""
    with open(SEEN_ALERTS_FILE, "a") as f:
        f.write(f"{alert_id}\n")

# ==========================================
# 4. SYSTEM PROMPT & INSTITUTIONAL LOGIC
# ==========================================
SYSTEM_INSTRUCTION = """
Act as a Bloomberg Terminal, institutional equity research desk, and event-driven hedge fund analyst.
Your primary goal is to analyze corporate announcements and extract ONLY high-conviction, market-moving alerts.

Enforce the following threshold filters:
- Orders: Value >10% of market cap
- Financial Results: Revenue/EBITDA/PAT beat or miss >10%, OPM change >200 bps
- Guidance: Revision >10%
- Corporate Actions: Buybacks >3% of market cap, major M&A, demergers
- Ownership: Promoter changes >1%, significant institutional/insider accumulation
- Capex: Expansion >10% of market cap
- Regulatory: USFDA approvals, major licenses, mining/environmental clearances with clear revenue impacts.

Format your output EXACTLY as follows:
STOCK:
SECTOR:
CMP:
MARKET CAP:
F&O: Yes/No
EVENT:
IMPACT SCORE: 0-10
CONVICTION: Low / Medium / High
DIRECTION: Bullish / Bearish / Neutral
WHY IT MATTERS:
FUTURE EARNINGS IMPACT: Low / Medium / High
PE RERATING IMPACT: Low / Medium / High
TIME HORIZON: Intraday / Days / Weeks / Months
RISKS:
CONFIDENCE: %
"""

# ==========================================
# 5. STATION 3 — PROTECTED AI EXECUTION ENGINE
# ==========================================
def execute_ai_analysis(announcement_text: str) -> str:
    """
    Executes content generation using gemini-2.5-flash with a built-in
    defensive loop against 429 Rate Limits and 503 Service Overloads.
    """
    max_retries = 5
    base_backoff_seconds = 6

    for attempt in range(max_retries):
        try:
            # Official Google GenAI SDK v1.0.0 signature
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=announcement_text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.1,  # Kept low for factual, institutional precision
                )
            )
            
            # Free Tier safety delay pacing: enforce 5-second sleep after successful calls
            time.sleep(5)
            return response.text

        except APIError as e:
            # Catch Rate Limits (429) or Service Overloads (503) safely
            if e.code in [429, 503]:
                sleep_duration = base_backoff_seconds * (2 ** attempt)
                logger.warning(f"Rate limit / API error ({e.code}) encountered. Sleeping for {sleep_duration}s and retrying...")
                time.sleep(sleep_duration)
            else:
                logger.error(f"Unrecoverable Google API Error: {e}")
                raise e
        except Exception as e:
            logger.error(f"Unexpected system execution error: {e}")
            raise e

    raise RuntimeError("System failed to execute prompt after maximum backoff retry cycles due to continuous 429 Rate Limits.")

# ==========================================
# 6. CENTRAL DISPATCH / ALERT PIPELINE
# ==========================================
def process_incoming_announcement(announcement: dict):
    """
    Core pipeline entry point processing individual inbound raw data packets.
    Expects announcement dict keys: 'company', 'headline', 'text'
    """
    company = announcement.get("company", "").strip()
    headline = announcement.get("headline", "").strip()
    raw_text = announcement.get("text", "").strip()

    # Step 1: Compute a deterministic unique signature to block duplicates
    alert_signature = f"{company}_{headline}".replace(" ", "_").lower()
    processed_cache = load_processed_alerts()

    if alert_signature in processed_cache:
        logger.info(f"Duplicate Alert Blocks initiated for {company}: Engine bypassed redundant feed.")
        return

    # Step 2: Clear noise via text parsing rules
    if not is_announcement_valuable(headline, raw_text):
        return

    # Step 3: Run AI analysis inside the protected framework
    logger.info(f"Processing high-value, unique event for {company} via Gemini Engine...")
    payload = f"Company: {company}\nHeadline: {headline}\nFull Announcement Text:\n{raw_text}"
    
    analysis_result = execute_ai_analysis(payload)

    # Step 4: Dispatch output and save state
    print("\n==================================================")
    print(analysis_result)
    print("==================================================\n")
    
    # Commit to memory to prevent duplicates in the future
    save_processed_alert(alert_signature)

# ==========================================
# 7. MAIN EVENT LOOP RUNNER
# ==========================================
if __name__ == "__main__":
    logger.info("==================================================")
    logger.info("SYSTEM ENGINE READY: Institutional Intelligence Active")
    logger.info("==================================================")
    
    # Mock data feed simulation mimicking real-time ingestion loop
    mock_incoming_stream = [
        {
            "company": "Tata Power",
            "headline": "Trading Window Closure Announcement Q2",
            "text": "This is to inform that the trading window for dealing in securities will be closed..."
        },
        {
            "company": "Larsen & Toubro LLC",
            "headline": "Secured Mega Order Win from Middle East Value Exceeding 12000 Crores",
            "text": "The business vertical of L&T has secured a milestone order from an international client for infrastructure building worth 12500 INR Crores."
        },
        {
            "company": "Larsen & Toubro LLC",
            "headline": "Secured Mega Order Win from Middle East Value Exceeding 12000 Crores",
            "text": "Duplicate feed incoming from another data channel..."
        }
    ]

    for data_packet in mock_incoming_stream:
        process_incoming_announcement(data_packet)

    # Keep container alive on Railway
    while True:
        time.sleep(3600)
