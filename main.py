import os
import io
import time
import json
import logging
import threading
import requests
import google.generativeai as genai
import pytz
import schedule
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MARKET_BOT")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")

if not GEMINI_API_KEY:
    logger.critical("GEMINI_API_KEY missing in Railway variables!")
    raise ValueError("Set GEMINI_API_KEY in Railway dashboard.")

IST       = pytz.timezone("Asia/Kolkata")
seen_ann  = set()
SEEN_FILE = "seen_ann.json"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

def now_ist(fmt="%d %b %Y %H:%M IST"):
    return datetime.now(IST).strftime(fmt)

# ==========================================
# PERSISTENT DEDUP
# ==========================================
def load_seen():
    global seen_ann
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f:
                seen_ann = set(json.load(f))
            logger.info(f"Loaded {len(seen_ann)} seen IDs")
    except Exception as e:
        logger.error(f"Load seen error: {e}")

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_ann)[-3000:], f)
    except Exception as e:
        logger.error(f"Save seen error: {e}")

# ==========================================
# TELEGRAM
# ==========================================
def send_msg(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=12
        )
        if r.status_code != 200:
            logger.error(f"Telegram error: {r.text[:80]}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ==========================================
# NSE SESSION
# ==========================================
def get_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
    except:
        pass
    return s

# ==========================================
# PDF FETCH
# ==========================================
def fetch_pdf_text(session, pdf_path):
    try:
        import pypdf
        url  = f"https://www.nseindia.com{pdf_path}" if pdf_path.startswith("/") else pdf_path
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return ""
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        text   = ""
        for page in reader.pages[:4]:
            text += page.extract_text() or ""
        return text[:3000]
    except Exception as e:
        logger.error(f"PDF error: {e}")
        return ""

# ==========================================
# STOCK INFO — CMP, MCAP, F&O
# ==========================================
def get_stock_info(session, symbol):
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            timeout=10
        )
        if resp.status_code == 200:
            d      = resp.json()
            meta   = d.get("metadata", {})
            sec    = d.get("securityInfo", {})
            price  = meta.get("lastPrice", 0)
            shares = sec.get("issuedSize", 0)
            mc     = round((price * shares) / 1e7, 0) if price and shares else 0
            fno    = "✅ Yes" if sec.get("isFNOSec") else "❌ No"
            return {"cmp": price, "mcap": mc, "fno": fno}
    except:
        pass
    return {}

# ==========================================
# ORDER SIZE CONTEXT
# ==========================================
def order_context(order_cr, mc_cr):
    if mc_cr <= 0:
        return ""
    r = (order_cr / mc_cr) * 100
    if r >= 100: return f"🚀 MASSIVE — {r:.0f}% of Mkt Cap!"
    if r >= 50:  return f"🔴 HUGE — {r:.0f}% of Mkt Cap"
    if r >= 20:  return f"🟠 LARGE — {r:.0f}% of Mkt Cap"
    if r >= 10:  return f"🟡 SIGNIFICANT — {r:.0f}% of Mkt Cap"
    return       f"⚪ ROUTINE — {r:.0f}% of Mkt Cap"

# ==========================================
# JUNK PRE-FILTER — zero AI cost
# ==========================================
JUNK = [
    "trading window", "shareholding pattern", "newspaper",
    "loss of share", "duplicate share", "voting result",
    "agm notice", "egm notice", "compliance certificate",
    "transcript", "presentation uploaded", "closure of trading",
    "change in address", "book closure", "investor meet",
    "con. call updates", "website update", "csr activity",
    "change in registrar", "intimation of board meeting",
    "loss of certificate", "change in auditor address",
    "analysts/institutional investor meet",
    "investor presentation", "outcome of board",
    "postal ballot", "corporate governance",
    "insider trading window", "media interview"
]

def is_junk(subject):
    s = subject.lower()
    return any(k in s for k in JUNK)

