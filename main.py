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
SEEN_FILE      = "seen_ann.json"
SEEN_NEWS_FILE = "seen_news.json"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

def now_ist(fmt="%d %b %Y %H:%M IST"):
    return datetime.now(IST).strftime(fmt)

# ==========================================
# PERSISTENT DEDUP
# ==========================================
def load_seen():
    global seen_ann, seen_news
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f:
                seen_ann = set(json.load(f))
            logger.info(f"Loaded {len(seen_ann)} seen announcement IDs")
    except Exception as e:
        logger.error(f"Load seen_ann error: {e}")
    try:
        if os.path.exists(SEEN_NEWS_FILE):
            with open(SEEN_NEWS_FILE) as f:
                seen_news = set(json.load(f))
            logger.info(f"Loaded {len(seen_news)} seen news IDs")
    except Exception as e:
        logger.error(f"Load seen_news error: {e}")

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_ann)[-3000:], f)
    except Exception as e:
        logger.error(f"Save seen_ann error: {e}")

def save_seen_news():
    try:
        with open(SEEN_NEWS_FILE, "w") as f:
            json.dump(list(seen_news)[-3000:], f)
    except Exception as e:
        logger.error(f"Save seen_news error: {e}")

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

# These subjects must ALWAYS reach Gemini — never skip
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
    # Always process if subject contains important keywords
    if any(k in s for k in ALWAYS_PROCESS):
        return True
    # Skip if junk
    if any(k in s for k in JUNK):
        return False
    # Default — send to Gemini to decide
    return True

# ==========================================
# GEMINI — NSE ANNOUNCEMENT CLASSIFIER
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

NSE FILING CATEGORIES TO ALWAYS ALERT:

FINANCIAL RESULTS (score 75-95):
Alert if revenue growth >20% OR profit growth >25% OR EBITDA growth >20% OR margin expansion >150bps OR guidance upgrade.
Extract: revenue, profit, EBITDA, YoY growth %, guidance.

OUTCOME OF BOARD MEETING (score 70-90):
Contains dividend, buyback, capex approval, fund raise, merger approval — extract exact details.

GENERAL UPDATES / PRESS RELEASE / UPDATES (score 70-95):
These often contain order wins, JV deals, new plants, export agreements.
Look carefully at PDF — extract order value, client, contract type.

MONTHLY BUSINESS UPDATE / OPERATIONAL UPDATE (score 70-85):
Auto sector volumes, dispatch numbers, retail sales.
Alert if YoY growth >10% or decline >10%.
Extract: total volumes, YoY growth %, key models.

ACQUISITION / MERGER / AMALGAMATION (score 80-95):
Extract deal value, strategic significance, earnings impact.

BUYBACK / BONUS / DIVIDEND / SPLIT (score 75-90):
Extract exact amounts, dates, premium over CMP.

CHANGE IN MANAGEMENT (score 70-85):
CEO/CFO/MD changes are HIGH priority.
Extract: full name, designation, resigned or appointed, reason.

CREDIT RATING (score 75-90):
Upgrade or downgrade — extract rating, agency, outlook.

ORDER / CONTRACT / LOI (score 80-100):
Extract exact value in crores, client name, contract type, duration.
Compare with company marketcap for context.

CAPACITY EXPANSION / NEW PLANT / CAPEX (score 70-85):
Extract investment amount, capacity addition, timeline.

QIP / PREFERENTIAL / FUNDRAISE (score 75-90):
Extract amount, price, dilution %.

INSOLVENCY / FRAUD / SEBI / NCLT (score 85-100):
Always extreme priority — extract all details.

USFDA / REGULATORY APPROVAL (score 80-95):
Drug approvals, warning letters — extract drug name, market size.

IGNORE COMPLETELY — return null:
Trading window, shareholding pattern, newspaper ad, AGM/EGM notice,
compliance certificate, loss of share certificate, voting results,
change of address, postal ballot, book closure notice.

Return exactly: null — if score below 70 or genuinely not material.

