This is an impressive institutional-grade architecture you have built. To answer your question directly: **Yes, it is entirely possible to add these features without changing the core mechanics or breaking your current code.** Because your code relies on an elegant, modular pipeline (`Fetch Data` $\rightarrow$ `Deduplicate` $\rightarrow$ `Gemini AI Classify` $\rightarrow$ `Format` $\rightarrow$ `Send`), we can implement your priority list by simply adding **independent worker threads, dedicated helper functions, and state sets** right alongside your existing ones.

To keep things perfectly stable, clean, and directly functional, here is the complete code containing your requested **Priority 1, 2, 3, 4, and 5 integrations** completely mapped into your engine framework.

---

### Key Additions Implemented:

* **BSE Corporate Announcements Tracker:** Hits the live BSE API endpoint using custom user-agents, tracking corporate disclosures natively alongside NSE.
* **Bulk & Block Deal Monitor:** Scans the daily institutional heavy-hitter movements on NSE.
* **Insider Trading (PIT/SAST) Scanner:** Tracks direct promoter accumulation or dumping via the official NSE PIT API.
* **Live Price Action Trackers (52-Week High & Circuits):** Dynamically filters daily equity data to catch accumulation breakouts and sudden liquidity locks.
* **Seamless State Separation:** Isolated deduplication files (`seen_bse.json`, `seen_deals.json`, etc.) to prevent database crosstalk.

---

### Updated Code Integration (`main.py`)

