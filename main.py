import html
import hashlib
import io
import json
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import feedparser
import google.generativeai as genai
import pytz
import requests
import schedule
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_NAME = "MARKET_BOT"
IST = pytz.timezone("Asia/Kolkata")
DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
SEEN_ANN_FILE = DATA_DIR / "seen_ann.json"
SEEN_NEWS_FILE = DATA_DIR / "seen_news.json"
PURE_ALERT_MODE = os.environ.get("PURE_ALERT_MODE", "1").strip().lower() not in {"0", "false", "no"}
MAX_DAILY_ALERTS = int(os.environ.get("MAX_DAILY_ALERTS", "10"))
ENABLE_BSE_ANNOUNCEMENTS = os.environ.get("ENABLE_BSE_ANNOUNCEMENTS", "1").strip().lower() in {"1", "true", "yes"}
ENABLE_BROKER_NEWS = os.environ.get("ENABLE_BROKER_NEWS", "1").strip().lower() in {"1", "true", "yes"}
ENABLE_OI_AUTO = os.environ.get("ENABLE_OI_AUTO", "0").strip().lower() in {"1", "true", "yes"}
ENABLE_MORNING_BRIEFING = os.environ.get("ENABLE_MORNING_BRIEFING", "0").strip().lower() in {"1", "true", "yes"}
SEND_STARTUP_ALERT = os.environ.get("SEND_STARTUP_ALERT", "0").strip().lower() in {"1", "true", "yes"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(APP_NAME)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    gemini_api_key: str
    port: int
    webhook_secret: str
    model_name: str
    min_score: int

    @classmethod
    def from_env(cls) -> "Config":
        gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_api_key:
            raise ValueError("Set GEMINI_API_KEY in your deployment environment.")

        return cls(
            telegram_token=os.environ.get("TELEGRAM_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            gemini_api_key=gemini_api_key,
            port=int(os.environ.get("PORT", "8080")),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
            model_name=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            min_score=int(os.environ.get("MIN_ALERT_SCORE", "80")),
        )


CONFIG = Config.from_env()
DATA_DIR.mkdir(parents=True, exist_ok=True)

genai.configure(api_key=CONFIG.gemini_api_key)


def now_ist(fmt: str = "%d %b %Y %H:%M IST") -> str:
    return datetime.now(IST).strftime(fmt)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def number(value: Any, default: float = 0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


# =============================================================================
# PERSISTENT DEDUPE
# =============================================================================


class SeenStore:
    def __init__(self, path: Path, max_items: int = 3000) -> None:
        self.path = path
        self.max_items = max_items
        self.items: list[str] = []
        self.index: set[str] = set()
        self.lock = threading.Lock()

    def load(self) -> None:
        with self.lock:
            try:
                if self.path.exists():
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self.items = [str(item) for item in data[-self.max_items :]]
                    self.index = set(self.items)
                logger.info("Loaded %s seen ids from %s", len(self.items), self.path.name)
            except Exception as exc:
                logger.error("Could not load %s: %s", self.path.name, exc)
                self.items = []
                self.index = set()

    def contains(self, item: str) -> bool:
        with self.lock:
            return item in self.index

    def add(self, item: str) -> None:
        with self.lock:
            if item in self.index:
                return
            self.items.append(item)
            self.index.add(item)
            if len(self.items) > self.max_items:
                removed = self.items[: -self.max_items]
                self.items = self.items[-self.max_items :]
                self.index.difference_update(removed)

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
            except Exception as exc:
                logger.error("Could not save %s: %s", self.path.name, exc)


seen_ann = SeenStore(SEEN_ANN_FILE)
seen_news = SeenStore(SEEN_NEWS_FILE)
daily_alert_lock = threading.Lock()
daily_alert_date = ""
daily_alert_count = 0


def reserve_daily_alert_slot() -> bool:
    global daily_alert_count, daily_alert_date

    if MAX_DAILY_ALERTS <= 0:
        return True

    today = datetime.now(IST).strftime("%Y-%m-%d")
    with daily_alert_lock:
        if daily_alert_date != today:
            daily_alert_date = today
            daily_alert_count = 0
        if daily_alert_count >= MAX_DAILY_ALERTS:
            logger.info("Daily alert cap reached: %s", MAX_DAILY_ALERTS)
            return False
        daily_alert_count += 1
        return True


def is_pure_alert(result: dict[str, Any]) -> bool:
    if int(number(result.get("score"))) < CONFIG.min_score:
        return False
    if str(result.get("sentiment", "")).upper() == "NEUTRAL":
        return False
    return True


# =============================================================================
# HTTP / TELEGRAM
# =============================================================================


def requests_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class Telegram:
    def __init__(self, token: str, default_chat_id: str) -> None:
        self.token = token
        self.default_chat_id = default_chat_id
        self.session = requests_session()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def send(self, text: str, chat_id: str | int | None = None) -> None:
        target = str(chat_id or self.default_chat_id).strip()
        if not self.enabled or not target:
            logger.warning("Telegram is not configured; message skipped.")
            return

        chunks = self._chunks(text, limit=3900)
        for chunk in chunks:
            try:
                resp = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={
                        "chat_id": target,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.error("Telegram error %s: %s", resp.status_code, resp.text[:250])
            except Exception as exc:
                logger.error("Telegram send failed: %s", exc)

    @staticmethod
    def _chunks(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        parts: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > limit:
                parts.append(current)
                current = line
            else:
                current += line
        if current:
            parts.append(current)
        return parts


telegram = Telegram(CONFIG.telegram_token, CONFIG.telegram_chat_id)


# =============================================================================
# NSE CLIENT
# =============================================================================


class NSEClient:
    def __init__(self) -> None:
        self.session = requests_session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.nseindia.com/",
                "Origin": "https://www.nseindia.com",
            }
        )
        try:
            self.session.get("https://www.nseindia.com", timeout=12)
            time.sleep(0.7)
        except Exception:
            logger.debug("NSE warmup request failed", exc_info=True)

    def get_json(self, url: str, timeout: int = 15) -> Any:
        resp = self.session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def announcements(self) -> list[dict[str, Any]]:
        data = self.get_json("https://www.nseindia.com/api/corporate-announcements?index=equities")
        return data if isinstance(data, list) else []

    def stock_info(self, symbol: str) -> dict[str, Any]:
        if not symbol:
            return {}
        try:
            data = self.get_json(f"https://www.nseindia.com/api/quote-equity?symbol={symbol}", timeout=10)
            meta = data.get("metadata", {})
            sec = data.get("securityInfo", {})
            price = number(meta.get("lastPrice"))
            shares = number(sec.get("issuedSize"))
            market_cap_cr = round((price * shares) / 1e7, 0) if price and shares else 0
            return {
                "cmp": price,
                "mcap": market_cap_cr,
                "fno": "Yes" if sec.get("isFNOSec") else "No",
            }
        except Exception as exc:
            logger.debug("Stock info failed for %s: %s", symbol, exc)
            return {}

    def pdf_text(self, pdf_path: str, max_pages: int = 4, max_chars: int = 3000) -> str:
        if not pdf_path:
            return ""
        try:
            import pypdf

            url = f"https://www.nseindia.com{pdf_path}" if pdf_path.startswith("/") else pdf_path
            resp = self.session.get(url, timeout=25)
            resp.raise_for_status()
            reader = pypdf.PdfReader(io.BytesIO(resp.content))
            text = "".join((page.extract_text() or "") for page in reader.pages[:max_pages])
            return text[:max_chars]
        except Exception as exc:
            logger.error("PDF fetch/extract failed: %s", exc)
            return ""

    def indices(self) -> list[dict[str, Any]]:
        data = self.get_json("https://www.nseindia.com/api/allIndices")
        return data.get("data", []) if isinstance(data, dict) else []

    def holidays(self) -> list[dict[str, Any]]:
        data = self.get_json("https://www.nseindia.com/api/holiday-master?type=trading")
        return data.get("CM", []) if isinstance(data, dict) else []

    def earnings_events(self) -> list[dict[str, Any]]:
        data = self.get_json("https://www.nseindia.com/api/event-calendar?index=equities")
        return data if isinstance(data, list) else []

    def oi_spurts(self) -> list[dict[str, Any]]:
        data = self.get_json("https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings")
        return data.get("data", []) if isinstance(data, dict) else []


# =============================================================================
# BSE CLIENT
# =============================================================================


class BSEClient:
    def __init__(self) -> None:
        self.session = requests_session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.bseindia.com/corporates/ann.html",
                "Origin": "https://www.bseindia.com",
            }
        )

    def get_json(self, url: str, params: dict[str, str] | None = None, timeout: int = 15) -> Any:
        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def announcements(self) -> list[dict[str, Any]]:
        today = datetime.now(IST).strftime("%d/%m/%Y")
        data = self.get_json(
            "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w",
            params={
                "strCat": "-1",
                "strPrevDate": today,
                "strScrip": "",
                "strSearch": "P",
                "strToDate": today,
                "strType": "C",
            },
        )
        rows = data.get("Table", []) if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []

    def pdf_text(self, attachment: str, max_pages: int = 4, max_chars: int = 3000) -> str:
        if not attachment:
            return ""
        try:
            import pypdf

            url = attachment
            if not url.startswith(("http://", "https://")):
                url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
            resp = self.session.get(url, timeout=25)
            resp.raise_for_status()
            reader = pypdf.PdfReader(io.BytesIO(resp.content))
            text = "".join((page.extract_text() or "") for page in reader.pages[:max_pages])
            return text[:max_chars]
        except Exception as exc:
            logger.error("BSE PDF fetch/extract failed: %s", exc)
            return ""


# =============================================================================
# FILTERS AND CLASSIFIERS
# =============================================================================


JUNK_SUBJECTS = (
    "trading window",
    "shareholding pattern",
    "newspaper",
    "loss of share",
    "duplicate share",
    "voting result",
    "agm notice",
    "egm notice",
    "compliance certificate",
    "transcript",
    "presentation uploaded",
    "closure of trading",
    "change in address",
    "book closure",
    "change in registrar",
    "intimation of board meeting",
    "loss of certificate",
    "change in auditor address",
    "postal ballot",
    "corporate governance",
    "insider trading window",
    "media interview",
    "reg. 57",
    "reg. 74",
    "reg. 76",
)

IMPORTANT_SUBJECTS = (
    "financial result",
    "outcome of board",
    "acquisition",
    "merger",
    "amalgamation",
    "buyback",
    "bonus",
    "dividend",
    "split",
    "rights issue",
    "open offer",
    "delisting",
    "insolvency",
    "fraud",
    "sebi",
    "usfda",
    "fda",
    "order",
    "contract",
    "loi",
    "letter of intent",
    "work order",
    "project award",
    "export order",
    "credit rating",
    "qip",
    "preferential",
    "fundraise",
    "capacity expansion",
    "new plant",
    "capex",
    "joint venture",
    "partnership",
    "nclt",
    "default",
    "restructuring",
)

HIGH_VALUE_PDF = (
    "financial result",
    "order",
    "contract",
    "work order",
    "project award",
    "acquisition",
    "merger",
    "credit rating",
    "capex",
    "capacity",
    "fda",
)

BROKER_KEYWORDS = (
    "goldman",
    "morgan stanley",
    "jefferies",
    "nomura",
    "clsa",
    "macquarie",
    "ubs",
    "citi",
    "jp morgan",
    "upgrade",
    "downgrade",
    "block deal",
    "bulk deal",
    "msci",
    "index inclusion",
    "financial result",
    "results",
    "order",
    "contract",
    "acquisition",
    "merger",
    "buyback",
    "bonus",
    "dividend",
    "rights issue",
    "open offer",
    "delisting",
    "insolvency",
    "fraud",
    "sebi",
    "usfda",
    "fda",
    "credit rating",
    "qip",
    "fundraise",
    "capacity expansion",
    "capex",
    "joint venture",
    "nclt",
    "default",
)

NEWS_FEEDS = (
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets",
)


def should_process_announcement(subject: str) -> bool:
    value = subject.lower()

    if any(term in value for term in JUNK_SUBJECTS):
        return False

    if any(term in value for term in IMPORTANT_SUBJECTS):
        return True

    return False


class GeminiClassifier:
    def __init__(self, model_name: str) -> None:
        self.model = genai.GenerativeModel(model_name)

    def classify_announcement(self, company: str, subject: str, pdf_text: str = "") -> dict[str, Any] | None:
        body = f"Subject: {subject}\n\nPDF Content:\n{pdf_text}" if pdf_text else f"Subject: {subject}"
        prompt = f"""
You are an institutional-grade Indian stock market event filter.

Return null if the filing is routine, low impact, from a tiny/SME/penny/suspended company, or below score 80.
Prioritize Nifty 500, F&O stocks, large caps, mid caps, institutionally owned companies, and clear earnings impact.

Classify sentiment:
- BULLISH: strong results, order win, dividend/bonus/buyback, acquisition, upgrade, FDA approval, capex with clear economics
- BEARISH: weak results, cancellation, resignation of key leader, downgrade, fraud, SEBI/NCLT/default/shutdown
- NEUTRAL: unclear impact or informational update

Always examine PDFs for values, clients, dates, growth %, order size, capex amount, rating change, and management names.

Return exactly null when not material.
For material filings return only raw JSON with these keys:
{{
  "score": 80,
  "tier": "EXTREME or HIGH or MEDIUM",
  "category": "RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or CAPACITY or MONTHLY_UPDATE or OTHER",
  "sentiment": "BULLISH or BEARISH or NEUTRAL",
  "actionability": "ALERT_IMMEDIATELY or WATCHLIST",
  "institutional_interest": "HIGH or MEDIUM or LOW",
  "summary": "one specific line with numbers",
  "why_it_matters": "why this changes earnings, growth, balance sheet, valuation, or risk",
  "expected_impact": "Low or Medium or High or Extreme",
  "estimated_earnings_impact": "quantified if possible, otherwise NA",
  "market_reaction": "expected direction and reason",
  "dividend_amount": null,
  "dividend_exdate": null,
  "buyback_price": 0,
  "buyback_size_cr": 0,
  "buyback_premium_pct": 0,
  "person_name": null,
  "person_designation": null,
  "person_action": null,
  "person_reason": null,
  "order_value_cr": 0,
  "order_client": null,
  "order_type": null,
  "volume_growth_pct": 0,
  "key_figures": "all important numbers"
}}

Company: {company}
{body}
""".strip()
        return self._generate_json(prompt)

    def classify_news(self, headline: str, description: str = "") -> dict[str, Any] | None:
        body = f"Headline: {headline}\n\nDescription: {description[:500]}" if description else f"Headline: {headline}"
        prompt = f"""
You are an institutional Indian stock market news filter.

Alert only for high-impact news: major orders/contracts, results, M&A, corporate actions, FDA/regulatory events,
broker initiations, rating changes, target revisions, earnings estimate changes, block/bulk deals,
index inclusion/exclusion, regulatory policy, and major sector news.
Ignore generic commentary, tips, crypto, and global macro without direct Indian equity impact.

Return exactly null if not worth alerting.
For material news return only raw JSON:
{{
  "score": 80,
  "category": "ORDER or RESULTS or MA or CORPORATE_ACTION or REGULATORY or BROKER_RATING or BLOCK_DEAL or INDEX_CHANGE or POLICY or SECTOR or OTHER",
  "sentiment": "BULLISH or BEARISH or NEUTRAL",
  "broker": null,
  "rating": null,
  "target_price": 0,
  "upside_pct": 0,
  "company_name": null,
  "ticker": null,
  "summary": "one line with key numbers",
  "market_reaction": "why the stock may move"
}}

{body}
""".strip()
        return self._generate_json(prompt)

    def _generate_json(self, prompt: str) -> dict[str, Any] | None:
        for attempt in range(3):
            try:
                response = self.model.generate_content(prompt)
                text = (getattr(response, "text", "") or "").strip()
                if not text or text.lower().startswith("null"):
                    return None
                text = text.replace("```json", "").replace("```", "").strip()
                match = re.search(r"\{.*\}", text, re.DOTALL)
                if not match:
                    return None
                return json.loads(match.group(0))
            except Exception as exc:
                message = str(exc)
                if "429" in message and attempt < 2:
                    wait = 15 * (attempt + 1)
                    logger.info("Gemini rate limited; retrying in %ss", wait)
                    time.sleep(wait)
                    continue
                logger.error("Gemini classification failed: %s", message[:200])
                return None
        return None


classifier = GeminiClassifier(CONFIG.model_name)


# =============================================================================
# FORMATTERS
# =============================================================================


def order_context(order_cr: float, market_cap_cr: float) -> str:
    if market_cap_cr <= 0 or order_cr <= 0:
        return ""
    ratio = (order_cr / market_cap_cr) * 100
    if ratio >= 100:
        label = "MASSIVE"
    elif ratio >= 50:
        label = "HUGE"
    elif ratio >= 20:
        label = "LARGE"
    elif ratio >= 10:
        label = "SIGNIFICANT"
    else:
        label = "ROUTINE"
    return f"{label} - {ratio:.0f}% of market cap"


def format_announcement_alert(
    company: str,
    symbol: str,
    result: dict[str, Any],
    ann_time: str,
    nse: NSEClient,
    source: str = "NSE",
) -> str:
    category = result.get("category", "OTHER")
    sentiment = result.get("sentiment", "NEUTRAL")
    score = int(number(result.get("score")))
    stock = nse.stock_info(symbol)
    cmp_value = number(stock.get("cmp"))
    market_cap = number(stock.get("mcap"))

    lines = [
        f"<b>{esc(source)} FILING | SCORE {score}/100 | {esc(sentiment)}</b>",
        "--------------------------------",
        f"<b>{esc(company)}</b>{' | <code>' + esc(symbol) + '</code>' if symbol else ''}",
        f"<b>{esc(category)}</b> | Impact: <b>{esc(result.get('expected_impact', 'Medium'))}</b> | {esc(result.get('actionability', 'WATCHLIST'))}",
    ]
    if cmp_value > 0:
        lines.append(f"CMP: Rs {cmp_value:,.2f} | MCap: Rs {market_cap:,.0f} Cr | F&O: {esc(stock.get('fno', '?'))}")

    lines.extend(
        [
            f"Inst. interest: <b>{esc(result.get('institutional_interest', 'LOW'))}</b>",
            "",
            f"<b>Summary:</b> {esc(result.get('summary', ''))}",
        ]
    )

    if category == "CORPORATE_ACTION":
        if result.get("dividend_amount"):
            lines.append(f"Dividend: {esc(result.get('dividend_amount'))}")
        if result.get("dividend_exdate"):
            lines.append(f"Ex-date: {esc(result.get('dividend_exdate'))}")
        if number(result.get("buyback_price")) > 0:
            lines.append(f"Buyback price: Rs {number(result.get('buyback_price')):,.2f}")
        if number(result.get("buyback_size_cr")) > 0:
            lines.append(f"Buyback size: Rs {number(result.get('buyback_size_cr')):,.0f} Cr")

    if category == "MANAGEMENT":
        for label, key in (
            ("Person", "person_name"),
            ("Role", "person_designation"),
            ("Action", "person_action"),
            ("Reason", "person_reason"),
        ):
            if result.get(key):
                lines.append(f"{label}: {esc(result.get(key))}")

    if category == "ORDER":
        order_value = number(result.get("order_value_cr"))
        if order_value > 0:
            lines.append(f"Order value: Rs {order_value:,.0f} Cr")
            if result.get("order_client"):
                lines.append(f"Client: {esc(result.get('order_client'))}")
            if result.get("order_type"):
                lines.append(f"Type: {esc(str(result.get('order_type')).upper())}")
            context = order_context(order_value, market_cap)
            if context:
                lines.append(f"Vs market cap: {esc(context)}")

    if category == "MONTHLY_UPDATE" and number(result.get("volume_growth_pct")) != 0:
        lines.append(f"Volume growth: {number(result.get('volume_growth_pct')):+.1f}% YoY")

    for label, key in (
        ("Key figures", "key_figures"),
        ("Why it matters", "why_it_matters"),
        ("Earnings impact", "estimated_earnings_impact"),
        ("Market reaction", "market_reaction"),
    ):
        value = result.get(key)
        if value and str(value).upper() not in {"NA", "N/A", "NULL", "NONE"}:
            lines.extend(["", f"<b>{label}:</b> {esc(value)}"])

    lines.append("")
    lines.append("<b>Risk note:</b> Not a trade signal. Trade only if your setup confirms. Max daily loss: Rs 5,000.")
    lines.append("")
    lines.append(f"Time: {esc(ann_time)}")
    return "\n".join(lines)


def format_news_alert(result: dict[str, Any], link: str = "") -> str:
    lines = [
        f"<b>MARKET NEWS | SCORE {int(number(result.get('score'))):d}/100 | {esc(result.get('sentiment', 'NEUTRAL'))}</b>",
        "--------------------------------",
    ]
    company = result.get("company_name")
    ticker = result.get("ticker")
    if company:
        lines.append(f"<b>{esc(company)}</b>{' | <code>' + esc(ticker) + '</code>' if ticker else ''}")
    lines.append(f"<b>{esc(result.get('category', 'OTHER'))}</b>")

    for label, key in (
        ("Broker", "broker"),
        ("Rating", "rating"),
        ("Target price", "target_price"),
        ("Upside/downside", "upside_pct"),
    ):
        value = result.get(key)
        if key == "target_price" and number(value) > 0:
            lines.append(f"{label}: Rs {number(value):,.0f}")
        elif key == "upside_pct" and number(value) != 0:
            lines.append(f"{label}: {number(value):+.1f}%")
        elif value and str(value).lower() != "null":
            lines.append(f"{label}: {esc(value)}")

    lines.extend(
        [
            "",
            f"<b>Summary:</b> {esc(result.get('summary', ''))}",
            "",
            f"<b>Market reaction:</b> {esc(result.get('market_reaction', ''))}",
            "",
            "<b>Risk note:</b> Not a trade signal. Trade only if your setup confirms. Max daily loss: Rs 5,000.",
            "",
            f"Time: {now_ist()}",
        ]
    )
    if link:
        lines.append(f'<a href="{html.escape(link, quote=True)}">Read source</a>')
    return "\n".join(lines)


# =============================================================================
# BOT TASKS
# =============================================================================


def check_announcements() -> None:
    logger.info("[%s] Checking NSE filings", now_ist("%H:%M IST"))
    nse = NSEClient()
    try:
        announcements = nse.announcements()
    except Exception as exc:
        logger.error("NSE announcements failed: %s", exc)
        return

    logger.info("Fetched %s announcements", len(announcements))
    sent = 0

    for ann in announcements[:25]:
        symbol = str(ann.get("symbol", "")).strip()
        subject = str(ann.get("desc", "")).strip()
        company = str(ann.get("sm_name") or symbol or "Unknown").strip()
        ann_time = str(ann.get("an_dt") or now_ist()).strip()
        pdf_path = str(ann.get("attchmntFile", "")).strip()
        ann_id = hashlib.sha256(
            f"{symbol}|{pdf_path}".encode()
        ).hexdigest()

        if not subject or seen_ann.contains(ann_id):
            continue
        seen_ann.add(ann_id)

        if not should_process_announcement(subject):
            logger.info("JUNK | %s", subject[:80])
            continue

        stock = nse.stock_info(symbol)

        if stock.get("fno") != "Yes":
            continue

        if float(stock.get("mcap", 0)) < 5000:
            continue

        logger.info("CHECK | %s | %s", company[:35], subject[:80])
        pdf_text = ""
        if pdf_path and any(k in subject.lower() for k in HIGH_VALUE_PDF):
            pdf_text = nse.pdf_text(pdf_path)
        result = classifier.classify_announcement(company, subject, pdf_text)
        if not result:
            continue

        score = int(number(result.get("score")))
        if PURE_ALERT_MODE and not is_pure_alert(result):
            logger.info("SKIP PURE GATE %s | %s | %s", score, result.get("sentiment"), company[:35])
            continue
        if score < CONFIG.min_score:
            logger.info("LOW SCORE %s | %s", score, company[:35])
            continue
        if not reserve_daily_alert_slot():
            continue

        telegram.send(format_announcement_alert(company, symbol, result, ann_time, nse))
        logger.info("SENT filing alert | %s | %s/100", company, score)
        sent += 1
        time.sleep(2)

    seen_ann.save()
    logger.info("NSE filings done. Sent: %s", sent)


def check_bse_announcements() -> None:
    logger.info("[%s] Checking BSE filings", now_ist("%H:%M IST"))
    bse = BSEClient()
    nse = NSEClient()
    try:
        announcements = bse.announcements()
    except Exception as exc:
        logger.error("BSE announcements failed: %s", exc)
        return

    logger.info("Fetched %s BSE announcements", len(announcements))
    sent = 0

    for ann in announcements[:30]:
        symbol = str(ann.get("SCRIP_CD") or ann.get("SCRIPCODE") or "").strip()
        subject = str(ann.get("NEWS_SUB") or ann.get("HEADLINE") or "").strip()
        company = str(ann.get("SLONGNAME") or ann.get("COMPANYNAME") or symbol or "Unknown").strip()
        ann_time = str(ann.get("DT_TM") or ann.get("NEWS_DT") or now_ist()).strip()
        attachment = str(ann.get("ATTACHMENTNAME") or ann.get("NSURL") or "").strip()
        ann_id = hashlib.sha256(
            f"BSE|{symbol}|{attachment or subject}|{ann_time}".encode()
        ).hexdigest()

        if not subject or seen_ann.contains(ann_id):
            continue
        seen_ann.add(ann_id)

        if not should_process_announcement(subject):
            logger.info("BSE JUNK | %s", subject[:80])
            continue

        logger.info("BSE CHECK | %s | %s", company[:35], subject[:80])
        pdf_text = ""
        if attachment and any(k in subject.lower() for k in HIGH_VALUE_PDF):
            pdf_text = bse.pdf_text(attachment)
        result = classifier.classify_announcement(company, subject, pdf_text)
        if not result:
            continue

        score = int(number(result.get("score")))
        if PURE_ALERT_MODE and not is_pure_alert(result):
            logger.info("BSE SKIP PURE GATE %s | %s | %s", score, result.get("sentiment"), company[:35])
            continue
        if score < CONFIG.min_score:
            logger.info("BSE LOW SCORE %s | %s", score, company[:35])
            continue
        if not reserve_daily_alert_slot():
            continue

        telegram.send(format_announcement_alert(company, symbol, result, ann_time, nse, source="BSE"))
        logger.info("SENT BSE filing alert | %s | %s/100", company, score)
        sent += 1
        time.sleep(2)

    seen_ann.save()
    logger.info("BSE filings done. Sent: %s", sent)


def check_broker_news() -> None:
    logger.info("[%s] Checking market news", now_ist("%H:%M IST"))
    sent = 0

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = str(entry.get("title", "")).strip()
                link = str(entry.get("link", "")).strip()
                desc = str(entry.get("summary", "")).strip()[:300]
                news_id = title[:120].lower()

                if not title or seen_news.contains(news_id):
                    continue
                if not any(keyword in title.lower() for keyword in BROKER_KEYWORDS):
                    continue

                seen_news.add(news_id)
                logger.info("NEWS CHECK | %s", title[:80])
                result = classifier.classify_news(title, desc)
                if not result:
                    continue

                score = int(number(result.get("score")))
                if PURE_ALERT_MODE:
                    if score < CONFIG.min_score:
                        continue
                    if str(result.get("sentiment", "")).upper() == "NEUTRAL":
                        continue
                if score < CONFIG.min_score:
                    continue
                if not reserve_daily_alert_slot():
                    continue

                telegram.send(format_news_alert(result, link))
                logger.info("SENT news alert | %s/100 | %s", score, title[:80])
                sent += 1
                time.sleep(2)
        except Exception as exc:
            logger.error("Feed failed %s: %s", feed_url, exc)

    seen_news.save()
    logger.info("Market news done. Sent: %s", sent)


def check_oi_auto(chat_id: str | int | None = None, threshold: float = 20) -> None:
    logger.info("[%s] Checking OI", now_ist("%H:%M IST"))
    nse = NSEClient()
    try:
        stocks = nse.oi_spurts()
        significant = [
            stock
            for stock in stocks
            if number(stock.get("oiChange") or stock.get("perOIChange")) > threshold
        ]
    except Exception as exc:
        logger.error("OI check failed: %s", exc)
        return

    if not significant:
        logger.info("No significant OI spikes")
        if chat_id:
            telegram.send("<b>OI Report</b>\nNo significant OI buildups right now.", chat_id)
        return

    lines = [f"<b>OI BUILDUP ALERT</b>", f"Time: {now_ist()}", ""]
    for stock in significant[:8]:
        oi = number(stock.get("oiChange") or stock.get("perOIChange"))
        ltp = number(stock.get("lastPrice") or stock.get("ltp"))
        lines.append(f"<b>{esc(stock.get('symbol', '?'))}</b> - OI +{oi:.1f}% | Rs {ltp:,.2f}")
    lines.append("")
    lines.append("Data: NSE live F&O")
    telegram.send("\n".join(lines), chat_id)


def morning_briefing() -> None:
    logger.info("Sending morning briefing")
    nse = NSEClient()
    earnings_count = 0
    ban_count = 0

    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        earnings_count = len(
            [
                event
                for event in nse.earnings_events()
                if "Financial Results" in str(event.get("purpose", ""))
                and today in str(event.get("date", ""))
            ]
        )
    except Exception as exc:
        logger.error("Morning earnings count failed: %s", exc)

    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            ban_count = len([line for line in resp.text.strip().splitlines()[1:] if line.strip()])
    except Exception as exc:
        logger.error("Morning ban count failed: %s", exc)

    telegram.send(
        "\n".join(
            [
                "<b>GOOD MORNING - Market Briefing</b>",
                datetime.now(IST).strftime("%A, %d %b %Y"),
                "--------------------------------",
                f"Companies reporting results today: <b>{earnings_count}</b>",
                f"F&O ban list stocks: <b>{ban_count}</b>",
                "",
                "Auto alerts:",
                "- NSE filings every 10 min",
                "- Broker ratings/news every 15 min",
                "- OI alerts every 30 min",
                "",
                "Type /earnings for full results list.",
                "Type /ban for banned stocks list.",
            ]
        )
    )


def market_survival_protocol() -> None:
    logger.info("Sending market survival protocol")
    telegram.send(
        "\n".join(
            [
                "<b>MARKET SURVIVAL PROTOCOL</b>",
                "<b>Debt Zero Mission - Read before market open</b>",
                "",
                "First job: survival, not prediction.",
                "",
                "<b>Rules today:</b>",
                "1. Max daily loss: Rs 5,000. Stop immediately.",
                "2. No revenge trading, averaging losers, or doubling quantity.",
                "3. Close all positions by 3:00 PM. No overnight hope.",
                "4. Debt zero first. Protect capital before chasing returns.",
                "5. Daily target Rs 10,000 is only a guideline. No forced trades.",
                "6. Stop loss hit means exit. Small loss is business expense.",
                "7. Position size and risk must be fixed before entry.",
                "8. Process over prediction. News does not guarantee direction.",
                "",
                "One Rs 50,000 loss needs many good days to recover.",
                "One Rs 5,000 controlled loss keeps you alive tomorrow.",
                "",
                "<b>CAPITAL FIRST. RISK SECOND. PROFIT THIRD.</b>",
                "Follow the rules. Ignore the noise.",
            ]
        )
    )


# =============================================================================
# TELEGRAM COMMANDS
# =============================================================================


def cmd_nifty(chat_id: str | int) -> None:
    nse = NSEClient()
    try:
        targets = {"NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX", "NIFTY MIDCAP 100"}
        lines = [f"<b>LIVE MARKET</b>", f"Time: {now_ist()}", ""]
        for item in nse.indices():
            if item.get("index") in targets:
                last = number(item.get("last"))
                pct = number(item.get("percentChange"))
                sign = "+" if pct >= 0 else ""
                lines.append(f"<b>{esc(item.get('index'))}</b>: Rs {last:,.2f} ({sign}{pct:.2f}%)")
        telegram.send("\n".join(lines), chat_id)
    except Exception as exc:
        telegram.send(f"Error fetching live market: {esc(exc)}", chat_id)


def cmd_holiday(chat_id: str | int) -> None:
    nse = NSEClient()
    try:
        today = datetime.now(IST).date()
        month = today.strftime("%b").upper()
        month_holidays = [h for h in nse.holidays() if month in str(h.get("tradingDate", "")).upper()]
        lines = [f"<b>MARKET HOLIDAYS - {today.strftime('%B %Y')}</b>", ""]
        if month_holidays:
            for holiday in month_holidays:
                lines.append(f"{esc(holiday.get('tradingDate', ''))} - {esc(holiday.get('description', ''))}")
        else:
            lines.append("No holidays listed for this month.")
        telegram.send("\n".join(lines), chat_id)
    except Exception as exc:
        telegram.send(f"Error fetching holidays: {esc(exc)}", chat_id)


def cmd_earnings(chat_id: str | int) -> None:
    nse = NSEClient()
    try:
        today_fmt = datetime.now(IST).strftime("%Y-%m-%d")
        results = [
            event
            for event in nse.earnings_events()
            if "Financial Results" in str(event.get("purpose", ""))
            and today_fmt in str(event.get("date", ""))
        ]
        lines = [f"<b>TODAY'S EARNINGS</b>", datetime.now(IST).strftime("%d %b %Y"), ""]
        if results:
            lines.append(f"{len(results)} companies reporting:")
            lines.append("")
            for item in results[:30]:
                lines.append(f"<b>{esc(item.get('symbol', '?'))}</b> - {esc(item.get('companyName', ''))}")
        else:
            lines.append("No companies scheduled to report today.")
        telegram.send("\n".join(lines), chat_id)
    except Exception as exc:
        telegram.send(f"Error fetching earnings: {esc(exc)}", chat_id)


def cmd_ban(chat_id: str | int) -> None:
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        stocks = [line.strip() for line in resp.text.strip().splitlines()[1:] if line.strip()]
        lines = [f"<b>F&O BAN LIST</b>", datetime.now(IST).strftime("%d %b %Y"), ""]
        if stocks:
            lines.append(f"{len(stocks)} stocks in ban period:")
            lines.append("")
            for idx, stock in enumerate(stocks, 1):
                lines.append(f"{idx}. {esc(stock)}")
            lines.append("")
            lines.append("No fresh F&O positions allowed.")
        else:
            lines.append("No stocks in ban period today.")
        telegram.send("\n".join(lines), chat_id)
    except Exception as exc:
        telegram.send(f"Error fetching ban list: {esc(exc)}", chat_id)


def cmd_help(chat_id: str | int) -> None:
    telegram.send(
        "\n".join(
            [
                "<b>Market Intelligence Bot</b>",
                "",
                "/nifty - Live indices",
                "/holiday - Market holidays",
                "/earnings - Today's results calendar",
                "/ban - F&O ban list",
                "/oi - OI buildup report",
                "/help - This menu",
                "",
                "<b>Auto alerts</b>",
                "NSE filings - every 10 min",
                f"BSE filings - {'every 10 min' if ENABLE_BSE_ANNOUNCEMENTS else 'OFF'}",
                f"Broker ratings/news - {'ON every 15 min' if ENABLE_BROKER_NEWS else 'OFF'}",
                f"OI auto report - {'ON every 30 min' if ENABLE_OI_AUTO else 'OFF'}",
                f"Morning briefing - {'8:30 AM IST' if ENABLE_MORNING_BRIEFING else 'OFF'}",
                "Survival protocol - 9:00 AM IST",
                "",
                f"Pure Alert Mode: {'ON' if PURE_ALERT_MODE else 'OFF'}",
                f"Max market alerts/day: {MAX_DAILY_ALERTS if MAX_DAILY_ALERTS > 0 else 'unlimited'}",
                f"Minimum alert score: {CONFIG.min_score}/100",
            ]
        ),
        chat_id,
    )


COMMANDS: dict[str, Callable[[str | int], None]] = {
    "/nifty": cmd_nifty,
    "/holiday": cmd_holiday,
    "/earnings": cmd_earnings,
    "/ban": cmd_ban,
    "/oi": lambda chat_id: check_oi_auto(chat_id=chat_id, threshold=15),
    "/help": cmd_help,
    "/start": cmd_help,
}


# =============================================================================
# WEBHOOK SERVER
# =============================================================================


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Market Intelligence Bot - ACTIVE")

    def do_POST(self) -> None:
        if CONFIG.webhook_secret:
            token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != CONFIG.webhook_secret:
                self.send_response(403)
                self.end_headers()
                return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()
            data = json.loads(body)
            message = data.get("message") or data.get("edited_message") or {}
            text = str(message.get("text", "")).strip()
            chat_id = message.get("chat", {}).get("id")

            if not text or not chat_id:
                return
            command = text.split()[0].split("@")[0].lower()
            logger.info("Telegram command: %s", command)
            handler = COMMANDS.get(command)
            if handler:
                threading.Thread(target=handler, args=(chat_id,), daemon=True).start()
        except Exception as exc:
            logger.error("Webhook error: %s", exc)

    def log_message(self, *_args: Any) -> None:
        return


# =============================================================================
# SCHEDULER / ENTRY POINT
# =============================================================================


def run_scheduler(stop_event: threading.Event) -> None:
    schedule.every(10).minutes.do(check_announcements)
    if ENABLE_BSE_ANNOUNCEMENTS:
        schedule.every(10).minutes.do(check_bse_announcements)
    if ENABLE_BROKER_NEWS:
        schedule.every(15).minutes.do(check_broker_news)
    if ENABLE_OI_AUTO:
        schedule.every(30).minutes.do(check_oi_auto)
    if ENABLE_MORNING_BRIEFING:
        schedule.every().day.at("08:30", "Asia/Kolkata").do(morning_briefing)
    schedule.every().day.at("09:00", "Asia/Kolkata").do(market_survival_protocol)

    while not stop_event.is_set():
        schedule.run_pending()
        stop_event.wait(10)


def main() -> None:
    logger.info("Market Intelligence Bot starting")
    if not CONFIG.telegram_token or not CONFIG.telegram_chat_id:
        logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing; alerts will not be delivered.")

    seen_ann.load()
    seen_news.load()

    stop_event = threading.Event()
    scheduler_thread = threading.Thread(target=run_scheduler, args=(stop_event,), daemon=True)
    scheduler_thread.start()

    if SEND_STARTUP_ALERT:
        telegram.send(
            "\n".join(
                [
                    "<b>Market Intelligence Bot LIVE</b>",
                    "",
                    "NSE filings - every 10 min",
                    f"BSE filings - {'ON every 10 min' if ENABLE_BSE_ANNOUNCEMENTS else 'OFF'}",
                    f"Broker ratings/news - {'ON' if ENABLE_BROKER_NEWS else 'OFF'}",
                    f"OI auto alerts - {'ON' if ENABLE_OI_AUTO else 'OFF'}",
                    f"Morning briefing - {'ON' if ENABLE_MORNING_BRIEFING else 'OFF'}",
                    "Survival protocol - 9:00 AM IST",
                    "",
                    f"Pure Alert Mode: {'ON' if PURE_ALERT_MODE else 'OFF'}",
                    f"Minimum alert score: {CONFIG.min_score}/100",
                    f"Max market alerts/day: {MAX_DAILY_ALERTS if MAX_DAILY_ALERTS > 0 else 'unlimited'}",
                    "",
                    "Type /help for commands.",
                ]
            )
    )

    threading.Thread(target=check_announcements, daemon=True).start()
    if ENABLE_BSE_ANNOUNCEMENTS:
        threading.Thread(target=check_bse_announcements, daemon=True).start()
    if ENABLE_BROKER_NEWS:
        threading.Thread(target=check_broker_news, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.port), WebhookHandler)
    logger.info("Server active on port %s", CONFIG.port)

    def shutdown(_signum: int, _frame: Any) -> None:
        logger.info("Shutdown requested")
        stop_event.set()
        seen_ann.save()
        seen_news.save()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
