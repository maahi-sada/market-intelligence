import requests
import google.generativeai as genai
import asyncio
import schedule
import time
import json
import os
from datetime import datetime
from telegram import Bot

# ── Config ─────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY")

# ── Setup ───────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model     = genai.GenerativeModel("gemini-2.5-flash")
bot       = Bot(token=TELEGRAM_TOKEN)
seen_ann  = set()
seen_oi   = set()

# ════════════════════════════════════════════════
# NSE SESSION — shared across all fetches
# ════════════════════════════════════════════════
def get_nse_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    })
    try:
        session.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
        print("✅ NSE session established")
    except Exception as e:
        print(f"⚠️ NSE session warning: {e}")
    return session

# ════════════════════════════════════════════════
# PHASE 1 — ANNOUNCEMENTS
# ════════════════════════════════════════════════
def fetch_announcements(session):
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ NSE announcements: {len(data)} items")
            return data
        print(f"⚠️ Announcements status: {resp.status_code}")
    except Exception as e:
        print(f"❌ Announcements error: {e}")
    return []

def classify(company, subject):
    prompt = f"""You are a senior Indian stock market analyst.
Analyze this NSE corporate announcement.
Respond ONLY in raw JSON. No markdown. No extra text.

{{"category":"RESULTS or DIVIDEND or BUYBACK or BOARD_MEETING or MERGER or CONCALL or GUIDANCE or OTHER","impact":"HIGH or MEDIUM or LOW","score":<1-10>,"summary":"<2 sentences what this means for stock price>"}}

Company: {company}
Announcement: {subject}

Scoring: 8-10=strong mover, 5-7=moderate, 1-4=routine"""
    try:
        r = model.generate_content(prompt)
        text = r.text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        print(f"❌ Gemini classify error: {e}")
        return None

async def send_announcement_alert(company, result, ann_time):
    ie = {"HIGH":"🔴","MEDIUM":"🟡","LOW":"🟢"}.get(result["impact"],"⚪")
    ce = {"RESULTS":"📊","DIVIDEND":"💰","BUYBACK":"🔄","BOARD_MEETING":"📋",
          "MERGER":"🤝","CONCALL":"📞","GUIDANCE":"🎯","OTHER":"📌"}.get(result["category"],"📌")
    msg = (
        f"{ie} *{result['impact']} IMPACT ALERT*\n\n"
        f"🏢 *Company:* {company}\n"
        f"{ce} *Category:* {result['category']}\n"
        f"⚡ *Score:* {result['score']}/10\n"
        f"📝 *Summary:* {result['summary']}\n"
        f"🕐 *Time:* {ann_time}\n"
        f"🔗 *Exchange:* NSE"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

def check_announcements():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ── Checking announcements...")
    session = get_nse_session()
    anns    = fetch_announcements(session)
    sent    = 0

    for ann in anns[:15]:
        ann_id  = (ann.get("an_dt","") + ann.get("symbol","") + ann.get("desc","")[:20])
        if ann_id in seen_ann:
            continue
        seen_ann.add(ann_id)

        company  = ann.get("sm_name", ann.get("symbol","Unknown"))
        subject  = ann.get("desc","")
        ann_time = ann.get("an_dt", datetime.now().strftime("%Y-%m-%d %H:%M"))

        print(f"  🔍 {company} — {subject[:60]}")
        result = classify(company, subject)
        if not result:
            continue

        if result["score"] >= 4:
            asyncio.run(send_announcement_alert(company, result, ann_time))
            print(f"  ✅ Alert sent: {company} | {result['category']} | {result['score']}/10")
            sent += 1
        else:
            print(f"  ⏭ Low score ({result['score']}): {company}")
        time.sleep(1)

    print(f"  Done. Sent: {sent}")

# ════════════════════════════════════════════════
# PHASE 2 — OI SPIKE DETECTION
# ════════════════════════════════════════════════
def fetch_oi_spikes(session):
    url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            stocks = data.get("data", [])
            print(f"✅ OI spurts: {len(stocks)} stocks")
            return stocks
        print(f"⚠️ OI API status: {resp.status_code}")
    except Exception as e:
        print(f"❌ OI fetch error: {e}")
    return []

def fetch_oi_gainers_losers(session):
    results = {}
    urls = {
        "long_buildup":  "https://www.nseindia.com/api/live-analysis-variations?index=gainers&limit=10",
        "short_buildup": "https://www.nseindia.com/api/live-analysis-variations?index=loosers&limit=10",
    }
    for key, url in urls.items():
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                results[key] = (data.get("data") or [])[:5]
                print(f"✅ {key}: {len(results[key])} stocks")
        except Exception as e:
            print(f"❌ {key} error: {e}")
            results[key] = []
    return results

def analyze_oi_with_ai(spikes, gainers_losers):
    top = spikes[:5]
    stocks_text = "\n".join([
        f"- {s.get('symbol','?')}: OI change +{s.get('oiChange') or s.get('perOIChange',0):.1f}%, LTP ₹{s.get('lastPrice') or s.get('ltp',0)}"
        for s in top
    ])
    long_text = "\n".join([
        f"- {s.get('symbol','?')}: price up, OI up (long buildup)"
        for s in gainers_losers.get("long_buildup", [])
    ])
    short_text = "\n".join([
        f"- {s.get('symbol','?')}: price down, OI up (short buildup)"
        for s in gainers_losers.get("short_buildup", [])
    ])

    prompt = f"""You are a senior F&O trader and market analyst in India.
Analyze this real-time NSE options data and give a sharp, actionable summary.
Respond ONLY in raw JSON. No markdown.

{{
  "market_mood": "BULLISH or BEARISH or NEUTRAL or MIXED",
  "top_pick": "<single best stock to watch with reason>",
  "key_insight": "<1-2 sentences — what this OI data signals for today>",
  "watch_list": ["<symbol1>", "<symbol2>", "<symbol3>"]
}}

OI SPURTS (unusual buildup):
{stocks_text}

LONG BUILDUP (price up + OI up):
{long_text}

SHORT BUILDUP (price down + OI up):
{short_text}

Be direct. Think like a prop desk trader."""

    try:
        r = model.generate_content(prompt)
        text = r.text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        print(f"❌ OI AI analysis error: {e}")
        return None

async def send_oi_alert(spikes, gainers_losers, ai_analysis):
    now = datetime.now().strftime("%H:%M IST")

    # Mood emoji
    mood_emoji = {"BULLISH":"🟢","BEARISH":"🔴","NEUTRAL":"⚪","MIXED":"🟡"}.get(
        ai_analysis.get("market_mood","MIXED") if ai_analysis else "MIXED", "⚪"
    )

    msg = f"📈 *OI INTELLIGENCE REPORT*\n"
    msg += f"🕐 {now}\n\n"

    # AI summary
    if ai_analysis:
        msg += f"{mood_emoji} *Market mood:* {ai_analysis.get('market_mood','?')}\n"
        msg += f"💡 *Key insight:* {ai_analysis.get('key_insight','')}\n"
        msg += f"🎯 *Top pick:* {ai_analysis.get('top_pick','')}\n\n"

    # OI spikes table
    msg += "*Top OI spurts:*\n"
    for i, s in enumerate(spikes[:6]):
        oi   = s.get("oiChange") or s.get("perOIChange") or 0
        ltp  = s.get("lastPrice") or s.get("ltp") or 0
        sym  = s.get("symbol","?")
        sig  = "🔴" if oi > 50 else "🟡" if oi > 25 else "🟢"
        msg += f"{sig} *{sym}* — OI +{oi:.1f}% | ₹{ltp}\n"

    # Long/short buildup
    long_b  = gainers_losers.get("long_buildup",[])
    short_b = gainers_losers.get("short_buildup",[])

    if long_b:
        msg += f"\n📗 *Long buildup:* "
        msg += " | ".join([s.get("symbol","?") for s in long_b[:4]])

    if short_b:
        msg += f"\n📕 *Short buildup:* "
        msg += " | ".join([s.get("symbol","?") for s in short_b[:4]])

    if ai_analysis and ai_analysis.get("watch_list"):
        msg += f"\n\n👀 *Watch list:* {', '.join(ai_analysis['watch_list'])}"

    msg += "\n\n_Data: NSE live F&O_"

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="Markdown"
    )