If score >= 70 return ONLY raw JSON:
{{"score":<70-100>,"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or CAPACITY or MONTHLY_UPDATE or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","actionability":"ALERT_IMMEDIATELY or WATCHLIST","institutional_interest":"HIGH or MEDIUM or LOW","summary":"<specific one line with all numbers>","why_it_matters":"<why this changes earnings/growth/valuation>","expected_impact":"Low or Medium or High or Extreme","estimated_earnings_impact":"<quantify or NA>","market_reaction":"<expected direction and reason>","dividend_amount":"<Rs X per share or null>","dividend_exdate":"<DD-Mon-YYYY or null>","buyback_price":<Rs or 0>,"buyback_size_cr":<crores or 0>,"buyback_premium_pct":<% or 0>,"person_name":"<full name or null>","person_designation":"<exact title or null>","person_action":"<resigned or appointed or null>","person_reason":"<reason or null>","order_value_cr":<crores or 0>,"order_client":"<client or null>","order_type":"<defence/govt/export/telecom/other or null>","volume_growth_pct":<% or 0>,"key_figures":"<all numbers revenues profits percentages>"}}

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
# GEMINI — NEWS / BROKER RATING CLASSIFIER
# ==========================================
def classify_news(headline, description=""):
    body = f"Headline: {headline}\n\nDescription: {description[:500]}" if description else f"Headline: {headline}"

    prompt = f"""You are an institutional stock market news filter.

Identify ONLY high-impact news about Indian stocks that moves prices.

ALERT FOR:
- Broker initiations: Goldman Sachs, Morgan Stanley, Jefferies, Nomura, CLSA, Macquarie, UBS, Citi, JP Morgan, BofA, Kotak, Motilal Oswal, ICICI Securities, Axis, Emkay, Nuvama, Edelweiss, Bernstein
- Rating changes: Buy, Sell, Hold, Outperform, Underperform, Neutral, Overweight, Underweight
- Target price upgrades or downgrades
- Earnings estimate revisions
- Sector upgrades or downgrades
- Major institutional buy/sell calls
- Block deals by known institutions
- FII/DII bulk deals
- Index inclusion or exclusion (Nifty 50, Nifty 500, MSCI, FTSE)
- Regulatory news (SEBI, RBI, government policy affecting sectors)
- Major sector-level news (PLI scheme, budget impact, policy change)

IGNORE:
- Generic market commentary
- Crypto news
- Global macro without India stock impact
- Repetitive news already widely covered
- Tips or speculation without credible source

Return exactly: null — if not worth alerting.

If worth alerting return ONLY raw JSON:
{{"score":<70-100>,"category":"BROKER_RATING or BLOCK_DEAL or INDEX_CHANGE or POLICY or SECTOR or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","broker":"<broker name or null>","rating":"<Buy/Sell/Hold/Outperform/Underperform or null>","target_price":<Rs or 0>,"upside_pct":<% upside from CMP or 0>,"company_name":"<company name or null>","ticker":"<NSE ticker or null>","summary":"<one line with key numbers>","market_reaction":"<why stock will move>"}}

{body}"""

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
        logger.error(f"Gemini news error: {str(ex)[:100]}")
        return None

