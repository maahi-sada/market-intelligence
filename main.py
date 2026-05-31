import requests
import google.generativeai as genai
import schedule
import time
import json
import os
import threading
from datetime import datetime, date
import pytz
IST = pytz.timezone("Asia/Kolkata")
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI"
TELEGRAM_CHAT_ID = "787902453"
GEMINI_API_KEY = "AQ.Ab8RN6KGEW36r2cX1-qepXdK8tqyrZoJqnJb2dmbPdX61HFi-Q"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash-lite")

seen_ann = set()
SEEN_FILE = "/app/seen_ann.json"


def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE) as f:
                data = json.load(f)
                seen_ann.update(data)
                print(f"Loaded {len(seen_ann)} seen announcements")
    except:
        pass


def save_seen():
    try:
        items = list(seen_ann)[-2000:]
        with open(SEEN_FILE, "w") as f:
            json.dump(items, f)
    except:
        pass


def send_msg(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def get_nse_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        session.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
    except:
        pass
    return session


def fetch_pdf_text(session, pdf_path):
    try:
        import io
        try:
            import pypdf
            pdf_lib = "pypdf"
        except:
            try:
                import PyPDF2
                pdf_lib = "pypdf2"
            except:
                return ""
        url = f"https://www.nseindia.com{pdf_path}" if pdf_path.startswith("/") else pdf_path
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return ""
        pdf_bytes = io.BytesIO(resp.content)
        text = ""
        if pdf_lib == "pypdf":
            reader = pypdf.PdfReader(pdf_bytes)
            for page in reader.pages[:3]:
                text += page.extract_text() or ""
        else:
            reader = PyPDF2.PdfReader(pdf_bytes)
            for page in reader.pages[:3]:
                text += page.extract_text() or ""
        return text[:2000]
    except Exception as e:
        print(f"PDF error: {e}")
        return ""


def get_company_marketcap(session, symbol):
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            info = data.get("metadata", {})
            price = info.get("lastPrice", 0)
            shares = data.get("securityInfo", {}).get("issuedSize", 0)
            if price and shares:
                return (price * shares) / 1e7
    except:
        pass
    return 0


def order_context(order_value_cr, marketcap_cr):
    if marketcap_cr <= 0:
        return ""
    ratio = (order_value_cr / marketcap_cr) * 100
    if ratio >= 100:
        return f"🚀 MASSIVE — {ratio:.0f}% of Marketcap!"
    elif ratio >= 50:
        return f"🔴 HUGE — {ratio:.0f}% of Marketcap"
    elif ratio >= 20:
        return f"🟠 LARGE — {ratio:.0f}% of Marketcap"
    elif ratio >= 10:
        return f"🟡 SIGNIFICANT — {ratio:.0f}% of Marketcap"
    else:
        return f"⚪ ROUTINE — {ratio:.0f}% of Marketcap"


def classify(company, subject, pdf_text=""):
    combined = f"Subject: {subject}\n\nPDF Content:\n{pdf_text[:1500]}" if pdf_text else f"Subject: {subject}"

    prompt = f"""You are a senior equity analyst at a top hedge fund. Classify NSE announcements strictly.

SENTIMENT RULES — follow exactly:
- BULLISH: good results, order win, dividend, bonus, buyback, acquisition of asset, rating upgrade, new product approval
- BEARISH: bad results, order loss/cancellation, CEO/CFO resignation, rating downgrade, fraud, SEBI action, insolvency, plant shutdown
- NEUTRAL: routine appointment, JV with unclear terms, capex with no timeline

TIER RULES:

EXTREME (score 8-10):
- Quarterly/annual results — ALWAYS classify if profit/revenue mentioned, even partial
- Merger, acquisition, takeover, demerger
- SEBI action, fraud, forensic audit, auditor resignation
- Insolvency, NCLT, debt default
- USFDA approval or warning letter
- Promoter stake sale >2%

HIGH (score 5-7):
- Order win or cancellation >100 crore
- Buyback, bonus, stock split
- QIP, preferential allotment with price
- CEO or CFO resignation or appointment
- Credit rating change
- Block deal >1% equity
- Debt restructuring

MEDIUM (score 3-4):
- Dividend with exact amount
- Promoter pledge increase >5%
- Patent win/loss (pharma)
- Plant fire or shutdown
- Capex >200 crore
- Strategic JV or partnership

IGNORE — return null:
- AGM/EGM notice
- Board meeting intimation (without agenda)
- Shareholding pattern
- Newspaper ad
- Compliance/trading window/closure
- Analyst meet or concall schedule
- Transcript upload
- CSR/ESG
- Loss of share certificate
- Voting results
- Exchange clarification
- Website update
- General updates without numbers

CRITICAL: For RESULTS announcements — always extract profit, revenue, growth % from PDF.
CRITICAL: Never give both BULLISH and BEARISH for same announcement. Pick ONE based on net impact.
CRITICAL: If announcement is genuinely ambiguous, use NEUTRAL.

Return exactly null if not in any tier.

If in a tier return ONLY this raw JSON (no markdown, no explanation):
{{"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","score":<3-10>,"summary":"<specific one line — include numbers if available>","market_reaction":"<one line — why stock will move and in which direction>","dividend_amount":"<Rs X per share or null>","dividend_exdate":"<DD-Mon-YYYY or null>","person_name":"<full name or null>","person_designation":"<exact title or null>","person_action":"<resigned or appointed or null>","person_reason":"<reason if mentioned or null>","order_value_cr":<crores as number or 0>,"key_figures":"<revenue profit growth % all numbers>"}}

Company: {company}
{combined}"""

    for attempt in range(3):
        try:
            r = model.generate_content(prompt)
            text = r.text.strip().replace("```json", "").replace("```", "").strip()
            if text.lower() == "null" or text == "":
                return None
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
            return json.loads(text)
        except Exception as e:
            err = str(e)
            if "429" in err:
                wait = 20 + (attempt * 15)
                print(f"  Rate limit — waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Gemini error: {e}")
                return None
    return None


def format_alert(company, symbol, result, ann_time, session):
    tier = result.get("tier", "MEDIUM")
    category = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL")
    score = result.get("score") or 0

    te = {"EXTREME": "🔴 EXTREME", "HIGH": "🟠 HIGH", "MEDIUM": "🟡 MEDIUM"}.get(tier, "🟡 MEDIUM")
    ie = {"EXTREME": "🚨", "HIGH": "🔴", "MEDIUM": "🟡"}.get(tier, "🟡")
    se = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(sentiment, "➡️")
    ce = {
        "RESULTS": "📊", "ORDER": "📦", "PROMOTER": "👤",
        "CORPORATE_ACTION": "🔄", "MA": "🤝", "FUNDRAISE": "💰",
        "REGULATORY": "⚖️", "PHARMA": "💊", "MANAGEMENT": "👔",
        "CREDIT": "🏦", "OTHER": "📌"
    }.get(category, "📌")

    msg = (
        f"{ie} *{te} ALERT* | {se} *{sentiment}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *{company}*\n"
        f"{ce} *{category}* | ⚡ *{score}/10*\n\n"
        f"📝 {result.get('summary', '')}\n\n"
    )

    if category == "CORPORATE_ACTION":
        div_amt = result.get("dividend_amount")
        div_ex = result.get("dividend_exdate")
        if div_amt and div_amt != "null":
            msg += f"💵 *Dividend:* {div_amt} per share\n"
        if div_ex and div_ex != "null":
            msg += f"📅 *Ex-date:* {div_ex}\n"
        msg += "\n"

    elif category == "MANAGEMENT":
        name = result.get("person_name")
        desig = result.get("person_designation")
        action = result.get("person_action")
        reason = result.get("person_reason")
        if name and name != "null":
            msg += f"👤 *Person:* {name}\n"
        if desig and desig != "null":
            msg += f"🎯 *Role:* {desig}\n"
        if action and action != "null":
            msg += f"🔄 *Action:* {action.upper()}\n"
        if reason and reason not in ["null", None]:
            msg += f"💬 *Reason:* {reason}\n"
        msg += "\n"

    elif category == "ORDER":
        order_cr = result.get("order_value_cr") or 0
        if order_cr > 0:
            mc = get_company_marketcap(session, symbol) if symbol else 0
            ctx = order_context(order_cr, mc) if mc > 0 else ""
            msg += f"📦 *Order Value:* ₹{order_cr:,.0f} Cr\n"
            if mc > 0:
                msg += f"📊 *Mkt Cap:* ₹{mc:,.0f} Cr\n"
            if ctx:
                msg += f"🎯 *Size:* {ctx}\n"
            msg += "\n"

    kf = result.get("key_figures", "")
    if kf and kf not in ["null", "None", "N/A", "", None]:
        msg += f"🔢 *Figures:* {kf}\n\n"

    msg += f"💡 _{result.get('market_reaction', '')}_\n\n"
    msg += f"🕐 {ann_time}"

    return msg


def handle_nifty(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            targets = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX"]
            msg = f"📊 *LIVE INDICES*\n🕐 {datetime.now(IST).strftime('%H:%M IST')}\n\n"
            for item in data:
                if item.get("index") in targets:
                    ltp = item.get("last", 0)
                    chg = item.get("change", 0)
                    pct = item.get("percentChange", 0)
                    e = "🟢" if chg >= 0 else "🔴"
                    msg += f"{e} *{item['index']}*\n"
                    msg += f"₹{ltp:,.2f}  {'+' if chg>=0 else ''}{chg:.2f} ({pct:+.2f}%)\n\n"
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch data right now.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")


def handle_holiday(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=15)
        if resp.status_code == 200:
            holidays = resp.json().get("CM", [])
            today = date.today()
            month = today.strftime("%b").upper()
            month_h = [h for h in holidays if month in h.get("tradingDate", "").upper()]
            msg = f"📅 *MARKET HOLIDAYS — {today.strftime('%B %Y')}*\n\n"
            if month_h:
                for h in month_h:
                    msg += f"📌 {h.get('tradingDate', '')} — {h.get('description', '')}\n"
            else:
                msg += "✅ No holidays this month."
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch holiday data.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")


def handle_earnings(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/event-calendar?index=equities", timeout=15)
        if resp.status_code == 200:
            events = resp.json()
            today_fmt = datetime.now().strftime("%Y-%m-%d")
            results = [
                e for e in events
                if "Financial Results" in e.get("purpose", "")
                and today_fmt in e.get("date", "")
            ]
            msg = f"📊 *TODAY'S EARNINGS — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if results:
                msg += f"*{len(results)} companies reporting:*\n\n"
                for r in results[:20]:
                    msg += f"📋 *{r.get('symbol', '?')}* — {r.get('companyName', '')}\n"
            else:
                msg += "No results scheduled today."
            send_msg(chat_id, msg)
        else:
            send_msg(chat_id, "⚠️ Could not fetch earnings data.")
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")


def handle_ban(chat_id):
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        if resp.status_code == 200:
            lines = resp.text.strip().split("\n")
            stocks = [l.strip() for l in lines[1:] if l.strip()]
            msg = f"🚫 *F&O BAN LIST — {datetime.now().strftime('%d %b %Y')}*\n\n"
            if stocks:
                msg += f"*{len(stocks)} stocks banned:*\n"
                for i, s in enumerate(stocks, 1):
                    msg += f"{i}. {s}\n"
                msg += "\n⚠️ _No fresh F&O positions allowed._"
            else:
                msg += "✅ No stocks in ban period today."
        else:
            msg = "⚠️ Could not fetch ban list."
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")


def handle_oi(chat_id):
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            timeout=15
        )
        stocks = resp.json().get("data", []) if resp.status_code == 200 else []
        sig = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            send_msg(chat_id, "📈 *OI Report* — No significant spikes right now.")
            return
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now(IST).strftime('%H:%M IST')}\n\n"
        for s in sig[:6]:
            oi = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol", "?")
            e = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(chat_id, msg)
    except Exception as e:
        send_msg(chat_id, f"❌ Error: {e}")


def handle_help(chat_id):
    send_msg(chat_id, (
        "🤖 *Market Intelligence Bot — Commands*\n\n"
        "📊 /nifty — Live Nifty, Bank Nifty, IT, VIX\n"
        "📅 /holiday — Market holidays this month\n"
        "📋 /earnings — Companies reporting today\n"
        "🚫 /ban — F&O ban list\n"
        "📈 /oi — OI spike report on demand\n"
        "❓ /help — This menu\n\n"
        "_Auto alerts every 6 min — material events only_\n"
        "_OI intelligence report every 30 min_"
    ))


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        try:
            data = json.loads(body)
            message = data.get("message", {})
            text = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if not chat_id or not text:
                return
            print(f"Command: {text} from {chat_id}")
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/nifty":
                handle_nifty(chat_id)
            elif cmd == "/holiday":
                handle_holiday(chat_id)
            elif cmd == "/earnings":
                handle_earnings(chat_id)
            elif cmd == "/ban":
                handle_ban(chat_id)
            elif cmd == "/oi":
                handle_oi(chat_id)
            elif cmd in ["/help", "/start"]:
                handle_help(chat_id)
        except Exception as e:
            print(f"Webhook error: {e}")

    def log_message(self, format, *args):
        pass


JUNK_KEYWORDS = [
    "newspaper", "trading window", "shareholding pattern",
    "loss of share", "voting result", "agm notice", "egm notice",
    "compliance certificate", "website", "investor meet",
    "con. call updates", "transcript", "presentation uploaded",
    "closure of", "change in address", "book closure"
]


def check_announcements():
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Checking announcements...")
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=15
        )
        if resp.status_code != 200:
            print(f"NSE status: {resp.status_code}")
            return
        anns = resp.json()
        print(f"Got {len(anns)} announcements")
    except Exception as e:
        print(f"NSE error: {e}")
        return

    sent = 0
    for ann in anns[:25]:
        ann_id = ann.get("symbol", "") + "||" + ann.get("desc", "").strip()
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)

        company = ann.get("sm_name", ann.get("symbol", "Unknown"))
        symbol = ann.get("symbol", "")
        subject = ann.get("desc", "").strip()
        ann_time = ann.get("an_dt", datetime.now(IST).strftime("%Y-%m-%d %H:%M"))
        pdf_path = ann.get("attchmntFile", "")

        print(f"  RAW | {subject[:80]}")

        subject_lower = subject.lower()
        if any(k in subject_lower for k in JUNK_KEYWORDS):
            print(f"  JUNK SKIP | {subject[:50]}")
            continue

        # Only fetch PDF for high-value announcement types
        pdf_text = ""
        subject_upper = subject.upper()
        pdf_worthy = any(k in subject_upper for k in [
            "RESULT", "FINANCIAL", "QUARTER", "ANNUAL", "PROFIT",
            "REVENUE", "ACQUISITION", "MERGER", "AMALGAMAT",
            "FRAUD", "SEBI", "INSOLVENCY", "DEFAULT", "USFDA"
        ])
        if pdf_path and pdf_worthy:
            pdf_text = fetch_pdf_text(session, pdf_path)
            if pdf_text:
                print(f"  PDF: {len(pdf_text)} chars")
        elif pdf_path:
            print(f"  PDF skipped — not pdf-worthy")

        result = classify(company, subject, pdf_text)
        if not result:
            print(f"  SKIP | {company[:40]}")
            continue

        score = result.get("score") or 0
        tier = result.get("tier", "")
        sentiment = result.get("sentiment", "")
        print(f"  {score}/10 | {tier} | {sentiment} | {company[:30]}")

        if score < 3:
            continue

        msg = format_alert(company, symbol, result, ann_time, session)
        send_msg(TELEGRAM_CHAT_ID, msg)
        print(f"  ✅ SENT: {company} | {score}/10 | {sentiment}")
        sent += 1
        time.sleep(6)

    save_seen()
    print(f"Done. Sent: {sent}")