def check_oi():
    # Only run during market hours
    now  = datetime.now()
    hour = now.hour + (5.5/1)  # rough IST
    # Skip if before 9:15 AM or after 3:30 PM IST
    ist_hour = (now.hour + 5) % 24 + (1 if now.minute >= 30 else 0)
    print(f"\n[{now.strftime('%H:%M:%S')}] ── Checking OI spikes...")

    session        = get_nse_session()
    spikes         = fetch_oi_spikes(session)
    gainers_losers = fetch_oi_gainers_losers(session)

    if not spikes:
        print("  No OI data — market may be closed or NSE slow")
        return

    # Only alert if significant spikes exist
    significant = [s for s in spikes if (s.get("oiChange") or s.get("perOIChange") or 0) > 15]
    if not significant:
        print(f"  No significant spikes (all < 15%). Top: {spikes[0].get('symbol')} {spikes[0].get('oiChange')}%")
        return

    print(f"  🔍 Analyzing {len(significant)} significant OI spikes with AI...")
    ai_analysis = analyze_oi_with_ai(significant, gainers_losers)

    asyncio.run(send_oi_alert(significant, gainers_losers, ai_analysis))
    print(f"  ✅ OI intelligence report sent")

# ════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════
async def startup():
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            "✅ *Market Intelligence System LIVE*\n\n"
            "📡 *Phase 1:* NSE announcements — every 3 min\n"
            "📈 *Phase 2:* OI spike intelligence — every 30 min\n"
            "🤖 AI: Gemini Flash\n"
            "🔗 Source: NSE Direct (real-time)\n"
            "⚡ Min alert score: 4/10"
        ),
        parse_mode="Markdown"
    )
    print("✅ Startup message sent to Telegram")

# ════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════
if __name__ == "__main__":
    print("🚀 Market Intelligence System starting...")
    asyncio.run(startup())

    # Run both immediately on start
    check_announcements()
    check_oi()

    # Phase 1 — every 3 minutes
    schedule.every(3).minutes.do(check_announcements)

    # Phase 2 — every 30 minutes
    schedule.every(30).minutes.do(check_oi)

    print("\n📡 Scheduler active. Running...")
    while True:
        schedule.run_pending()
        time.sleep(10)
