import requests
import schedule
import time
import json
import os
import re
import threading
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq

TELEGRAM_TOKEN = "7712276746:AAE6x8jevrOHNW2L4EhjNdDC6h3e_ii8vOI"
TELEGRAM_CHAT_ID = "787902453"
GROQ_API_KEY = "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"

client = Groq(api_key=GROQ_API_KEY)
seen_ann = set()


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
    """Fetch PDF from NSE and extract text"""
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

        return text[:3000]
    except Exception as e:
        print(f"PDF error: {e}")
        return ""


def get_company_marketcap(session, symbol):
    """Get company marketcap from NSE"""
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            mc = data.get("industryInfo", {}).get("macroEconomicIndicators", {})
            # Try market cap from different fields
            info = data.get("metadata", {})
            price = info.get("lastPrice", 0)
            shares = data.get("securityInfo", {}).get("issuedSize", 0)
            if price and shares:
                marketcap_cr = (price * shares) / 1e7
                return marketcap_cr
    except:
        pass
    return 0


def order_context(order_value_cr, marketcap_cr):
    """Give context to order size vs marketcap"""
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
    """Classify announcement and extract smart details"""
    combined = f"Subject: {subject}\n\nPDF Content: {pdf_text[:800]}" if pdf_text else f"Subject: {subject}"

    prompt = f"""You are an institutional stock market alert system used by hedge funds and Bloomberg terminals.

Your job is EXTREMELY strict filtering. 95% of announcements must be ignored.

IGNORE EVERYTHING EXCEPT THESE 3 TIERS:

EXTREME (score 8-10) — stock moves 5-20%:
- Quarterly/annual results with surprise (beat or miss >10%)
- Merger, acquisition, takeover bid, demerger
- SEBI action, fraud, forensic audit, auditor resignation
- Insolvency, NCLT filing, debt default
- USFDA approval or import alert or warning letter
- Promoter selling stake >2% in open market

HIGH (score 5-7) — stock moves 3-5%:
- Major order win or cancellation above 100 crore
- Buyback, bonus, stock split announcement
- QIP or fundraise with pricing details
- CEO or CFO resignation or sudden management change
- Credit rating downgrade or upgrade
- Block deal above 1% of total equity
- Debt restructuring

MEDIUM (score 3-4) — stock moves 1-3%:
- Dividend announcement with amount
- Promoter pledge increase above 5%
- Pharma patent win or loss
- Plant shutdown, fire, major operational disruption
- Large capex or capacity expansion above 200 crore
- JV or partnership with a marquee company

STRICTLY IGNORE — return null for ALL of these:
- AGM / EGM notices
- Board meeting intimation
- Shareholding pattern filing
- Newspaper publication notice
- Compliance certificate
- Trading window closure
- Analyst meet or conference call schedule
- Transcript or presentation upload
- CSR or ESG update
- Loss of share certificate
- Voting results
- Routine tax or payment disclosure
- Exchange clarification
- Website update
- Appointment of non-executive directors only
- Any announcement not in the 3 tiers above

EXTRACTION RULES — extract these precisely:
- DIVIDEND: extract exact amount per share in Rs, ex-date, record date
- MANAGEMENT: extract person name, designation (CEO/CFO/MD/Director), whether resignation or appointment, reason if mentioned
- ORDER: extract exact order value in crores, client name if mentioned, project type
- RESULTS: extract revenue, profit, YoY growth %
- All other types: extract all numbers, percentages, amounts mentioned

If not in EXTREME, HIGH, or MEDIUM tiers — return exactly: null

If in one of the 3 tiers — return ONLY raw JSON no markdown:
{{"tier":"EXTREME or HIGH or MEDIUM","category":"RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or OTHER","sentiment":"BULLISH or BEARISH or NEUTRAL","score":<3-10>,"summary":"<one line what happened with specific details>","market_reaction":"<one line why market will react>","dividend_amount":"<Rs X per share or null>","dividend_exdate":"<DD-Mon-YYYY or null>","person_name":"<name or null>","person_designation":"<CEO/CFO/MD/Director or null>","person_action":"<resigned/appointed or null>","person_reason":"<reason or null>","order_value_cr":<number in crores or 0>,"key_figures":"<all numbers amounts percentages>"}}

Company: {company}
{combined}"""

    try:
        r = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.1
        )
        text = r.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        if text.lower() == "null" or text == "":
            return None
        return json.loads(text)
    except Exception as e:
        print(f"Groq error: {e}")
        return None


