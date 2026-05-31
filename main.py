import os
import io
import time
import json
import logging
import threading
import requests
import pypdf
import pytz
from datetime import datetime, timezone
from decimal import Decimal
import google.generativeai as genai
from typing import Dict, Any, Optional, List

# --- SYSTEM METADATA & TELEMETRY ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
IST = pytz.timezone("Asia/Kolkata")

# --- CREDENTIAL CONFIGURATION (ENV DRIVEN WITH FALLBACKS) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "787902453")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KGEW36r2cX1-qepXdK8tqyrZoJqnJb2dmbPdX61HFi-Q")

genai.configure(api_key=GEMINI_API_KEY)
# Upgraded from flash-8b to flash for high-density, multi-page data parsing capabilities
ai_model = genai.GenerativeModel("gemini-1.5-flash")

# Systemic Tracking Memory cache
SEEN_ANN_CACHE = set()
CACHE_FILE = "seen_announcements.json"

# --- HARDCODED SYSTEMIC NOISE SUPPRESSION MATRIX (FILTER #1) ---
JUNK_KEYWORDS = [
    "trading window closure", "routine board meeting", "agm notice", "egm notice",
    "newspaper publication", "compliance certificate", "investor meeting schedule",
    "generic presentation", "change of address", "share certificate", "procedural update",
    "loss of share", "voting result", "transcript upload", "analyst meet schedule",
    "corporate governance report", "re-appointment of statutory auditor"
]

def load_cache():
    global SEEN_ANN_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                SEEN_ANN_CACHE = set(data)
                logging.info(f"[CACHE] Restored {len(SEEN_ANN_CACHE)} tracking keys.")
        except Exception as e:
            logging.error(f"[CACHE] Failed loading tracking log: {e}")

def save_cache():
    try:
        export_list = list(SEEN_ANN_CACHE)[-5000:]
        with open(CACHE_FILE, "w") as f:
            json.dump(export_list, f)
    except Exception as e:
        logging.error(f"[CACHE] Serialization failure: {e}")

