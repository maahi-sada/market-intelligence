import os
import io
import time
import json
import logging
import zipfile
import threading
import requests
import pypdf
import pytz
from datetime import datetime, timezone
from decimal import Decimal
import google.generativeai as genai
from typing import Dict, Any, Optional, List

# --- LOGGING & TIME SETTING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
IST = pytz.timezone("Asia/Kolkata")

# --- CREDENTIAL KEYS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "787902453")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KGEW36r2cX1-qepXdK8tqyrZoJqnJb2dmbPdX61HFi-Q")

genai.configure(api_key=GEMINI_API_KEY)

# CRITICAL FIX 1: Upgrade to the active production model to fix 404 API Version issues
TARGET_AI_MODEL = "gemini-2.5-flash"
ai_model = genai.GenerativeModel(TARGET_AI_MODEL)

SEEN_ANN_CACHE = set()
CACHE_FILE = "seen_announcements.json"

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
                SEEN_ANN_CACHE = set(json.load(f))
                logging.info(f"[CACHE] Loaded {len(SEEN_ANN_CACHE)} items.")
        except Exception as e:
            logging.error(f"[CACHE] Load error: {e}")

def save_cache():
    try:
        export_list = list(SEEN_ANN_CACHE)[-5000:]
        with open(CACHE_FILE, "w") as f:
            json.dump(export_list, f)
    except Exception as e:
        logging.error(f"[CACHE] Save error: {e}")

# --- SELF-HEALING NETWORK LAYER ---
class InstitutionalSession:
    def __init__(self):
        self.session = requests.Session()
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        self.ua_index = 0
        self.rotate_identity()

    def rotate_identity(self):
        """Rotates browser signatures to navigate past host firewalls."""
        ua = self.user_agents[self.ua_index % len(self.user_agents)]
        self.ua_index += 1
        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com"
        })
        logging.info(f"[IDENTITY] Session signature rotated to minimize firewall footprint.")

    def fetch_json(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        try:
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "").lower()
                if "application/json" in content_type or "text/plain" in content_type:
                    return response.json()
                else:
                    logging.warning(f"[FIREWALL] Received non-JSON block from {url}. Initiating signature rotation.")
                    self.rotate_identity()
                    return None
            elif response.status_code in [403, 401]:
                self.rotate_identity()
        except Exception as e:
            logging.error(f"[NETWORK ERROR] Connection failure: {e}")
        return None

    def fetch_file_content(self, url: str) -> str:
        """CRITICAL FIX 2: Dynamic reader that safely unzips and parses documents."""
        try:
            resp = self.session.get(url, timeout=25)
            if resp.status_code != 200:
                return ""
            
            content_bytes = resp.content
            
            # Defensive check: Handle zip files cleanly
            if url.lower().endswith(".zip") or content_bytes.startswith(b'PK\x03\x04'):
                logging.info(f"[ZIP DECOMPRESSOR] Decompressing archive attachment: {url}")
                extracted_text = []
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                    for filename in z.namelist():
                        if filename.lower().endswith(".pdf"):
                            with z.open(filename) as pdf_file:
                                pdf_data = pdf_file.read()
                                text = self._extract_pdf_text(io.BytesIO(pdf_data))
                                if text: extracted_text.append(text)
                return "\n".join(extracted_text)[:12000]
            
            # Normal raw PDF flow
            return self._extract_pdf_text(io.BytesIO(content_bytes))[:12000]
            
        except Exception as e:
            logging.error(f"[FILE MODULE ERROR] Failed parsing link payload: {e}")
            return ""

    def _extract_pdf_text(self, stream) -> str:
        try:
            reader = pypdf.PdfReader(stream)
            pages_text = []
            max_pages = min(len(reader.pages), 10)
            for i in range(max_pages):
                txt = reader.pages[i].extract_text()
                if txt: pages_text.append(txt)
            return "\n".join(pages_text)
        except Exception as e:
            logging.warning(f"[PDF PARSER CRASH] Stream read failure bypassed cleanly: {e}")
            return ""