def format_alert(company, symbol, result, ann_time, session):
    """Format alert with smart context"""
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

    # Base message
    msg = (
        f"{ie} *{te} ALERT* | {se} {sentiment}\n"
        f"{'─' * 30}\n"
        f"🏢 *{company}*\n"
        f"{ce} *{category}* | ⚡ *{score}/10*\n\n"
        f"📝 *{result.get('summary', '')}*\n\n"
    )

    # Smart details by category
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
        if reason and reason != "null":
            msg += f"💬 *Reason:* {reason}\n"
        msg += "\n"

    elif category == "ORDER":
        order_cr = result.get("order_value_cr", 0)
        if order_cr and order_cr > 0:
            # Get marketcap for context
            mc = get_company_marketcap(session, symbol) if symbol else 0
            ctx = order_context(order_cr, mc) if mc > 0 else ""
            msg += f"📦 *Order Value:* ₹{order_cr:,.0f} Cr\n"
            if mc > 0:
                msg += f"📊 *Marketcap:* ₹{mc:,.0f} Cr\n"
            if ctx:
                msg += f"🎯 *Size Context:* {ctx}\n"
            msg += "\n"

    # Key figures for all
    kf = result.get("key_figures", "")
    if kf and kf not in ["null", "None", "N/A", ""]:
        msg += f"🔢 *Key figures:* {kf}\n\n"

    msg += f"💡 *{result.get('market_reaction', '')}*\n\n"
    msg += f"🕐 {ann_time} | 🔗 NSE"

    return msg


def handle_nifty(chat_id):
    session = get_nse_session()
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            targets = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX"]
            msg = f"📊 *LIVE INDICES*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
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
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
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


def check_announcements():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking announcements...")
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
    for ann in anns[:20]:
        ann_id = ann.get("symbol", "") + ann.get("desc", "")[:30]
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)
        company = ann.get("sm_name", ann.get("symbol", "Unknown"))
        symbol = ann.get("symbol", "")
        subject = ann.get("desc", "")
        ann_time = ann.get("an_dt", datetime.now().strftime("%Y-%m-%d %H:%M"))
        pdf_path = ann.get("attchmntFile", "")

        print(f"  RAW | {subject[:80]}")
# Skip obvious junk without calling Groq
        junk_keywords = ["newspaper", "trading window", "shareholding", "loss of share",
                         "voting result", "agm", "egm", "compliance", "website", "investor meet",
                         "con. call", "transcript", "presentation uploaded"]
        if any(k in subject.lower() for k in junk_keywords):
            print(f"  JUNK SKIP | {subject[:50]}")
            continue

        # Fetch PDF if available
        pdf_text = ""
        if pdf_path:
            print(f"  Fetching PDF...")
            pdf_text = fetch_pdf_text(session, pdf_path)
            if pdf_text:
                print(f"  PDF extracted: {len(pdf_text)} chars")
            else:
                print(f"  PDF empty/failed")

        result = classify(company, subject, pdf_text)
        if not result:
            print(f"  SKIP | {company[:40]}")
            continue

        score = result.get("score", 0)
        print(f"  {score}/10 | {result.get('sentiment','?')} | {company[:35]}")
        if score < 3:
            continue

        msg = format_alert(company, symbol, result, ann_time, session)
        send_msg(TELEGRAM_CHAT_ID, msg)
        print(f"  ✅ Alert sent: {company} | {score}/10")
        sent += 1
        time.sleep(3)

    print(f"Done. Sent: {sent}")


def check_oi_auto():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking OI...")
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
        msg = f"📈 *OI SPIKE ALERT*\n🕐 {datetime.now().strftime('%H:%M IST')}\n\n"
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