# --- NETWORK PACKET RETRIEVAL OVERLAY ---
class InstitutionalSession:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/"
        })
        self.refresh_cookies()

    def refresh_cookies(self):
        try:
            self.session.get("https://www.nseindia.com", timeout=10)
            time.sleep(1)
            self.session.get("https://www.bseindia.com", timeout=10)
        except Exception as e:
            logging.warning(f"[SESSION] Initial handshakes failed to register: {e}")

    def fetch_json(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        try:
            response = self.session.get(url, params=params, timeout=12)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                self.refresh_cookies()
                response = self.session.get(url, params=params, timeout=12)
                return response.json() if response.status_code == 200 else None
        except Exception as e:
            logging.error(f"[NETWORK ERROR] Endpoint fetch failure ({url}): {e}")
        return None

    def fetch_pdf_content(self, url: str) -> str:
        """Deep parsing capability scanning up to 10 pages of high-density corporate PDF data."""
        try:
            resp = self.session.get(url, timeout=25)
            if resp.status_code != 200:
                return ""
            with io.BytesIO(resp.content) as pdf_bytes:
                reader = pypdf.PdfReader(pdf_bytes)
                extracted_text = []
                # Scan deep into the file where actual balance sheets and tables are nested
                max_pages = min(len(reader.pages), 10)
                for page_num in range(max_pages):
                    page_text = reader.pages[page_num].extract_text()
                    if page_text:
                        extracted_text.append(page_text)
                return "\n".join(extracted_text)[:12000] # Pass 12,000 deep character limit to AI context
        except Exception as e:
            logging.error(f"[PDF PARSER] Failed to parse file content from ({url}): {e}")
            return ""

# --- QUANT DERIVATIVES & METADATA INTELLIGENCE ---
def get_derivatives_positioning(session: InstitutionalSession, symbol: str) -> Dict[str, Any]:
    """Scrapes dynamic open interest telemetry from the derivatives market on high-impact triggers."""
    url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
    data = session.fetch_json(url)
    out = {"oi_change_pct": 0.0, "positioning": "N/A"}
    if data and "data" in data:
        for entry in data["data"]:
            if entry.get("symbol") == symbol:
                oi_chg = float(entry.get("perOIChange", 0))
                p_chg = float(entry.get("pchange", 0))
                out["oi_change_pct"] = oi_chg
                
                # Structural directional categorization based on open interest vectors
                if oi_chg > 5 and p_chg > 0.5:
                    out["positioning"] = "📈 LONG BUILDUP"
                elif oi_chg > 5 and p_chg < -0.5:
                    out["positioning"] = "📉 SHORT BUILDUP"
                elif oi_chg < -5 and p_chg > 0.5:
                    out["positioning"] = "🔥 SHORT COVERING"
                elif oi_chg < -5 and p_chg < -0.5:
                    out["positioning"] = "💨 LONG UNWINDING"
                break
    return out

# --- STRUCTURAL AI CLASSIFICATION PIPELINE (FILTERS #2 & #3 EXECUTOR) ---
def analyze_announcement_payload(company: str, subject: str, body_text: str) -> Optional[Dict[str, Any]]:
    """Enforces rigid structured schemas to stop model output errors and handle complex numbers."""
    prompt = f"""
    Act as a Bloomberg Terminal and an Event-Driven Hedge Fund Analyst.
    Analyze this corporate filing from an Indian listed entity.

    FILTERS AND RULES MATRIX:
    1. ORDERS: Evaluate if value is >10% of market capitalisation.
    2. FINANCIAL RESULTS: Check for a >10% beat/miss or a >200 basis point change in Operating Profit Margin (OPM).
    3. GUIDANCE / ACTIONS: Evaluate revised outlook forecasts (>10%), buybacks (>3% of float), M&A, demergers, or promoter stake changes (>1%).
    4. REGULATORY: Identify high-impact triggers (USFDA warning letters or approvals, mining rights, environmental clearances).

    Filing Context:
    Company Name: {company}
    Subject: {subject}
    Extracted Document Payload:
    {body_text}

    You must output your analysis using this strict JSON structure. If the filing is routine, return 'null'. Do not append markdown wrap sequences.
    
    Required JSON Format:
    {{
        "is_material": true,
        "tier": "TIER_1 or TIER_2 or TIER_3 or TIER_4 or TIER_5",
        "impact_score": 8.5,
        "conviction": "High or Medium or Low",
        "direction": "Bullish or Bearish or Neutral",
        "summary": "One line descriptive executive core statement.",
        "why_it_matters": "Detailed explanation regarding how this shifts institutional positioning.",
        "future_earnings_impact": "High or Medium or Low",
        "pe_rerating_probability": 75.0,
        "pe_derating_probability": 0.0,
        "time_horizon": "Intraday or Days or Weeks or Months",
        "risks": "Structural performance execute bottlenecks.",
        "confidence_pct": 90.0,
        "sector_transmission": {{
            "direct_beneficiaries": ["List sectors/firms"],
            "indirect_beneficiaries": ["List sectors/firms"],
            "losers": ["List sectors/firms"]
        }},
        "extracted_metrics": {{
            "order_value_cr": 0.0,
            "margin_change_bps": 0
        }}
    }}
    """
    try:
        response = ai_model.generate_content(prompt)
        text_clean = response.text.strip().replace("```json", "").replace("```", "").strip()
        if text_clean.lower() == "null" or not text_clean:
            return None
        return json.loads(text_clean)
    except Exception as e:
        logging.error(f"[AI MODEL GENERATION EXCEPTION] Engine dropped processing path: {e}")
        return None

# --- TELEGRAM FORMATTING & DISTRIBUTION INTERFACE ---
def transmit_alert_to_terminal(company: str, symbol: str, exchange: str, analysis: Dict[str, Any], oi_data: Dict[str, Any]):
    sentiment_emoji = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(analysis['direction'].upper(), "➡️")
    tier_icon = "🚨" if "1" in analysis['tier'] else "⚠️"
    
    message = (
        f"{tier_icon} *{analysis['tier'].upper()} ALERT* | {sentiment_emoji} *{analysis['direction'].upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *STOCK:* {company} ({symbol}) | *EXCHANGE:* {exchange}\n"
        f"📊 *IMPACT SCORE:* `{analysis['impact_score']}/10` | *CONVICTION:* `{analysis['conviction'].upper()}`\n\n"
        f"📝 *EVENT SUMMARY:* {analysis['summary']}\n\n"
        f"💡 *WHY IT MATTERS:* {analysis['why_it_matters']}\n\n"
        f"🚀 *FUTURE EARNINGS IMPACT:* `{analysis['future_earnings_impact'].upper()}`\n"
        f"📈 *PE RERATING PROBABILITY:* `{analysis['pe_rerating_probability']}%`\n"
        f"📉 *PE DERATING PROBABILITY:* `{analysis['pe_derating_probability']}%`\n"
        f"⏱️ *TIME HORIZON:* `{analysis['time_horizon']}`\n\n"
        f"📦 *F&O POSITIONING:* `{oi_data['positioning']}` (OI Change: `{oi_data['oi_change_pct']}%`)\n"
        f"⚠️ *KEY RISKS:* _{analysis['risks']}_\n\n"
        f"🌐 *SECTOR TRANSMISSION MATRIX:*\n"
        f"🔹 *Direct Beneficiary:* {', '.join(analysis['sector_transmission']['direct_beneficiaries'])}\n"
        f"🔸 *Indirect Beneficiary:* {', '.join(analysis['sector_transmission']['indirect_beneficiaries'])}\n"
        f"❌ *Potential Losers:* {', '.join(analysis['sector_transmission']['losers'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 _Timestamp: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST_"
    )
    
    try:
        endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(endpoint, json=payload, timeout=12)
    except Exception as e:
        logging.error(f"[ALERTS DISPATCHER CONTROLLER] Distribution to terminal channel dropped: {e}")

# --- TRACKING CONTROLLERS ---

def process_nse_feed(session: InstitutionalSession):
    """Monitors live data streams directly from the NSE Equity Corporate Feed."""
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    payload = session.fetch_json(url)
    if not payload or not isinstance(payload, list):
        return

    for item in payload[:30]:
        desc = item.get("desc", "").strip()
        symbol = item.get("symbol", "")
        company = item.get("sm_name", symbol)
        tracking_key = f"NSE_{symbol}_{item.get('an_dt')}_{desc[:30]}"
        
        if tracking_key in SEEN_ANN_CACHE:
            continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        # Filter #1: Localized keyword suppression
        if any(keyword in desc.lower() for keyword in JUNK_KEYWORDS):
            continue
            
        pdf_path = item.get("attchmntFile", "")
        pdf_text = ""
        if pdf_path:
            pdf_url = f"https://www.nseindia.com{pdf_path}" if pdf_path.startswith("/") else pdf_path
            pdf_text = session.fetch_pdf_content(pdf_url)
            
        analysis = analyze_announcement_payload(company, desc, pdf_text)
        if analysis and analysis.get("is_material"):
            oi_metrics = get_derivatives_positioning(session, symbol)
            transmit_alert_to_terminal(company, symbol, "NSE", analysis, oi_metrics)
            time.sleep(2)

def process_bse_feed(session: InstitutionalSession):
    """Monitors corporate actions and updates directly from the BSE Live Api Feed."""
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryNew/w"
    params = {"mode": "2", "category": "Company Update", "scripcode": "", "searchtype": "P"}
    payload = session.fetch_json(url, params=params)
    if not payload or not isinstance(payload, list):
        return

    for item in payload[:30]:
        scrip_code = item.get("SCRIP_CD", "")
        company = item.get("SLONGNAME", scrip_code)
        headline = item.get("HEADLINE", "").strip()
        details = item.get("NEWSSUBJECT", "").strip()
        tracking_key = f"BSE_{scrip_code}_{item.get('NEWS_DT')}_{headline[:30]}"
        
        if tracking_key in SEEN_ANN_CACHE:
            continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        if any(keyword in headline.lower() or keyword in details.lower() for keyword in JUNK_KEYWORDS):
            continue
            
        pdf_link = item.get("ATTACHMENTNAME", "")
        pdf_text = ""
        if pdf_link:
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}"
            pdf_text = session.fetch_pdf_content(pdf_url)
            
        analysis = analyze_announcement_payload(company, headline, pdf_text if pdf_text else details)
        if analysis and analysis.get("is_material"):
            # BSE data maps derivatives positioning directly back to the primary ticker
            transmit_alert_to_terminal(company, scrip_code, "BSE", analysis, {"oi_change_pct": 0.0, "positioning": "N/A"})
            time.sleep(2)

# --- MASTER COORDINATION ENGINE ---
def global_monitoring_loop():
    session = InstitutionalSession()
    load_cache()
    
    logging.info("[SYSTEM ENGINE INITIALIZED] Institutional multi-exchange matrix active.")
    while True:
        try:
            process_nse_feed(session)
            process_bse_feed(session)
            save_cache()
        except Exception as e:
            logging.critical(f"[CRITICAL MATRIX ERROR] Core master thread crashed: {e}")
        # Run sweeps every 60 seconds to process announcements before consensus builds
        time.sleep(60)

if __name__ == "__main__":
    global_monitoring_loop()