# --- ALERTS & DERIVATIVES DATA ENGINE ---
def get_derivatives_positioning(session: InstitutionalSession, symbol: str) -> Dict[str, Any]:
    url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
    data = session.fetch_json(url)
    out = {"oi_change_pct": 0.0, "positioning": "N/A"}
    if data and "data" in data:
        for entry in data["data"]:
            if entry.get("symbol") == symbol:
                oi_chg = float(entry.get("perOIChange", 0))
                p_chg = float(entry.get("pchange", 0))
                out["oi_change_pct"] = oi_chg
                if oi_chg > 5 and p_chg > 0.5: out["positioning"] = "📈 LONG BUILDUP"
                elif oi_chg > 5 and p_chg < -0.5: out["positioning"] = "📉 SHORT BUILDUP"
                elif oi_chg < -5 and p_chg > 0.5: out["positioning"] = "🔥 SHORT COVERING"
                elif oi_chg < -5 and p_chg < -0.5: out["positioning"] = "💨 LONG UNWINDING"
                break
    return out

def analyze_announcement_payload(company: str, subject: str, body_text: str) -> Optional[Dict[str, Any]]:
    prompt = f"""
    Act as an Event-Driven Hedge Fund Analyst.
    Analyze this corporate disclosure from an Indian listed entity.

    FILTERS:
    1. ORDERS: Value >10% of market capitalization.
    2. RESULTS: Revenue/EBITDA/PAT beat/miss >10% or OPM margin change >200 bps.
    3. STRATEGIC ACTIONS: Buybacks (>3% float), major M&A, demergers, promoter stakes changes (>1%).
    4. REGULATORY: USFDA decisions, mining leases, environmental clearances.

    Company Name: {company}
    Subject: {subject}
    Filing Document Text:
    {body_text if body_text.strip() else 'No separate document text. Evaluate headline subject only.'}

    Output exact JSON or return 'null'. Do not format with markdown blocks.
    Format:
    {{
        "is_material": true,
        "tier": "TIER_1",
        "impact_score": 8.5,
        "conviction": "High",
        "direction": "Bullish",
        "summary": "Core statement summary.",
        "why_it_matters": "Institutional justification analysis.",
        "future_earnings_impact": "High",
        "pe_rerating_probability": 80.0,
        "pe_derating_probability": 0.0,
        "time_horizon": "Months",
        "risks": "Execution bottlenecks.",
        "confidence_pct": 95.0,
        "sector_transmission": {{"direct_beneficiaries": ["Sectors"], "indirect_beneficiaries": ["Sectors"], "losers": ["Sectors"]}}
    }}
    """
    try:
        response = ai_model.generate_content(prompt)
        text_clean = response.text.strip().replace("```json", "").replace("```", "").strip()
        if text_clean.lower() == "null" or not text_clean:
            return None
        return json.loads(text_clean)
    except Exception as e:
        logging.error(f"[AI PIPELINE SYSTEM ERROR] Bypassed prompt drop logic: {e}")
        return None