def check_oi_auto():
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Checking OI...")
    session = get_nse_session()
    try:
        resp = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            timeout=15
        )
        stocks = resp.json().get("data", []) if resp.status_code == 200 else []
        sig = [s for s in stocks if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
        if not sig:
            print("No significant OI spikes")
            return
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now(IST).strftime('%H:%M IST')}\n\n"
        for s in sig[:6]:
            oi = s.get("oiChange") or s.get("perOIChange") or 0
            ltp = s.get("lastPrice") or s.get("ltp") or 0
            sym = s.get("symbol", "?")
            e = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
            msg += f"{e} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"
        send_msg(TELEGRAM_CHAT_ID, msg)
        print("✅ OI alert sent")
    except Exception as e:
        print(f"OI error: {e}")


def run_scheduler():
    schedule.every(6).minutes.do(check_announcements)
    schedule.every(30).minutes.do(check_oi_auto)
    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    print("🚀 Starting Market Intelligence Bot...")
    load_seen()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"✅ Webhook server on port {port}")
    send_msg(TELEGRAM_CHAT_ID,
        "✅ *Market Intelligence Bot LIVE*\n\n"
        "📡 NSE announcements — every 6 min\n"
        "🧠 Smart filter — material events only\n"
        "📈 OI spikes — every 30 min\n"
        "💬 Type /help for all commands"
    )
    threading.Thread(target=check_announcements, daemon=True).start()
    server.serve_forever()