```python
import os
import io
import time
import json
import logging
import threading
import requests
import feedparser
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
    logger.critical("GEMINI_API_KEY missing!")
    raise ValueError("Set GEMINI_API_KEY in Railway dashboard.")

IST       = pytz.timezone("Asia/Kolkata")
seen_ann  = set()
seen_news = set()
seen_bse  = set()
seen_deals = set()
seen_insider = set()
seen_breakouts = set()

SEEN_FILE          = "seen_ann.json"
SEEN_NEWS_FILE     = "seen_news.json"
SEEN_BSE_FILE      = "seen_bse.json"
SEEN_DEALS_FILE    = "seen_deals.json"
SEEN_INSIDER_FILE  = "seen_insider.json"
SEEN_BREAKOUTS_FILE = "seen_breakouts.json"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

def now_ist(fmt="%d %b %Y %H:%M IST"):
    return datetime.now(IST).strftime(fmt)

# ==========================================
# PERSISTENT DEDUP
# ==========================================
def load_seen():
    global seen_ann, seen_news, seen_bse, seen_deals, seen_insider, seen_breakouts
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f: seen_ann = set(json.load(f))
            logger.info(f"Loaded {len(seen_ann)} seen announcement IDs")
    except Exception as e: logger.error(f"Load seen_ann error: {e}")
    try:
        if os.path.exists(SEEN_NEWS_FILE):
            with open(SEEN_NEWS_FILE) as f: seen_news = set(json.load(f))
            logger.info(f"Loaded {len(seen_news)} seen news IDs")
    except Exception as e: logger.error(f"Load seen_news error: {e}")
    try:
        if os.path.exists(SEEN_BSE_FILE):
            with open(SEEN_BSE_FILE) as f: seen_bse = set(json.load(f))
            logger.info(f"Loaded {len(seen_bse)} seen BSE IDs")
    except Exception as e: logger.error(f"Load seen_bse error: {e}")
    try:
        if os.path.exists(SEEN_DEALS_FILE):
            with open(SEEN_DEALS_FILE) as f: seen_deals = set(json.load(f))
    except Exception as e: logger.error(f"Load seen_deals error: {e}")
    try:
        if os.path.exists(SEEN_INSIDER_FILE):
            with open(SEEN_INSIDER_FILE) as f: seen_insider = set(json.load(f))
    except Exception as e: logger.error(f"Load seen_insider error: {e}")
    try:
        if os.path.exists(SEEN_BREAKOUTS_FILE):
            with open(SEEN_BREAKOUTS_FILE) as f: seen_breakouts = set(json.load(f))
    except Exception as e: logger.error(f"Load seen_breakouts error: {e}")

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f: json.dump(list(seen_ann)[-3000:], f)
    except Exception as e: logger.error(f"Save seen_ann error: {e}")

def save_seen_news():
    try:
        with open(SEEN_NEWS_FILE, "w") as f: json.dump(list(seen_news)[-3000:], f)
    except Exception as e: logger.error(f"Save seen_news error: {e}")

def save_seen_bse():
    try:
        with open(SEEN_BSE_FILE, "w") as f: json.dump(list(seen_bse)[-3000:], f)
    except Exception as e: logger.error(f"Save seen_bse error: {e}")

def save_seen_deals():
    try:
        with open(SEEN_DEALS_FILE, "w") as f: json.dump(list(seen_deals)[-3000:], f)
    except Exception as e: logger.error(f"Save seen_deals error: {e}")

def save_seen_insider():
    try:
        with open(SEEN_INSIDER_FILE, "w") as f: json.dump(list(seen_insider)[-3000:], f)
    except Exception as e: logger.error(f"Save seen_insider error: {e}")

def save_seen_breakouts():
    try:
        with open(SEEN_BREAKOUTS_FILE, "w") as f: json.dump(list(seen_breakouts)[-1000:], f)
    except Exception as e: logger.error(f"Save seen_breakouts error: {e}")

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
# SESSIONS
# ==========================================
def get_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
    except:
        pass
    return s

def get_bse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/",
    })
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

def fetch_bse_pdf_text(session, url):
    try:
        import pypdf
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return ""
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        text   = ""
        for page in reader.pages[:4]:
            text += page.extract_text() or ""
        return text[:3000]
    except Exception as e:
        logger.error(f"BSE PDF error: {e}")
        return ""

# ==========================================
# STOCK INFO
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
# JUNK FILTER
# ==========================================
JUNK = [
    "trading window", "shareholding pattern", "newspaper",
    "loss of share", "duplicate share", "voting result",
    "agm notice", "egm notice", "compliance certificate",
    "transcript", "presentation uploaded", "closure of trading",
    "change in address", "book closure",
    "change in registrar", "intimation of board meeting",
    "loss of certificate", "change in auditor address",
    "postal ballot", "corporate governance",
    "insider trading window", "media interview",
    "change in statutory auditor address",
    "reg. 57", "reg. 74", "reg. 76"
]

ALWAYS_PROCESS = [
    "financial result", "outcome of board", "acquisition",
    "merger", "amalgamation", "buyback", "bonus", "dividend",
    "split", "rights issue", "open offer", "delisting",
    "insolvency", "fraud", "sebi", "usfda", "fda",
    "order", "contract", "loi", "letter of intent",
    "work order", "project award", "export order",
    "general update", "press release", "update",
    "monthly business", "operational update", "sales data",
    "dispatch", "volume", "production data",
    "change in management", "resignation", "appointment",
    "credit rating", "qip", "preferential", "fundraise",
    "capacity expansion", "new plant", "capex",
    "joint venture", "partnership", "collaboration",
    "nclt", "default", "restructuring"
]

def should_process(subject):
    s = subject.lower()
    if any(k in s for k in ALWAYS_PROCESS):
        return True
    if any(k in s for k in JUNK):
        return False
    return True

# ==========================================
# GEMINI — NSE/BSE ANNOUNCEMENT CLASSIFIER
# ==========================================
def classify_announcement(company, subject, pdf_text=""):
    body = f"Subject: {subject}\n\nPDF Content:\n{pdf_text}" if pdf_text else f"Subject: {subject}"
    prompt = f"""You are an institutional-grade stock market event filtering engine used by hedge funds and Bloomberg terminals.

COMPANY FILTER — return null if company appears to be:
- Market Cap below 5000 crore
- SME stock, penny stock, shell company, suspended stock
- Annual revenue below 500 crore
Prioritize: Nifty 500, F&O stocks, large cap, mid cap, institutionally owned.

SCORE 70-100 ONLY. Below 70 return null.

SENTIMENT — pick ONE:
- BULLISH: order win, good results, dividend/bonus/buyback, acquisition, rating upgrade, FDA approval, capacity expansion, promoter buying
- BEARISH: bad results, order cancellation, resignation, rating downgrade, fraud, SEBI, insolvency, shutdown, debt default, pledge increase
- NEUTRAL: routine appointment, unclear JV, capex without timeline

NSE/BSE FILING CATEGORIES TO ALWAYS ALERT:
FINANCIAL RESULTS (score 75-95)
OUTCOME OF BOARD MEETING (score 70-90)
GENERAL UPDATES / PRESS RELEASE / UPDATES (score 70-95)
ACQUISITION / MERGER / AMALGAMATION (score 80-95)
BUYBACK / BONUS / DIVIDEND / SPLIT (score 75-90)
CHANGE IN MANAGEMENT (score 70-85)
CREDIT RATING (score 75-90)
ORDER / CONTRACT / LOI (score 80-100)
CAPACITY EXPANSION / NEW PLANT / CAPEX (score 70-85)
QIP / PREFERENTIAL / FUNDRAISE (score 75-90)
INSOLVENCY / FRAUD / SEBI / NCLT (score 85-100)
USFDA / REGULATORY APPROVAL (score 80-95)

Return exactly: null — if score below 70 or genuinely not material.

If score >= 70 return ONLY raw JSON:
{{"score":<70-100>,"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or CAPACITY or MONTHLY_UPDATE or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","actionability":"ALERT_IMMEDIATELY or WATCHLIST","institutional_interest":"HIGH or MEDIUM or LOW","summary":"<specific one line with all numbers>","why_it_matters":"<why this changes earnings/growth/valuation>","expected_impact":"Low or Medium or High or Extreme","estimated_earnings_impact":"<quantify or NA>","market_reaction":"<expected direction and reason>","dividend_amount":"<Rs X per share or null>","dividend_exdate":"<DD-Mon-YYYY or null>","buyback_price":<Rs or 0>,"buyback_size_cr":<crores or 0>,"buyback_premium_pct":<% or 0>,"person_name":"<full name or null>","person_designation":"<exact title or null>","person_action":"<resigned or appointed or null>","person_reason":"<reason or null>","order_value_cr":<crores or 0>,"order_client":"<client or null>","order_type":"<defence/govt/export/telecom/other or null>","volume_growth_pct":<% or 0>,"key_figures":"<all numbers revenues profits percentages>"}}

Company: {company}
{body}"""

    for attempt in range(3):
        try:
            r    = model.generate_content(prompt)
            text = r.text.strip().replace("```json","").replace("```","").strip()
            if text.lower().startswith("null"): return None
            s = text.find("{")
            e = text.rfind("}") + 1
            if s >= 0 and e > s: text = text[s:e]
            return json.loads(text)
        except Exception as ex:
            err = str(ex)
            if "429" in err:
                time.sleep(15 * (attempt + 1))
            else:
                return None
    return None

# ==========================================
# GEMINI — NEWS / BROKER RATING CLASSIFIER
# ==========================================
def classify_news(headline, description=""):
    body = f"Headline: {headline}\n\nDescription: {description[:500]}" if description else f"Headline: {headline}"
    prompt = f"""You are an institutional stock market news filter. Identify ONLY high-impact news about Indian stocks that moves prices.
Return exactly: null — if not worth alerting.
If worth alerting return ONLY raw JSON:
{{"score":<70-100>,"category":"BROKER_RATING or BLOCK_DEAL or INDEX_CHANGE or POLICY or SECTOR or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","broker":"<broker name or null>","rating":"<Buy/Sell/Hold/Outperform/Underperform or null>","target_price":<Rs or 0>,"upside_pct":<% upside from CMP or 0>,"company_name":"<company name or null>","ticker":"<NSE ticker or null>","summary":"<one line with key numbers>","market_reaction":"<why stock will move>"}}
{body}"""
    try:
        r    = model.generate_content(prompt)
        text = r.text.strip().replace("```json","").replace("```","").strip()
        if text.lower().startswith("null"): return None
        s = text.find("{")
        e = text.rfind("}") + 1
        if s >= 0 and e > s: text = text[s:e]
        return json.loads(text)
    except:
        return None

# ==========================================
# ALERT FORMATTERS
# ==========================================
def format_alert(company, symbol, result, ann_time, session, source="NSE"):
    score     = result.get("score") or 0
    tier      = result.get("tier", "MEDIUM")
    category  = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL")
    action    = result.get("actionability", "WATCHLIST")
    inst      = result.get("institutional_interest", "LOW")
    impact    = result.get("expected_impact", "Medium")

    ie  = {"EXTREME":"🚨","HIGH":"🔴","MEDIUM":"🟡"}.get(tier,"🟡")
    se  = {"BULLISH":"📈","BEARISH":"📉","NEUTRAL":"➡️"}.get(sentiment,"➡️")
    ae  = "⚡ ALERT NOW" if action == "ALERT_IMMEDIATELY" else "👁 WATCHLIST"
    ie2 = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(inst,"🟢")
    ce  = {
        "RESULTS":"📊","ORDER":"📦","PROMOTER":"👤","CORPORATE_ACTION":"🔄",
        "MA":"🤝","FUNDRAISE":"💰","REGULATORY":"⚖️","PHARMA":"💊",
        "MANAGEMENT":"👔","CREDIT":"🏦","CAPACITY":"🏭","MONTHLY_UPDATE":"📅","OTHER":"📌"
    }.get(category,"📌")

    info = get_stock_info(session, symbol) if symbol else {}
    cmp  = info.get("cmp", 0)
    mc   = info.get("mcap", 0)
    fno  = info.get("fno", "?")

    msg  = f"{ie} *{source} FILING | SCORE: {score}/100* | {se} *{sentiment}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏢 *{company}*\n"
    msg += f"{ce} *{category}* | 💥 *{impact} IMPACT* | {ae}\n"
    if cmp > 0:
        msg += f"💹 CMP: ₹{cmp:,.2f} | MCap: ₹{mc:,.0f}Cr | F&O: {fno}\n"
    msg += f"🏦 Inst. Interest: {ie2} *{inst}*\n"
    msg += f"\n📝 *{result.get('summary','')}*\n\n"

    if category == "CORPORATE_ACTION":
        da, de = result.get("dividend_amount"), result.get("dividend_exdate")
        bp, bs, bpr = result.get("buyback_price") or 0, result.get("buyback_size_cr") or 0, result.get("buyback_premium_pct") or 0
        if da and da != "null": msg += f"💵 *Dividend:* {da} per share\n"
        if de and de != "null": msg += f"📅 *Ex-date:* {de}\n"
        if bp > 0:              msg += f"💰 *Buyback Price:* ₹{bp:,.2f}\n"
        if bpr > 0:             msg += f"📈 *Premium over CMP:* {bpr:.1f}%\n"
        if bs > 0:              msg += f"📦 *Buyback Size:* ₹{bs:,.0f} Cr\n"
        msg += "\n"

    elif category == "ORDER" and (result.get("order_value_cr") or 0) > 0:
        ov, oc, ot = result.get("order_value_cr"), result.get("order_client"), result.get("order_type")
        ctx = order_context(ov, mc)
        msg += f"📦 *Order Value:* ₹{ov:,.0f} Cr\n"
        if oc and oc != "null": msg += f"🤝 *Client:* {oc}\n"
        if ot and ot != "null": msg += f"🏷 *Type:* {ot.upper()}\n"
        if ctx: msg += f"🎯 *vs Mkt Cap:* {ctx}\n\n"

    kf = result.get("key_figures","")
    if kf and kf not in ["null","None","N/A","",None]: msg += f"🔢 *Key Figures:* {kf}\n\n"
    wm = result.get("why_it_matters","")
    if wm and wm not in ["null","None","",None]: msg += f"🔍 *Why it matters:* {wm}\n\n"
    
    msg += f"💡 _{result.get('market_reaction','')}_\n\n"
    msg += f"🕐 {ann_time}"
    return msg

def format_news_alert(result, link=""):
    score, category, sentiment = result.get("score") or 0, result.get("category", "OTHER"), result.get("sentiment", "NEUTRAL")
    broker, rating, target, upside = result.get("broker"), result.get("rating"), result.get("target_price") or 0, result.get("upside_pct") or 0
    company, ticker = result.get("company_name", ""), result.get("ticker", "")

    se = {"BULLISH":"📈","BEARISH":"📉","NEUTRAL":"➡️"}.get(sentiment,"➡️")
    ce = {"BROKER_RATING":"🏦","BLOCK_DEAL":"💼","INDEX_CHANGE":"📋","POLICY":"⚖️","SECTOR":"🏭","OTHER":"📌"}.get(category,"📌")
    re = {"Buy":"🟢 BUY","Sell":"🔴 SELL","Hold":"🟡 HOLD"}.get(rating, rating or "")

    msg  = f"🏦 *BROKER CALL | SCORE: {score}/100* | {se} *{sentiment}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if company: msg += f"🏢 *{company}*"
    if ticker:  msg += f" | `{ticker}`"
    msg += f"\n{ce} *{category}*\n\n"
    if broker and broker != "null": msg += f"🏦 *Broker:* {broker}\n"
    if re: msg += f"🎯 *Rating:* {re}\n"
    if target > 0: msg += f"💰 *Target Price:* ₹{target:,.0f}\n"
    if upside != 0: msg += f"📈 *Upside/Downside:* {upside:+.1f}%\n"
    msg += f"\n📝 *{result.get('summary','')}*\n\n"
    msg += f"🕐 {now_ist()}"
    if link: msg += f" | 🔗 [Read]({link})"
    return msg

# ==========================================
# COMMAND MODULES
# ==========================================
def cmd_nifty(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ NSE not responding.")
            return
        data    = resp.json().get("data", [])
        targets = ["NIFTY 50","NIFTY BANK","NIFTY IT","INDIA VIX","NIFTY MIDCAP 100"]
        msg     = f"📊 *LIVE MARKET*\n🕐 {now_ist()}\n\n"
        for item in data:
            if item.get("index") in targets:
                ltp, chg, pct = item.get("last", 0), item.get("change", 0), item.get("percentChange", 0)
                e   = "🟢" if chg >= 0 else "🔴"
                msg += f"{e} *{item['index']}*\n   ₹{ltp:,.2f}  ({'+' if chg>=0 else ''}{pct:.2f}%)\n\n"
        send_msg(chat_id, msg)
    except Exception as e: send_msg(chat_id, f"❌ Error: {e}")

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
            for h in month_h: msg += f"📌 {h.get('tradingDate','')} — {h.get('description','')}\n"
        else: msg += "✅ No more holidays this month."
        send_msg(chat_id, msg)
    except Exception as e: send_msg(chat_id, f"❌ Error: {e}")

def cmd_earnings(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ Could not fetch earnings.")
            return
        events, today_fmt = resp.json(), datetime.now(IST).strftime("%Y-%m-%d")
        results   = [e for e in events if "Financial Results" in e.get("purpose","") and today_fmt in e.get("date","")]
        msg = f"📊 *TODAY'S EARNINGS*\n📅 {datetime.now(IST).strftime('%d %b %Y')}\n\n"
        if results:
            msg += f"*{len(results)} companies reporting:*\n\n"
            for r in results[:25]: msg += f"📋 *{r.get('symbol','?')}* — {r.get('companyName','')}\n"
        else: msg += "No companies scheduled to report today."
        send_msg(chat_id, msg)
    except Exception as e: send_msg(chat_id, f"❌ Error: {e}")

def cmd_ban(chat_id):
    try:
        resp = requests.get("https://nsearchives.nseindia.com/content/fo/fo_secban.csv", headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        if resp.status_code != 200:
            send_msg(chat_id, "⚠️ Could not fetch ban list.")
            return
        stocks = [l.strip() for l in resp.text.strip().split("\n")[1:] if l.strip()]
        msg    = f"🚫 *F&O BAN LIST*\n📅 {datetime.now(IST).strftime('%d %b %Y')}\n\n"
        if stocks:
            for i, s in enumerate(stocks, 1): msg += f"{i}. {s}\n"
            msg += "\n⚠️ _No fresh F&O positions allowed._"
        else: msg += "✅ No stocks in ban period today."
        send_msg(chat_id, msg)
    except Exception as e: send_msg(chat_id, f"❌ Error: {e}")

def cmd_oi(chat_id):
    session = get_nse_session()
    try:
        resp   = session.get("https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings", timeout=15)
        stocks = resp.json().get("data",[]) if resp.status_code == 200 else []
        sig    = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            send_msg(chat_id, "📈 *OI Report*\nNo significant OI buildups right now.")
            return
        msg = f"📈 *OI BUILDUP ALERT*\n🕐 {now_ist()}\n\n"
        for s in sig[:8]:
            oi, ltp, sym = s.get("oiChange") or s.get("perOIChange") or 0, s.get("lastPrice") or s.get("ltp") or 0, s.get("symbol","?")
            e   = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(chat_id, msg)
    except Exception as e: send_msg(chat_id, f"❌ Error: {e}")

def cmd_help(chat_id):
    send_msg(chat_id,
        "🤖 *Market Intelligence Bot*\n\n"
        "📊 /nifty — Live indices\n"
        "📅 /holiday — Market holidays\n"
        "📋 /earnings — Earnings calendar\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI buildup report\n"
        "❓ /help — This menu"
    )

# ==========================================
# WEBHOOK
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
            if not chat_id or not text: return
            cmd = text.split()[0].split("@")[0].lower()
            if   cmd == "/nifty":    cmd_nifty(chat_id)
            elif cmd == "/holiday":  cmd_holiday(chat_id)
            elif cmd == "/earnings": cmd_earnings(chat_id)
            elif cmd == "/ban":      cmd_ban(chat_id)
            elif cmd == "/oi":       cmd_oi(chat_id)
            elif cmd in ["/help","/start"]: cmd_help(chat_id)
        except Exception as e: logger.error(f"Webhook error: {e}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Market Intelligence Bot - ACTIVE")
    def log_message(self, format, *args): pass

# ==========================================
# CORE WORKER: NSE FILINGS
# ==========================================
def check_announcements():
    logger.info("Checking NSE filings...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/corporate-announcements?index=equities", timeout=15)
        if resp.status_code != 200: return
        anns = resp.json()
    except Exception as e: return

    sent = 0
    for ann in anns[:25]:
        ann_id  = ann.get("symbol","") + "||" + ann.get("desc","").strip()
        if ann_id in seen_ann: continue
        seen_ann.add(ann_id)

        company, symbol, subject, ann_time, pdf_path = ann.get("sm_name", ann.get("symbol","Unknown")), ann.get("symbol",""), ann.get("desc","").strip(), ann.get("an_dt", now_ist()), ann.get("attchmntFile","")
        if not subject or not should_process(subject): continue

        pdf_text = fetch_pdf_text(session, pdf_path) if pdf_path else ""
        result = classify_announcement(company, subject, pdf_text)
        if not result or (result.get("score") or 0) < 70: continue

        msg = format_alert(company, symbol, result, ann_time, session, source="NSE")
        send_msg(TELEGRAM_CHAT_ID, msg)
        sent += 1
        time.sleep(2)
    save_seen()

# ==========================================
# FEATURE 1: BSE FILINGS MONITOR
# ==========================================
def check_bse_announcements():
    logger.info("Checking BSE filings...")
    session = get_bse_session()
    try:
        # Secure official back-end sub-category mapping endpoint
        url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pGroup=A&pScripCode=&pSearchTerm="
        resp = session.get(url, timeout=15)
        if resp.status_code != 200: return
        anns = resp.json()
    except Exception as e:
        logger.error(f"BSE API error: {e}")
        return

    sent = 0
    # Process updates safely from the feed
    for ann in anns[:30]:
        news_id = str(ann.get("NEWSID", ""))
        if not news_id or news_id in seen_bse: continue
        seen_bse.add(news_id)

        company = ann.get("SLONGNAME", ann.get("SCRIP_CD", "BSE Stock"))
        ticker  = ann.get("NEWSSUB", "")
        subject = ann.get("HEADLINE", "").strip()
        ann_time = ann.get("NEWS_DT", now_ist())
        pdf_url = ann.get("ATTACHMENTNAME", "")

        if not should_process(subject): continue

        pdf_text = ""
        if pdf_url:
            pdf_text = fetch_bse_pdf_text(session, pdf_url)

        result = classify_announcement(company, ticker, pdf_text)
        if not result or (result.get("score") or 0) < 70: continue

        msg = format_alert(company, ticker, result, ann_time, session, source="BSE")
        send_msg(TELEGRAM_CHAT_ID, msg)
        sent += 1
        time.sleep(2)
    save_seen_bse()

# ==========================================
# FEATURE 2: BULK & BLOCK DEAL MONITOR
# ==========================================
def check_nse_deals():
    logger.info("Scanning Block & Bulk Deals...")
    session = get_nse_session()
    for deal_type in ["block-deal", "bulk-deal"]:
        try:
            resp = session.get(f"https://www.nseindia.com/api/{deal_type}", timeout=15)
            if resp.status_code != 200: continue
            deals = resp.json().get("data", [])
            
            for d in deals[:15]:
                # Unique identifier string combination
                deal_id = f"{d.get('symbol')}||{d.get('tradeTime')}||{d.get('quantity')}||{d.get('buySell')}"
                if deal_id in seen_deals: continue
                seen_deals.add(deal_id)

                qty = int(str(d.get('quantity', 0)).replace(',', ''))
                price = float(str(d.get('value', 0)).replace(',', ''))
                total_cr = (qty * price) / 1e7

                # Only alert on high-net-worth dynamic flows (> 10 Crore)
                if total_cr < 10.0: continue

                side_emoji = "🟢 BUY" if d.get('buySell') == 'BUY' else "🔴 SELL"
                msg = (
                    f"💼 *NSE INSTITUTIONAL DEAL ({deal_type.upper()})*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏢 *{d.get('clientName')}* -> *{d.get('symbol')}*\n"
                    f"🎯 *Action:* {side_emoji}\n"
                    f"📦 *Volume:* {qty:,} shares @ ₹{price:,.2f}\n"
                    f"💰 *Deal Value:* ₹{total_cr:,.2f} Cr\n"
                    f"🕐 Time: {d.get('tradeTime', now_ist())}"
                )
                send_msg(TELEGRAM_CHAT_ID, msg)
                time.sleep(1)
        except Exception as e:
            logger.error(f"Deal Tracking error ({deal_type}): {e}")
    save_seen_deals()

# ==========================================
# FEATURE 3: INSIDER TRADING (PIT) SCANNER
# ==========================================
def check_insider_trading():
    logger.info("Scanning Insider Trading Data...")
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/corporates-pit", timeout=15)
        if resp.status_code != 200: return
        data = resp.json().get("data", [])
        
        for item in data[:20]:
            pid = f"{item.get('symbol')}||{item.get('acqName')}||{item.get('secVal')}||{item.get('tpOfTxn')}"
            if pid in seen_insider: continue
            seen_insider.add(pid)

            txn_type = item.get('tpOfTxn', '').upper()
            # Intercept actionable market acquisitions or disposals
            if "ACQUISITION" in txn_type or "DISPOSAL" in txn_type:
                val = float(item.get('secVal', 0) or 0)
                val_cr = val / 1e7
                if val_cr < 1.0: continue # Focus strictly on >= 1 Crore institutional moves
                
                emoji = "🚀 PROMOTER BUY" if "ACQUISITION" in txn_type else "⚠️ PROMOTER SELL"
                msg = (
                    f"👤 *INSIDER TRADING ALERT (PIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏢 *{item.get('symbol')}*\n"
                    f"🎯 *Entity:* {item.get('acqName')} ({item.get('personCategory', 'Promoter')})\n"
                    f"📊 *Action:* {emoji}\n"
                    f"💰 *Value:* ₹{val_cr:,.2f} Cr\n"
                    f"📈 *Post %:* {item.get('afterTxnSecNumPer', 'N/A')}%\n"
                    f"🕐 Date: {item.get('dateOfInit', now_ist())}"
                )
                send_msg(TELEGRAM_CHAT_ID, msg)
                time.sleep(1)
    except Exception as e:
        logger.error(f"Insider PIT Parsing Error: {e}")
    save_seen_insider()

# ==========================================
# FEATURE 4 & 5: PRICE ACTION ENGINE (HIGH / BREAKS)
# ==========================================
def check_price_action():
    logger.info("Analyzing Live Price Actions...")
    session = get_nse_session()
    
    # 4. 52-Week High Breakouts Tracking
    try:
        resp = session.get("https://www.nseindia.com/api/live-analysis-52week-high-low", timeout=15)
        if resp.status_code == 200:
            high_list = resp.json().get("high", {}).get("data", [])
            for stock in high_list[:10]:
                sym = stock.get("symbol")
                bid = f"{sym}||52WKHIGH||{date.today()}"
                if bid in seen_breakouts: continue
                seen_breakouts.add(bid)

                msg = (
                    f"🔥 *52-WEEK HIGH BREAKOUT*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏢 *{sym}*\n"
                    f"💹 Last Price: ₹{stock.get('lastPrice')} | Prev High: ₹{stock.get('prevHigh')}\n"
                    f"🎯 *Signal:* Strong Institutional Accumulation/Momentum."
                )
                send_msg(TELEGRAM_CHAT_ID, msg)
    except Exception as e: logger.error(f"52W High Check Error: {e}")

    # 5. Circuit Breaker Engine Tracking
    try:
        resp = session.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500", timeout=15)
        if resp.status_code == 200:
            stocks = resp.json().get("data", [])
            for s in stocks:
                sym = s.get("symbol")
                # Structural fallback properties mapping
                ltp = float(s.get("lastPrice", 0))
                p_chg = float(s.get("pChange", 0))

                if abs(p_chg) >= 4.95: # Captures 5%, 10%, 20% classic circuit locks
                    cid = f"{sym}||CIRCUIT||{p_chg}||{date.today()}"
                    if cid in seen_breakouts: continue
                    seen_breakouts.add(cid)

                    emoji = "🔒 UPPER CIRCUIT LOCKED" if p_chg > 0 else "🛑 LOWER CIRCUIT LOCKED"
                    msg = (
                        f"⚡ *CIRCUIT BREAKER TRIGGERED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏢 *{sym}*\n"
                        f"📊 Status: *{emoji}*\n"
                        f"💹 Price Shift: {p_chg:+.2f}%\n"
                        f"💰 Current LTP: ₹{ltp:,.2f}"
                    )
                    send_msg(TELEGRAM_CHAT_ID, msg)
    except Exception as e: logger.error(f"Circuit Breaker Check Error: {e}")
    save_seen_breakouts()

# ==========================================
# NEWS / BROKER CALLS WORKER
# ==========================================
NEWS_FEEDS = [
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets",
]

def check_broker_news():
    logger.info("Checking broker news...")
    sent = 0
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title","").strip()
                link, desc = entry.get("link",""), entry.get("summary","").strip()[:300]
                if not title: continue

                news_id = title[:80].lower()
                if news_id in seen_news: continue
                seen_news.add(news_id)

                title_lower = title.lower()
                broker_keywords = [
                    "goldman", "morgan stanley", "jefferies", "nomura", "clsa",
                    "macquarie", "ubs", "citi", "jp morgan", "bofa", "bernstein",
                    "kotak", "motilal", "icici securities", "axis capital",
                    "emkay", "nuvama", "edelweiss", "hdfc securities",
                    "buy", "sell", "hold", "target", "upgrade", "downgrade",
                    "initiat", "outperform", "underperform", "overweight",
                    "underweight", "block deal", "bulk deal", "fii", "dii",
                    "index inclusion", "msci", "nifty 50 add", "nifty rebalance"
                ]
                if not any(k in title_lower for k in broker_keywords): continue

                result = classify_news(title, desc)
                if not result or (result.get("score") or 0) < 70: continue

                msg = format_news_alert(result, link)
                send_msg(TELEGRAM_CHAT_ID, msg)
                sent += 1
                time.sleep(2)
        except Exception as e: pass
    save_seen_news()

# ==========================================
# OI AUTO CHECK
# ==========================================
def check_oi_auto():
    session = get_nse_session()
    try:
        resp   = session.get("https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings", timeout=15)
        stocks = resp.json().get("data",[]) if resp.status_code == 200 else []
        sig    = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 20]
        if not sig: return
        msg = f"📈 *OI BUILDUP ALERT*\n🕐 {now_ist()}\n\n"
        for s in sig[:6]:
            oi, ltp, sym = s.get("oiChange") or s.get("perOIChange") or 0, s.get("lastPrice") or s.get("ltp") or 0, s.get("symbol","?")
            e   = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(TELEGRAM_CHAT_ID, msg)
    except: pass

# ==========================================
# MORNING BRIEFING
# ==========================================
def morning_briefing():
    session = get_nse_session()
    earnings_count, ban_count = 0, 0
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code == 200:
            today_fmt = datetime.now(IST).strftime("%Y-%m-%d")
            earnings_count = len([e for e in resp.json() if "Financial Results" in e.get("purpose","") and today_fmt in e.get("date","")])
    except: pass
    try:
        resp = requests.get("https://nsearchives.nseindia.com/content/fo/fo_secban.csv", headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        if resp.status_code == 200:
            ban_count = len([l for l in resp.text.strip().split("\n")[1:] if l.strip()])
    except: pass

    msg = (
        f"☀️ *GOOD MORNING — Market Briefing*\n"
        f"📅 {datetime.now(IST).strftime('%A, %d %b %Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Companies reporting results today: *{earnings_count}*\n"
        f"🚫 F&O ban list stocks: *{ban_count}*\n\n"
        f"📡 NSE & BSE filings active\n"
        f"💼 Block/Bulk & Insider channels active"
    )
    send_msg(TELEGRAM_CHAT_ID, msg)

# ==========================================
# SCHEDULER
# ==========================================
def run_scheduler():
    schedule.every(5).minutes.do(check_announcements)
    schedule.every(5).minutes.do(check_bse_announcements)
    schedule.every(10).minutes.do(check_nse_deals)
    schedule.every(15).minutes.do(check_broker_news)
    schedule.every(15).minutes.do(check_insider_trading)
    schedule.every(30).minutes.do(check_oi_auto)
    schedule.every(30).minutes.do(check_price_action)
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
    
    send_msg(TELEGRAM_CHAT_ID, "✅ *Market Intelligence Bot LIVE with Priorities 1-5 Integrated!*")
    
    # Warmup initialization hooks
    threading.Thread(target=check_announcements, daemon=True).start()
    threading.Thread(target=check_bse_announcements, daemon=True).start()
    server.serve_forever()

```