# ==========================================
# NSE ALERT FORMATTER
# ==========================================
def format_alert(company, symbol, result, ann_time, session):
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

    msg  = f"{ie} *NSE FILING | SCORE: {score}/100* | {se} *{sentiment}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏢 *{company}*\n"
    msg += f"{ce} *{category}* | 💥 *{impact} IMPACT* | {ae}\n"
    if cmp > 0:
        msg += f"💹 CMP: ₹{cmp:,.2f} | MCap: ₹{mc:,.0f}Cr | F&O: {fno}\n"
    msg += f"🏦 Inst. Interest: {ie2} *{inst}*\n"
    msg += f"\n📝 *{result.get('summary','')}*\n\n"

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
        if pr and pr not in ["null",None]: msg += f"💬 *Reason:* {pr}\n"
        msg += "\n"

    elif category == "ORDER":
        ov = result.get("order_value_cr") or 0
        oc = result.get("order_client")
        ot = result.get("order_type")
        if ov > 0:
            ctx = order_context(ov, mc)
            msg += f"📦 *Order Value:* ₹{ov:,.0f} Cr\n"
            if oc and oc != "null": msg += f"🤝 *Client:* {oc}\n"
            if ot and ot != "null": msg += f"🏷 *Type:* {ot.upper()}\n"
            if ctx: msg += f"🎯 *vs Mkt Cap:* {ctx}\n"
            msg += "\n"

    elif category == "MONTHLY_UPDATE":
        vg = result.get("volume_growth_pct") or 0
        if vg != 0:
            arrow = "📈" if vg > 0 else "📉"
            msg += f"{arrow} *Volume Growth:* {vg:+.1f}% YoY\n\n"

    kf = result.get("key_figures","")
    if kf and kf not in ["null","None","N/A","",None]:
        msg += f"🔢 *Key Figures:* {kf}\n\n"

    wm = result.get("why_it_matters","")
    if wm and wm not in ["null","None","",None]:
        msg += f"🔍 *Why it matters:* {wm}\n\n"

    ei = result.get("estimated_earnings_impact","")
    if ei and ei not in ["null","None","NA","",None]:
        msg += f"💰 *Earnings Impact:* {ei}\n\n"

    msg += f"💡 _{result.get('market_reaction','')}_\n\n"
    msg += f"🕐 {ann_time}"
    return msg

# ==========================================
# NEWS ALERT FORMATTER
# ==========================================
def format_news_alert(result, link=""):
    score     = result.get("score") or 0
    category  = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL")
    broker    = result.get("broker")
    rating    = result.get("rating")
    target    = result.get("target_price") or 0
    upside    = result.get("upside_pct") or 0
    company   = result.get("company_name", "")
    ticker    = result.get("ticker", "")

    se  = {"BULLISH":"📈","BEARISH":"📉","NEUTRAL":"➡️"}.get(sentiment,"➡️")
    ce  = {
        "BROKER_RATING":"🏦","BLOCK_DEAL":"💼","INDEX_CHANGE":"📋",
        "POLICY":"⚖️","SECTOR":"🏭","OTHER":"📌"
    }.get(category,"📌")

    re  = {"Buy":"🟢 BUY","Sell":"🔴 SELL","Hold":"🟡 HOLD",
           "Outperform":"🟢 OUTPERFORM","Underperform":"🔴 UNDERPERFORM",
           "Neutral":"🟡 NEUTRAL","Overweight":"🟢 OVERWEIGHT",
           "Underweight":"🔴 UNDERWEIGHT"}.get(rating, rating or "")

    msg  = f"🏦 *BROKER CALL | SCORE: {score}/100* | {se} *{sentiment}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if company: msg += f"🏢 *{company}*"
    if ticker:  msg += f" | `{ticker}`"
    msg += f"\n{ce} *{category}*\n\n"

    if broker and broker != "null":
        msg += f"🏦 *Broker:* {broker}\n"
    if re:
        msg += f"🎯 *Rating:* {re}\n"
    if target > 0:
        msg += f"💰 *Target Price:* ₹{target:,.0f}\n"
    if upside != 0:
        arrow = "📈" if upside > 0 else "📉"
        msg += f"{arrow} *Upside/Downside:* {upside:+.1f}%\n"

    msg += f"\n📝 *{result.get('summary','')}*\n\n"
    msg += f"💡 _{result.get('market_reaction','')}_\n\n"
    msg += f"🕐 {now_ist()}"
    if link:
        msg += f" | 🔗 [Read]({link})"
    return msg

