import os
import io
import time
import json
import logging
import zipfile
import requests
import pypdf
import pytz
from datetime import datetime, timezone
import google.generativeai as genai
from typing import Dict, Any, Optional

# --- CORE SETTINGS ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
IST = pytz.timezone("Asia/Kolkata")

# --- PROTECTION: KEYS PULLED FROM ENVIRONMENT ONLY ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logging.critical("[CONFIG ERROR] Missing required Environment Variables in Railway settings panel!")

genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-2.5-flash")

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
                logging.info(f"[CACHE] Loaded {len(SEEN_ANN_CACHE)} tracking anchors.")
        except Exception as e:
            logging.error(f"[CACHE] Error: {e}")

def save_cache():
    try:
        export_list = list(SEEN_ANN_CACHE)[-3000:]
        with open(CACHE_FILE, "w") as f:
            json.dump(export_list, f)
    except Exception as e:
        logging.error(f"[CACHE] Backup fail: {e}")

# --- PROTECTION NETWORK OVERLAY ---
class InstitutionalSession:
    def __init__(self):
        self.session = requests.Session()
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ]
        self.ua_index = 0
        self.rotate_identity()

    def rotate_identity(self):
        ua = self.user_agents[self.ua_index % len(self.user_agents)]
        self.ua_index += 1
        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com"
        })

    def fetch_json(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        try:
            response = self.session.get(url, params=params, timeout=12)
            if response.status_code == 200:
                ct = response.headers.get("Content-Type", "").lower()
                if "application/json" in ct or "text/plain" in ct:
                    return response.json()
                else:
                    self.rotate_identity()
                    return None
            elif response.status_code in [401, 403]:
                self.rotate_identity()
        except Exception:
            pass
        return None

    def fetch_file_content(self, url: str) -> str:
        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200: return ""
            content_bytes = resp.content
            
            if url.lower().endswith(".zip") or content_bytes.startswith(b'PK\x03\x04'):
                extracted_text = []
                with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                    for filename in z.namelist():
                        if filename.lower().endswith(".pdf"):
                            with z.open(filename) as pdf_file:
                                text = self._extract_pdf_text(io.BytesIO(pdf_file.read()))
                                if text: extracted_text.append(text)
                return "\n".join(extracted_text)[:8000]
            
            return self._extract_pdf_text(io.BytesIO(content_bytes))[:8000]
        except Exception:
            return ""

    def _extract_pdf_text(self, stream) -> str:
        try:
            reader = pypdf.PdfReader(stream)
            pages_text = [p.extract_text() for p in reader.pages[:6] if p.extract_text()]
            return "\n".join(pages_text)
        except Exception:
            return ""

# --- QUANT & AI ALGORITHM MODULES ---
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
    # Dynamic rate limiter: Puts a strict 5.5-second pacing gap between calls
    # This maintains ~11 requests per minute, staying well under the free tier limit
    time.sleep(5.5)
    
    prompt = f"""
    Act as an Event-Driven Hedge Fund Analyst. Analyze this Indian corporate disclosure.
    FILTERS:
    1. ORDERS: Value >10% of market capitalization.
    2. RESULTS: Revenue/EBITDA/PAT beat/miss >10% or OPM margin change >200 bps.
    3. STRATEGIC ACTIONS: Buybacks (>3% float), major M&A, demergers, promoter changes (>1%).
    4. REGULATORY: USFDA decisions, environmental clearances.

    Company Name: {company} Subject: {subject}
    Filing Text: {body_text[:6000] if body_text.strip() else 'Use Headline Subject only.'}

    Output exact JSON string or return 'null'. Do not format with markdown wrappers.
    Format:
    {{
        "is_material": true,
        "tier": "TIER_1",
        "impact_score": 8.5,
        "conviction": "High",
        "direction": "Bullish",
        "summary": "Core outcome statement.",
        "why_it_matters": "Hedge fund structural investment rationale thesis.",
        "future_earnings_impact": "High",
        "pe_rerating_probability": 80.0,
        "pe_derating_probability": 0.0,
        "time_horizon": "Months",
        "risks": "Execution speed blocks.",
        "confidence_pct": 95.0,
        "sector_transmission": {{"direct_beneficiaries": ["Sectors"], "indirect_beneficiaries": ["Sectors"], "losers": ["Sectors"]}}
    }}
    """
    try:
        response = ai_model.generate_content(prompt)
        text_clean = response.text.strip().replace("```json", "").replace("```", "").strip()
        if text_clean.lower() == "null" or not text_clean: return None
        return json.loads(text_clean)
    except Exception as e:
        logging.error(f"[AI RATE WARNING] Bypassed prompt exception: {e}")
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
        f"📦 *F&O POSITIONING:* `{oi_data['positioning']}`\n"
        f"⚠️ *KEY RISKS:* _{analysis['risks']}_\n\n"
        f"🌐 *SECTOR TRANSMISSION:*\n"
        f"🔹 *Direct:* {', '.join(analysis['sector_transmission']['direct_beneficiaries'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 _Timestamp: {datetime.now(IST).strftime('%H:%M:%S')} IST_"
    )
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=12)
    except Exception as e:
        logging.error(f"[TELEGRAM DROP] Fail: {e}")