# ==========================================
# GEMINI CLASSIFICATION — INSTITUTIONAL GRADE
# ==========================================
def classify(company, subject, pdf_text=""):
    body = f"Subject: {subject}\n\nPDF Content:\n{pdf_text}" if pdf_text else f"Subject: {subject}"

    prompt = f"""You are an institutional-grade stock market event filtering engine used by hedge funds and Bloomberg terminals.

COMPANY FILTER — reject and return null if company appears to be:
- Market Cap below 5000 crore
- SME stock, penny stock, shell company, suspended stock
- Illiquid or manipulated stock
- Annual revenue below 500 crore
Prioritize: Nifty 500, F&O stocks, MSCI stocks, large cap, mid cap, institutionally owned companies.

ALERT SCORING — only alert if score >= 70 out of 100:
100 = Exceptional
90+ = Extremely Important
80+ = High Priority
70+ = Worth Alerting
Below 70 = return null

SENTIMENT RULES — pick ONE only:
- BULLISH: order win, good results, dividend/bonus/buyback, asset acquisition, rating upgrade, FDA approval, capacity expansion, promoter buying, strategic deal
- BEARISH: bad results, order cancellation/loss, CEO/CFO resignation unexpectedly, rating downgrade, fraud, SEBI action, insolvency, plant shutdown, debt default, promoter pledge increase
- NEUTRAL: routine appointment, unclear JV terms, capex without timeline or financials

CATEGORY 1 — LARGE ORDERS (score 85-100):
Alert when order value exceeds 10% of annual revenue OR is a major government/defence/telecom/export contract.
Examples: BEL defence order, HFCL telecom contract, export agreement with Fortune 500 customer.

CATEGORY 2 — LICENSING / COMMERCIAL DEALS (score 85-100):
Licensing agreement, technology transfer, royalty deal, exclusive distribution especially in pharma/biotech/tech.

CATEGORY 3 — REGULATORY APPROVALS (score 80-95):
USFDA approval, ANDA approval, CE mark, environmental clearance, mining clearance, commercial production approval.

CATEGORY 4 — MERGER & ACQUISITION (score 80-95):
Acquisition, merger, demerger, strategic stake purchase, business sale, asset acquisition. Evaluate purchase value and strategic significance.

CATEGORY 5 — PROMOTER ACTIVITY (score 75-90):
Promoter buying from open market, promoter stake increase, strategic investor stake increase.
IGNORE: family transfers, gift transfers, inter-se transfers.

CATEGORY 6 — INSTITUTIONAL ACTIVITY (score 70-90):
QIP, block deal, bulk deal — especially if buyer is BlackRock, Vanguard, GQG, Fidelity, Morgan Stanley, Goldman Sachs, Norges Bank, GIC, ADIA, Temasek, CPP, major mutual funds.

CATEGORY 7 — CAPACITY EXPANSION (score 70-85):
New plant, new manufacturing line, capacity doubling, commercial production commencement. Estimate future revenue impact.

CATEGORY 8 — RESULTS (score 70-90):
Alert ONLY if: Revenue growth >20% OR Profit growth >25% OR EBITDA growth >20% OR Margin expansion >150 bps OR Management upgrades guidance.
Guidance upgrades = highest priority.

CATEGORY 9 — MINING & COMMODITIES (score 75-95):
New mine approval, reserve discovery, production guidance upgrade, resource upgrade — NALCO, NMDC, Coal India, MOIL, Vedanta, Hindalco etc.

CATEGORY 10 — NEGATIVE EVENTS (score 80-100):
USFDA warning letter, plant shutdown, mine closure, large order cancellation, major customer loss, credit downgrade, fraud allegation, accounting irregularity, regulatory ban, significant litigation, debt default, promoter pledge increase.

EXTRACTION RULES:
- ORDERS: extract exact value in crores, client name, contract type, duration
- RESULTS: extract revenue, net profit, EBITDA, YoY growth %, guidance if any
- DIVIDENDS: extract exact Rs per share, ex-date, record date
- BUYBACK: extract price, size in crores, premium over CMP %
- MANAGEMENT: extract full name, exact designation, resigned or appointed, reason if stated
- ALL: extract every number, percentage, amount mentioned

Return exactly: null — if score below 70 or not worth alerting.

If score >= 70 return ONLY raw JSON no markdown no explanation:
{{"score":<70-100>,"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or CAPACITY or MINING or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","actionability":"ALERT_IMMEDIATELY or WATCHLIST","institutional_interest":"HIGH or MEDIUM or LOW","summary":"<specific one line with all key numbers>","why_it_matters":"<why this changes earnings/growth/valuation>","expected_impact":"<Low or Medium or High or Extreme>","estimated_earnings_impact":"<quantify if possible or NA>","market_reaction":"<expected stock direction and reason>","dividend_amount":"<Rs X per share or null>","dividend_exdate":"<DD-Mon-YYYY or null>","buyback_price":<Rs or 0>,"buyback_size_cr":<crores or 0>,"buyback_premium_pct":<% or 0>,"person_name":"<full name or null>","person_designation":"<exact title or null>","person_action":"<resigned or appointed or null>","person_reason":"<reason or null>","order_value_cr":<crores or 0>,"order_client":"<client name or null>","order_type":"<defence or govt or export or telecom or other or null>","key_figures":"<all numbers revenues profits percentages>"}}

Company: {company}
{body}"""

    for attempt in range(3):
        try:
            r    = model.generate_content(prompt)
            text = r.text.strip().replace("```json","").replace("```","").strip()
            if text.lower().startswith("null"):
                return None
            s = text.find("{")
            e = text.rfind("}") + 1
            if s >= 0 and e > s:
                text = text[s:e]
            return json.loads(text)
        except Exception as ex:
            err = str(ex)
            if "429" in err:
                wait = 15 * (attempt + 1)
                logger.info(f"Rate limit — waiting {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"Gemini error: {err[:100]}")
                return None
    return None

# ==========================================
# ALERT FORMATTER — INSTITUTIONAL STYLE
# ==========================================
def format_alert(company, symbol, result, ann_time, session):
    score      = result.get("score") or 0
    tier       = result.get("tier", "MEDIUM")
    category   = result.get("category", "OTHER")
    sentiment  = result.get("sentiment", "NEUTRAL")
    action     = result.get("actionability", "WATCHLIST")
    inst       = result.get("institutional_interest", "LOW")
    impact     = result.get("expected_impact", "Medium")

    # Icons
    ie  = {"EXTREME":"🚨","HIGH":"🔴","MEDIUM":"🟡"}.get(tier,"🟡")
    se  = {"BULLISH":"📈","BEARISH":"📉","NEUTRAL":"➡️"}.get(sentiment,"➡️")
    ae  = "⚡ ALERT NOW" if action == "ALERT_IMMEDIATELY" else "👁 WATCHLIST"
    ie2 = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(inst,"🟢")
    ce  = {
        "RESULTS":"📊","ORDER":"📦","PROMOTER":"👤","CORPORATE_ACTION":"🔄",
        "MA":"🤝","FUNDRAISE":"💰","REGULATORY":"⚖️","PHARMA":"💊",
        "MANAGEMENT":"👔","CREDIT":"🏦","CAPACITY":"🏭","MINING":"⛏️","OTHER":"📌"
    }.get(category,"📌")

    # Stock info
    info = get_stock_info(session, symbol) if symbol else {}
    cmp  = info.get("cmp", 0)
    mc   = info.get("mcap", 0)
    fno  = info.get("fno", "?")

    # Header
    msg  = f"{ie} *SCORE: {score}/100* | {se} *{sentiment}* | {ae}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏢 *{company}*\n"
    msg += f"{ce} *{category}* | 💥 *{impact} IMPACT*\n"
    if cmp > 0:
        msg += f"💹 CMP: ₹{cmp:,.2f} | MCap: ₹{mc:,.0f}Cr | F&O: {fno}\n"
    msg += f"🏦 Institutional Interest: {ie2} *{inst}*\n"
    msg += f"\n📝 *{result.get('summary','')}*\n\n"

    # Category-specific details
    if category == "CORPORATE_ACTION":
        da  = result.get("dividend_amount")
        de  = result.get("dividend_exdate")
        bp  = result.get("buyback_price") or 0
        bs  = result.get("buyback_size_cr") or 0
        bpr = result.get("buyback_premium_pct") or 0
        if da and da != "null":  msg += f"💵 *Dividend:* {da} per share\n"
        if de and de != "null":  msg += f"📅 *Ex-date:* {de}\n"
        if bp > 0:               msg += f"💰 *Buyback Price:* ₹{bp:,.2f}\n"
        if bpr > 0:              msg += f"📈 *Premium over CMP:* {bpr:.1f}%\n"
        if bs > 0:               msg += f"📦 *Buyback Size:* ₹{bs:,.0f} Cr\n"
        msg += "\n"

    elif category == "MANAGEMENT":
        pn = result.get("person_name")
        pd = result.get("person_designation")
        pa = result.get("person_action")
        pr = result.get("person_reason")
        if pn and pn != "null": msg += f"👤 *Person:* {pn}\n"
        if pd and pd != "null": msg += f"🎯 *Role:* {pd}\n"
        if pa and pa != "null": msg += f"🔄 *Action:* {pa.upper()}\n"
        if pr and pr not in ["null", None]: msg += f"💬 *Reason:* {pr}\n"
        msg += "\n"

    elif category == "ORDER":
        ov  = result.get("order_value_cr") or 0
        oc  = result.get("order_client")
        ot  = result.get("order_type")
        if ov > 0:
            ctx = order_context(ov, mc)
            msg += f"📦 *Order Value:* ₹{ov:,.0f} Cr\n"
            if oc and oc != "null": msg += f"🤝 *Client:* {oc}\n"
            if ot and ot != "null": msg += f"🏷 *Type:* {ot.upper()}\n"
            if ctx: msg += f"🎯 *vs Mkt Cap:* {ctx}\n"
            msg += "\n"

    # Key figures
    kf = result.get("key_figures","")
    if kf and kf not in ["null","None","N/A","",None]:
        msg += f"🔢 *Key Figures:* {kf}\n\n"

    # Why it matters
    wm = result.get("why_it_matters","")
    if wm and wm not in ["null","None","",None]:
        msg += f"🔍 *Why it matters:* {wm}\n\n"

    # Earnings impact
    ei = result.get("estimated_earnings_impact","")
    if ei and ei not in ["null","None","NA","",None]:
        msg += f"💰 *Earnings impact:* {ei}\n\n"

    msg += f"💡 _{result.get('market_reaction','')}_\n\n"
    msg += f"🕐 {ann_time}"
    return msg

# ==========================================
# COMMANDS
# ==========================================
def cmd_nifty(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ NSE not responding. Try again shortly.")
            return
        data    = resp.json().get("data", [])
        targets = ["NIFTY 50","NIFTY BANK","NIFTY IT","INDIA VIX"]
        msg     = f"📊 *LIVE MARKET*\n🕐 {now_ist()}\n\n"
        for item in data:
            if item.get("index") in targets:
                ltp = item.get("last", 0)
                chg = item.get("change", 0)
                pct = item.get("percentChange", 0)
                e   = "🟢" if chg >= 0 else "🔴"
                msg += f"{e} *{item['index']}*\n"
                msg += f"   ₹{ltp:,.2f}  ({'+' if chg>=0 else ''}{pct:.2f}%)\n\n"
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def cmd_holiday(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ Could not fetch holidays.")
            return
        holidays = resp.json().get("CM", [])
        today    = datetime.now(IST).date()
        month    = today.strftime("%b").upper()
        month_h  = [h for h in holidays if month in h.get("tradingDate","").upper()]
        msg      = f"📅 *MARKET HOLIDAYS — {today.strftime('%B %Y')}*\n\n"
        if month_h:
            for h in month_h:
                msg += f"📌 {h.get('tradingDate','')} — {h.get('description','')}\n"
        else:
            msg += "✅ No more holidays this month."
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def cmd_earnings(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ Could not fetch earnings.")
            return
        events    = resp.json()
        today_fmt = datetime.now(IST).strftime("%Y-%m-%d")
        results   = [
            e for e in events
            if "Financial Results" in e.get("purpose","")
            and today_fmt in e.get("date","")
        ]
        msg = f"📊 *TODAY'S EARNINGS*\n📅 {datetime.now(IST).strftime('%d %b %Y')}\n\n"
        if results:
            msg += f"*{len(results)} companies reporting:*\n\n"
            for r in results[:25]:
                msg += f"📋 *{r.get('symbol','?')}* — {r.get('companyName','')}\n"
        else:
            msg += "No companies scheduled to report today."
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def cmd_ban(chat_id):
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent":"Mozilla/5.0"},
            timeout=15
        )
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ Could not fetch ban list.")
            return
        lines  = resp.text.strip().split("\n")
        stocks = [l.strip() for l in lines[1:] if l.strip()]
        msg    = f"🚫 *F&O BAN LIST*\n📅 {datetime.now(IST).strftime('%d %b %Y')}\n\n"
        if stocks:
            msg += f"*{len(stocks)} stocks in ban period:*\n\n"
            for i, s in enumerate(stocks, 1):
                msg += f"{i}. {s}\n"
            msg += "\n⚠️ _No fresh F&O positions allowed._"
        else:
            msg += "✅ No stocks in ban period today."
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def cmd_oi(chat_id):
    session = get_nse_session()
    try:
        resp   = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            timeout=15
        )
        stocks = resp.json().get("data",[]) if resp.status_code == 200 else []
        sig    = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            send_msg(chat_id, "📈 *OI Report*\nNo significant OI buildups right now.")
            return
        msg = f"📈 *OI BUILDUP ALERT*\n🕐 {now_ist()}\n\n"
        for s in sig[:8]:
            oi  = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol","?")
            e   = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        msg += "\n_High OI = institutional activity_"
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")

def cmd_help(chat_id):
    send_msg(chat_id,
        "🤖 *Market Intelligence Bot*\n\n"
        "📊 /nifty — Live indices\n"
        "📅 /holiday — Market holidays\n"
        "📋 /earnings — Today's results calendar\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI buildup report\n"
        "❓ /help — This menu\n\n"
        "_Auto alerts every 5 min — institutional grade_\n"
        "_Score 70+ only | Nifty 500 focus_\n"
        "_OI report every 30 min_"
    )

# ==========================================
# WEBHOOK SERVER
# ==========================================
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        try:
            data    = json.loads(body)
            message = data.get("message", {})
            text    = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if not chat_id or not text:
                return
            cmd = text.split()[0].split("@")[0].lower()
            logger.info(f"CMD: {cmd}")
            if   cmd == "/nifty":    cmd_nifty(chat_id)
            elif cmd == "/holiday":  cmd_holiday(chat_id)
            elif cmd == "/earnings": cmd_earnings(chat_id)
            elif cmd == "/ban":      cmd_ban(chat_id)
            elif cmd == "/oi":       cmd_oi(chat_id)
            elif cmd in ["/help","/start"]: cmd_help(chat_id)
        except Exception as e:
            logger.error(f"Webhook error: {e}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Market Intelligence Bot - ACTIVE")

    def log_message(self, format, *args):
        pass

# ==========================================
# NSE ANNOUNCEMENT ALERTS
# ==========================================
def check_announcements():
    logger.info(f"[{now_ist('%H:%M IST')}] Checking NSE...")
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning(f"NSE status: {resp.status_code}")
            return
        anns = resp.json()
        logger.info(f"Got {len(anns)} announcements")
    except Exception as e:
        logger.error(f"NSE error: {e}")
        return

    sent = 0
    for ann in anns[:25]:
        ann_id  = ann.get("symbol","") + "||" + ann.get("desc","").strip()
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)

        company  = ann.get("sm_name", ann.get("symbol","Unknown"))
        symbol   = ann.get("symbol","")
        subject  = ann.get("desc","").strip()
        ann_time = ann.get("an_dt", now_ist())
        pdf_path = ann.get("attchmntFile","")

        if not subject:
            continue

        if is_junk(subject):
            logger.info(f"  JUNK | {subject[:50]}")
            continue

        logger.info(f"  CHECK | {company[:25]} | {subject[:50]}")

        # Fetch PDF for all non-junk announcements for better context
        pdf_text = ""
        if pdf_path:
            pdf_text = fetch_pdf_text(session, pdf_path)
            if pdf_text:
                logger.info(f"  PDF: {len(pdf_text)} chars")

        result = classify(company, subject, pdf_text)
        if not result:
            logger.info(f"  SKIP | {company[:30]}")
            continue

        score = result.get("score") or 0
        logger.info(f"  {score}/100 | {result.get('tier')} | {result.get('sentiment')} | {company[:25]}")

        if score < 70:
            logger.info(f"  LOW SCORE ({score}) — skip")
            continue

        msg = format_alert(company, symbol, result, ann_time, session)
        send_msg(TELEGRAM_CHAT_ID, msg)
        logger.info(f"  ✅ SENT: {company} | {score}/100")
        sent += 1
        time.sleep(2)

    save_seen()
    logger.info(f"Done. Sent: {sent}")

# ==========================================
# OI AUTO ALERTS
# ==========================================
def check_oi_auto():
    logger.info(f"[{now_ist('%H:%M IST')}] Checking OI...")
    session = get_nse_session()
    try:
        resp   = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            timeout=15
        )
        stocks = resp.json().get("data",[]) if resp.status_code == 200 else []
        sig    = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 20]
        if not sig:
            logger.info("No significant OI spikes")
            return
        msg = f"📈 *OI BUILDUP ALERT*\n🕐 {now_ist()}\n\n"
        for s in sig[:6]:
            oi  = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol","?")
            e   = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        msg += "\n_Data: NSE live F&O_"
        send_msg(TELEGRAM_CHAT_ID, msg)
        logger.info(f"OI alert sent: {len(sig)} spikes")
    except Exception as e:
        logger.error(f"OI error: {e}")