def transmit_alert_to_terminal(company: str, symbol: str, exchange: str, analysis: Dict[str, Any], oi_data: Dict[str, Any]):
    sentiment_emoji = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(analysis['direction'].upper(), "➡️")
    tier_icon = "🚨" if "1" in analysis['tier'] else "⚠️"
    
    message = (
        f"{tier_icon} *{analysis['tier'].upper()} ALERT* | {sentiment_emoji} *{analysis['direction'].upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *STOCK:* {company} ({symbol}) | *EXCHANGE:* {exchange}\n"
        f"📊 *IMPACT SCORE:* `{analysis['impact_score']}/10` | *CONVICTION:* `{analysis['conviction'].upper()}`\n\n"
        f"📝 *EVENT:* {analysis['summary']}\n\n"
        f"💡 *WHY IT MATTERS:* {analysis['why_it_matters']}\n\n"
        f"🚀 *FUTURE EARNINGS IMPACT:* `{analysis['future_earnings_impact'].upper()}`\n"
        f"📈 *PE RERATING PROBABILITY:* `{analysis['pe_rerating_probability']}%`\n"
        f"⏱️ *TIME HORIZON:* `{analysis['time_horizon']}`\n\n"
        f"📦 *F&O POSITIONING:* `{oi_data['positioning']}`\n"
        f"⚠️ *KEY RISKS:* _{analysis['risks']}_\n\n"
        f"🌐 *SECTOR TRANSMISSION:*\n"
        f"🔹 *Direct:* {', '.join(analysis['sector_transmission']['direct_beneficiaries'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 _Timestamp: {datetime.now(IST).strftime('%H:%M:%S')} IST_"
    )
    
    try:
        endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(endpoint, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=12)
    except Exception as e:
        logging.error(f"[DISPATCHER CHANNELS PROXY] Telegram alert drop: {e}")

# --- SYNC LOOPS ---
def process_nse_feed(session: InstitutionalSession):
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    payload = session.fetch_json(url)
    if not payload or not isinstance(payload, list): return

    for item in payload[:25]:
        desc = item.get("desc", "").strip()
        symbol = item.get("symbol", "")
        company = item.get("sm_name", symbol)
        tracking_key = f"NSE_{symbol}_{item.get('an_dt')}_{desc[:20]}"
        
        if tracking_key in SEEN_ANN_CACHE: continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        if any(keyword in desc.lower() for keyword in JUNK_KEYWORDS): continue
            
        pdf_path = item.get("attchmntFile", "")
        pdf_text = ""
        if pdf_path:
            pdf_url = f"https://www.nseindia.com{pdf_path}" if pdf_path.startswith("/") else pdf_path
            pdf_text = session.fetch_file_content(pdf_url)
            
        analysis = analyze_announcement_payload(company, desc, pdf_text)
        if analysis and analysis.get("is_material"):
            oi_metrics = get_derivatives_positioning(session, symbol)
            transmit_alert_to_terminal(company, symbol, "NSE", analysis, oi_metrics)
            time.sleep(1)

def process_bse_feed(session: InstitutionalSession):
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryNew/w"
    params = {"mode": "2", "category": "Company Update", "scripcode": "", "searchtype": "P"}
    payload = session.fetch_json(url, params=params)
    if not payload or not isinstance(payload, list): return

    for item in payload[:25]:
        scrip_code = item.get("SCRIP_CD", "")
        company = item.get("SLONGNAME", scrip_code)
        headline = item.get("HEADLINE", "").strip()
        details = item.get("NEWSSUBJECT", "").strip()
        tracking_key = f"BSE_{scrip_code}_{item.get('NEWS_DT')}_{headline[:20]}"
        
        if tracking_key in SEEN_ANN_CACHE: continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        if any(keyword in headline.lower() or keyword in details.lower() for keyword in JUNK_KEYWORDS): continue
            
        pdf_link = item.get("ATTACHMENTNAME", "")
        pdf_text = ""
        if pdf_link:
            pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}"
            pdf_text = session.fetch_file_content(pdf_url)
            
        analysis = analyze_announcement_payload(company, headline, pdf_text if pdf_text else details)
        if analysis and analysis.get("is_material"):
            transmit_alert_to_terminal(company, scrip_code, "BSE", analysis, {"oi_change_pct": 0.0, "positioning": "N/A"})
            time.sleep(1)

def global_monitoring_loop():
    session = InstitutionalSession()
    load_cache()
    logging.info("[SYSTEM ENGINE INITIALIZED] Running clean multi-exchange alert infrastructure.")
    while True:
        try:
            process_nse_feed(session)
            process_bse_feed(session)
            save_cache()
        except Exception as e:
            logging.critical(f"[CRITICAL CORE EXCEPTION] Main thread recovered error: {e}")
        time.sleep(45)

if __name__ == "__main__":
    global_monitoring_loop()