# ==========================================
# COMMANDS
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
            headers={"User-Agent":"Mozilla/5.0"}, timeout=15
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
        "📊 /nifty — Live indices + Midcap\n"
        "📅 /holiday — Market holidays\n"
        "📋 /earnings — Today's results calendar\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI buildup report\n"
        "❓ /help — This menu\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "*Auto Alerts:*\n"
        "📡 NSE filings — every 5 min\n"
        "🏦 Broker ratings — every 15 min\n"
        "📈 OI report — every 30 min\n"
        "☀️ Morning briefing — 8:30 AM\n\n"
        "_Score 70+/100 | Nifty 500 focus_"
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
# NSE ANNOUNCEMENT CHECK
# ==========================================
def check_announcements():
    logger.info(f"[{now_ist('%H:%M IST')}] Checking NSE filings...")
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

        if not should_process(subject):
            logger.info(f"  JUNK | {subject[:50]}")
            continue

        logger.info(f"  CHECK | {company[:25]} | {subject[:50]}")

        # Fetch PDF for all non-junk
        pdf_text = ""
        if pdf_path:
            pdf_text = fetch_pdf_text(session, pdf_path)
            if pdf_text:
                logger.info(f"  PDF: {len(pdf_text)} chars")

        result = classify_announcement(company, subject, pdf_text)
        if not result:
            logger.info(f"  SKIP | {company[:30]}")
            continue

        score = result.get("score") or 0
        if score < 70:
            logger.info(f"  LOW SCORE ({score}) | {company[:30]}")
            continue

        logger.info(f"  {score}/100 | {result.get('tier')} | {result.get('sentiment')} | {company[:25]}")
        msg = format_alert(company, symbol, result, ann_time, session)
        send_msg(TELEGRAM_CHAT_ID, msg)
        logger.info(f"  ✅ SENT: {company} | {score}/100")
        sent += 1
        time.sleep(2)

    save_seen()
    logger.info(f"NSE Done. Sent: {sent}")

# ==========================================
# BROKER RATING / NEWS CHECK
# ==========================================
NEWS_FEEDS = [
    # MoneyControl
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    # Economic Times Markets
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    # Business Standard Markets
    "https://www.business-standard.com/rss/markets-106.rss",
    # LiveMint Markets
    "https://www.livemint.com/rss/markets",
]

def check_broker_news():
    logger.info(f"[{now_ist('%H:%M IST')}] Checking broker news...")
    sent = 0

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title","").strip()
                link  = entry.get("link","")
                desc  = entry.get("summary","").strip()[:300]

                if not title:
                    continue

                # Dedup by title
                news_id = title[:80].lower()
                if news_id in seen_news:
                    continue
                seen_news.add(news_id)

                # Pre-filter — only process if broker/rating keywords present
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

                if not any(k in title_lower for k in broker_keywords):
                    continue

                logger.info(f"  NEWS CHECK | {title[:60]}")
                result = classify_news(title, desc)
                if not result:
                    continue

                score = result.get("score") or 0
                if score < 70:
                    continue

                logger.info(f"  NEWS {score}/100 | {result.get('category')} | {title[:40]}")
                msg = format_news_alert(result, link)
                send_msg(TELEGRAM_CHAT_ID, msg)
                logger.info(f"  ✅ NEWS SENT: {title[:40]}")
                sent += 1
                time.sleep(2)

        except Exception as e:
            logger.error(f"Feed error {feed_url[:40]}: {e}")

    save_seen_news()
    logger.info(f"News Done. Sent: {sent}")

# ==========================================
# OI AUTO CHECK
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
        logger.info(f"OI alert sent")
    except Exception as e:
        logger.error(f"OI error: {e}")

# ==========================================
# MORNING BRIEFING
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
        f"📡 NSE filings — every 5 min\n"
        f"🏦 Broker ratings — every 15 min\n"
        f"🎯 Institutional grade — Score 70+/100\n\n"
        f"_Type /earnings for full results list_\n"
        f"_Type /ban for banned stocks list_"
    )
    send_msg(TELEGRAM_CHAT_ID, msg)

# ==========================================
# SCHEDULER
# ==========================================
def run_scheduler():
    schedule.every(5).minutes.do(check_announcements)
    schedule.every(15).minutes.do(check_broker_news)
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
        "📡 NSE filings — every 5 min\n"
        "🏦 Broker ratings & news — every 15 min\n"
        "🎯 Institutional grade — Score 70+/100\n"
        "🏢 Nifty 500 / F&O / Large & Mid Cap\n"
        "📈 OI alerts — every 30 min\n"
        "☀️ Morning briefing — 8:30 AM IST\n\n"
        "Type /help for all commands"
    )
    threading.Thread(target=check_announcements, daemon=True).start()
    threading.Thread(target=check_broker_news, daemon=True).start()
    server.serve_forever()