# ==========================================
# MORNING BRIEFING — 8:30 AM IST daily
# ==========================================
def morning_briefing():
    logger.info("Sending morning briefing...")
    session = get_nse_session()

    earnings_count = 0
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code == 200:
            today_fmt = datetime.now(IST).strftime("%Y-%m-%d")
            earnings_count = len([
                e for e in resp.json()
                if "Financial Results" in e.get("purpose","")
                and today_fmt in e.get("date","")
            ])
    except:
        pass

    ban_count = 0
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent":"Mozilla/5.0"}, timeout=15
        )
        if resp.status_code == 200:
            lines     = resp.text.strip().split("\n")
            ban_count = len([l for l in lines[1:] if l.strip()])
    except:
        pass

    msg = (
        f"☀️ *GOOD MORNING — Market Briefing*\n"
        f"📅 {datetime.now(IST).strftime('%A, %d %b %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Companies reporting results today: *{earnings_count}*\n"
        f"🚫 F&O ban list stocks: *{ban_count}*\n\n"
        f"📡 Alert system active — monitoring NSE every 5 min\n"
        f"🎯 Institutional grade filter — Score 70+ only\n\n"
        f"_Type /earnings for full results list_\n"
        f"_Type /ban for banned stocks list_"
    )
    send_msg(TELEGRAM_CHAT_ID, msg)

# ==========================================
# SCHEDULER
# ==========================================
def run_scheduler():
    schedule.every(5).minutes.do(check_announcements)
    schedule.every(30).minutes.do(check_oi_auto)
    schedule.every().day.at("08:30").do(morning_briefing)
    while True:
        schedule.run_pending()
        time.sleep(10)

# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    logger.info("🚀 Market Intelligence Bot starting...")
    load_seen()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port   = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"✅ Server active on port {port}")
    send_msg(TELEGRAM_CHAT_ID,
        "✅ *Market Intelligence Bot LIVE*\n\n"
        "📡 NSE announcements — every 5 min\n"
        "🎯 Institutional grade — Score 70+/100\n"
        "🏢 Nifty 500 / F&O / Large & Mid Cap focus\n"
        "📈 OI alerts — every 30 min\n"
        "☀️ Morning briefing — 8:30 AM IST\n\n"
        "Type /help for commands"
    )
    threading.Thread(target=check_announcements, daemon=True).start()
    server.serve_forever()