# --- TIMELINE PROCESSORS ---
def process_nse_feed(session: InstitutionalSession):
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    payload = session.fetch_json(url)
    if not payload or not isinstance(payload, list): return

    # Process only the 8 most recent updates to optimize API usage
    for item in payload[:8]:
        desc = item.get("desc", "").strip()
        symbol = item.get("symbol", "")
        company = item.get("sm_name", symbol)
        tracking_key = f"NSE_{symbol}_{item.get('an_dt')}_{desc[:15]}"
        
        if tracking_key in SEEN_ANN_CACHE: continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        if any(keyword in desc.lower() for keyword in JUNK_KEYWORDS): continue
        
        pdf_path = item.get("attchmntFile", "")
        pdf_text = session.fetch_file_content(f"https://www.nseindia.com{pdf_path}") if pdf_path else ""
            
        analysis = analyze_announcement_payload(company, desc, pdf_text)
        if analysis and analysis.get("is_material"):
            oi_metrics = get_derivatives_positioning(session, symbol)
            transmit_alert_to_terminal(company, symbol, "NSE", analysis, oi_metrics)

def process_bse_feed(session: InstitutionalSession):
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryNew/w"
    params = {"mode": "2", "category": "Company Update", "scripcode": "", "searchtype": "P"}
    payload = session.fetch_json(url, params=params)
    if not payload or not isinstance(payload, list): return

    for item in payload[:8]:
        scrip_code = item.get("SCRIP_CD", "")
        company = item.get("SLONGNAME", scrip_code)
        headline = item.get("HEADLINE", "").strip()
        details = item.get("NEWSSUBJECT", "").strip()
        tracking_key = f"BSE_{scrip_code}_{item.get('NEWS_DT')}_{headline[:15]}"
        
        if tracking_key in SEEN_ANN_CACHE: continue
        SEEN_ANN_CACHE.add(tracking_key)
        
        if any(keyword in headline.lower() or keyword in details.lower() for keyword in JUNK_KEYWORDS): continue
            
        pdf_link = item.get("ATTACHMENTNAME", "")
        pdf_text = session.fetch_file_content(f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pdf_link}") if pdf_link else ""
            
        analysis = analyze_announcement_payload(company, headline, pdf_text if pdf_text else details)
        if analysis and analysis.get("is_material"):
            transmit_alert_to_terminal(company, scrip_code, "BSE", analysis, {"oi_change_pct": 0.0, "positioning": "N/A"})

def global_monitoring_loop():
    session = InstitutionalSession()
    load_cache()
    logging.info("[SYSTEM ENGINE READY] Free-tier safety matrix successfully deployed.")
    while True:
        try:
            process_nse_feed(session)
            process_bse_feed(session)
            save_cache()
        except Exception as e:
            logging.critical(f"[LOOP ANOMALY RECOVERED] Restructuring context thread: {e}")
        time.sleep(60)

if __name__ == "__main__":
    global_monitoring_loop()
