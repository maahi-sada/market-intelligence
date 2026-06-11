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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import feedparser
import sqlite3
from google import genai
import pytz
import requests
import schedule
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_NAME = "CAPITAL_DECODE"
IST = pytz.timezone("Asia/Kolkata")
DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
SEEN_ANN_FILE = DATA_DIR / "seen_ann.json"
SEEN_NEWS_FILE = DATA_DIR / "seen_news.json"
PURE_ALERT_MODE = os.environ.get("PURE_ALERT_MODE", "1").strip().lower() not in {"0", "false", "no"}
MAX_DAILY_ALERTS = int(os.environ.get("MAX_DAILY_ALERTS", "15"))
ENABLE_BSE_ANNOUNCEMENTS = os.environ.get("ENABLE_BSE_ANNOUNCEMENTS", "1").strip().lower() in {"1", "true", "yes"}
ENABLE_BROKER_NEWS = os.environ.get("ENABLE_BROKER_NEWS", "1").strip().lower() in {"1", "true", "yes"}
ENABLE_OI_AUTO = os.environ.get("ENABLE_OI_AUTO", "0").strip().lower() in {"1", "true", "yes"}
ENABLE_MORNING_BRIEFING = os.environ.get("ENABLE_MORNING_BRIEFING", "1").strip().lower() in {"1", "true", "yes"}
ENABLE_PATTERN_SCAN = os.environ.get("ENABLE_PATTERN_SCAN", "1").strip().lower() in {"1", "true", "yes"}
SEND_STARTUP_ALERT = os.environ.get("SEND_STARTUP_ALERT", "0").strip().lower() in {"1", "true", "yes"}
ALERT_LOGIC_VERSION = os.environ.get("ALERT_LOGIC_VERSION", "capital_decode_v1").strip()

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
            model_name=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite").strip(),
            min_score=int(os.environ.get("MIN_ALERT_SCORE", "82")),
        )


CONFIG = Config.from_env()
DATA_DIR.mkdir(parents=True, exist_ok=True)
gemini_client = genai.Client(api_key=CONFIG.gemini_api_key)


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
                    self.items = [str(item) for item in data[-self.max_items:]]
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
                removed = self.items[:-self.max_items]
                self.items = self.items[-self.max_items:]
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


# =============================================================================
# ACCURACY TRACKER — SQLite based, zero cost
# =============================================================================

TRACKER_DB = DATA_DIR / "accuracy.db"


def init_tracker_db() -> None:
    """Create accuracy tracker table if not exists."""
    try:
        conn = sqlite3.connect(str(TRACKER_DB))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type  TEXT,
                symbol      TEXT,
                company     TEXT,
                category    TEXT,
                sentiment   TEXT,
                score       INTEGER,
                price_alert REAL,
                price_1d    REAL,
                price_3d    REAL,
                move_1d_pct REAL,
                move_3d_pct REAL,
                correct_1d  INTEGER DEFAULT 0,
                correct_3d  INTEGER DEFAULT 0,
                alert_time  TEXT,
                checked_1d  INTEGER DEFAULT 0,
                checked_3d  INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
        logger.info("✅ Accuracy tracker DB ready")
    except Exception as e:
        logger.error("Tracker DB init failed: %s", e)


def track_alert(
    alert_type: str,
    symbol: str,
    company: str,
    category: str,
    sentiment: str,
    score: int,
    price_alert: float,
) -> None:
    """Record a new alert for accuracy tracking."""
    if not symbol or price_alert <= 0:
        return
    try:
        conn = sqlite3.connect(str(TRACKER_DB))
        conn.execute("""
            INSERT INTO alerts
            (alert_type, symbol, company, category, sentiment, score, price_alert, alert_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert_type,
            symbol.upper().strip(),
            company[:100],
            category,
            sentiment,
            score,
            price_alert,
            datetime.now(IST).isoformat(),
        ))
        conn.commit()
        conn.close()
        logger.info("📌 Tracked alert: %s | %s | ₹%.2f", symbol, sentiment, price_alert)
    except Exception as e:
        logger.error("Track alert failed: %s", e)


def fetch_current_price(symbol: str, nse: "NSEClient") -> float:
    """Fetch current price for a symbol via NSE."""
    try:
        info = nse.stock_info(symbol)
        return float(info.get("cmp", 0))
    except Exception:
        return 0.0


def update_alert_prices() -> None:
    """
    Check prices for pending alerts.
    Runs daily at 4:00 PM — updates 1-day and 3-day price moves.
    """
    try:
        conn = sqlite3.connect(str(TRACKER_DB))
        nse = NSEClient()
        now = datetime.now(IST)

        # Get alerts needing 1-day check
        pending_1d = conn.execute("""
            SELECT id, symbol, sentiment, price_alert, alert_time
            FROM alerts
            WHERE checked_1d = 0
            AND datetime(alert_time) <= datetime('now', '-1 day')
        """).fetchall()

        for row in pending_1d:
            alert_id, symbol, sentiment, price_alert, _ = row
            current = fetch_current_price(symbol, nse)
            if current <= 0:
                continue
            move_pct = ((current - price_alert) / price_alert) * 100
            correct = 1 if (
                (sentiment == "BULLISH" and move_pct >= 1.5) or
                (sentiment == "BEARISH" and move_pct <= -1.5)
            ) else 0
            conn.execute("""
                UPDATE alerts
                SET price_1d = ?, move_1d_pct = ?, correct_1d = ?, checked_1d = 1
                WHERE id = ?
            """, (current, round(move_pct, 2), correct, alert_id))
            logger.info("1D check %s: %.2f%% | %s", symbol, move_pct, "✅" if correct else "❌")
            time.sleep(0.5)

        # Get alerts needing 3-day check
        pending_3d = conn.execute("""
            SELECT id, symbol, sentiment, price_alert, alert_time
            FROM alerts
            WHERE checked_3d = 0
            AND datetime(alert_time) <= datetime('now', '-3 day')
        """).fetchall()

        for row in pending_3d:
            alert_id, symbol, sentiment, price_alert, _ = row
            current = fetch_current_price(symbol, nse)
            if current <= 0:
                continue
            move_pct = ((current - price_alert) / price_alert) * 100
            correct = 1 if (
                (sentiment == "BULLISH" and move_pct >= 2.0) or
                (sentiment == "BEARISH" and move_pct <= -2.0)
            ) else 0
            conn.execute("""
                UPDATE alerts
                SET price_3d = ?, move_3d_pct = ?, correct_3d = ?, checked_3d = 1
                WHERE id = ?
            """, (current, round(move_pct, 2), correct, alert_id))
            logger.info("3D check %s: %.2f%% | %s", symbol, move_pct, "✅" if correct else "❌")
            time.sleep(0.5)

        conn.commit()
        conn.close()
        logger.info("✅ Price update complete")

    except Exception as e:
        logger.error("update_alert_prices failed: %s", e)


def weekly_accuracy_report() -> None:
    """
    Send weekly accuracy report every Friday 6 PM IST.
    This is your sales pitch to beta users.
    """
    try:
        conn = sqlite3.connect(str(TRACKER_DB))

        # Last 7 days stats
        week_ago = (datetime.now(IST) - timedelta(days=7)).isoformat()

        total = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ?", (week_ago,)
        ).fetchone()[0]

        if total == 0:
            conn.close()
            logger.info("No alerts to report this week")
            return

        # 1-day accuracy
        checked_1d = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND checked_1d = 1", (week_ago,)
        ).fetchone()[0]
        correct_1d = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND correct_1d = 1", (week_ago,)
        ).fetchone()[0]

        # 3-day accuracy
        checked_3d = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND checked_3d = 1", (week_ago,)
        ).fetchone()[0]
        correct_3d = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND correct_3d = 1", (week_ago,)
        ).fetchone()[0]

        # Best call — highest 3d move in correct direction
        best = conn.execute("""
            SELECT symbol, sentiment, move_3d_pct, category
            FROM alerts
            WHERE alert_time >= ? AND correct_3d = 1
            ORDER BY ABS(move_3d_pct) DESC LIMIT 1
        """, (week_ago,)).fetchone()

        # Worst call
        worst = conn.execute("""
            SELECT symbol, sentiment, move_3d_pct, category
            FROM alerts
            WHERE alert_time >= ? AND checked_3d = 1 AND correct_3d = 0
            ORDER BY ABS(move_3d_pct) DESC LIMIT 1
        """, (week_ago,)).fetchone()

        # Average move (checked 3d)
        avg_move = conn.execute("""
            SELECT AVG(ABS(move_3d_pct))
            FROM alerts
            WHERE alert_time >= ? AND checked_3d = 1
        """, (week_ago,)).fetchone()[0] or 0

        # Breakdown by category
        categories = conn.execute("""
            SELECT category, COUNT(*) as cnt
            FROM alerts
            WHERE alert_time >= ?
            GROUP BY category
            ORDER BY cnt DESC LIMIT 5
        """, (week_ago,)).fetchall()

        # Bullish vs Bearish
        bullish_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND sentiment = 'BULLISH'", (week_ago,)
        ).fetchone()[0]
        bearish_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_time >= ? AND sentiment = 'BEARISH'", (week_ago,)
        ).fetchone()[0]

        conn.close()

        # Calculate accuracy rates
        acc_1d = f"{(correct_1d/checked_1d*100):.0f}%" if checked_1d > 0 else "Pending"
        acc_3d = f"{(correct_3d/checked_3d*100):.0f}%" if checked_3d > 0 else "Pending"

        week_str = datetime.now(IST).strftime("Week ending %d %b %Y")

        lines = [
            "<b>📊 CAPITAL DECODE — WEEKLY ACCURACY REPORT</b>",
            f"<b>{week_str}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"<b>Alerts sent this week: {total}</b>",
            f"🟢 Bullish: {bullish_count} | 🔴 Bearish: {bearish_count}",
            "",
            "<b>Accuracy:</b>",
            f"Next day (1.5%+ move): <b>{acc_1d}</b> ({correct_1d}/{checked_1d} checked)",
            f"3-day (2%+ move): <b>{acc_3d}</b> ({correct_3d}/{checked_3d} checked)",
            f"Avg price move (3d): <b>{avg_move:.1f}%</b>",
        ]

        if best:
            sym, sent, move, cat = best
            arrow = "📈" if sent == "BULLISH" else "📉"
            lines.extend([
                "",
                f"<b>Best call:</b> {arrow} {esc(sym)} {esc(cat)} → {move:+.1f}% in 3 days",
            ])

        if worst:
            sym, sent, move, cat = worst
            lines.extend([
                f"<b>Missed call:</b> ⚠️ {esc(sym)} {esc(cat)} → {move:+.1f}%",
            ])

        if categories:
            lines.extend(["", "<b>Alert breakdown:</b>"])
            for cat, cnt in categories:
                lines.append(f"• {esc(cat)}: {cnt}")

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "Data tracks alerts sent by Capital Decode.",
            "Past accuracy is not a guarantee of future results.",
        ])

        telegram.send("\n".join(lines))
        logger.info("✅ Weekly accuracy report sent")

    except Exception as e:
        logger.error("weekly_accuracy_report failed: %s", e)


def cmd_accuracy(chat_id: str | int) -> None:
    """Show accuracy stats on demand via /accuracy command."""
    try:
        conn = sqlite3.connect(str(TRACKER_DB))
        total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

        if total == 0:
            telegram.send("<b>📊 Accuracy Tracker</b>\n\nNo alerts tracked yet. Give it a few days.", chat_id)
            conn.close()
            return

        checked_1d = conn.execute("SELECT COUNT(*) FROM alerts WHERE checked_1d = 1").fetchone()[0]
        correct_1d = conn.execute("SELECT COUNT(*) FROM alerts WHERE correct_1d = 1").fetchone()[0]
        checked_3d = conn.execute("SELECT COUNT(*) FROM alerts WHERE checked_3d = 1").fetchone()[0]
        correct_3d = conn.execute("SELECT COUNT(*) FROM alerts WHERE correct_3d = 1").fetchone()[0]

        # Recent 5 alerts
        recent = conn.execute("""
            SELECT symbol, sentiment, category, move_1d_pct, move_3d_pct, correct_3d, alert_time
            FROM alerts
            ORDER BY id DESC LIMIT 5
        """).fetchall()

        conn.close()

        acc_1d = f"{(correct_1d/checked_1d*100):.0f}%" if checked_1d > 0 else "Pending"
        acc_3d = f"{(correct_3d/checked_3d*100):.0f}%" if checked_3d > 0 else "Pending"

        lines = [
            "<b>📊 ACCURACY TRACKER</b>",
            f"Total alerts tracked: <b>{total}</b>",
            "",
            f"1-day accuracy (1.5%+): <b>{acc_1d}</b>",
            f"3-day accuracy (2%+): <b>{acc_3d}</b>",
            "",
            "<b>Recent alerts:</b>",
        ]

        for row in recent:
            sym, sent, cat, m1, m3, corr3, at = row
            icon = "🟢" if sent == "BULLISH" else "🔴"
            m1_str = f"{m1:+.1f}%" if m1 is not None else "pending"
            m3_str = f"{m3:+.1f}%" if m3 is not None else "pending"
            result = "✅" if corr3 else ("❌" if m3 is not None else "⏳")
            lines.append(f"{icon} {esc(sym)} {esc(cat)} | 1D:{m1_str} 3D:{m3_str} {result}")

        telegram.send("\n".join(lines), chat_id)

    except Exception as e:
        telegram.send(f"Accuracy data unavailable: {esc(e)}", chat_id)


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
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/",
            "Origin": "https://www.nseindia.com",
        })
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
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.bseindia.com/corporates/ann.html",
            "Origin": "https://www.bseindia.com",
        })

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
# FILTERS — ZERO JUNK POLICY
# =============================================================================

# Hard drop — never pass to Gemini, zero cost, zero noise
JUNK_SUBJECTS = (
    "trading window",
    "shareholding pattern",
    "newspaper publication",
    "loss of share certificate",
    "duplicate share certificate",
    "voting result",
    "agm notice",
    "egm notice",
    "compliance certificate",
    "transcript",
    "presentation uploaded",
    "closure of trading window",
    "change in address",
    "book closure",
    "change in registrar",
    "intimation of board meeting",
    "loss of certificate",
    "change in auditor",
    "postal ballot",
    "corporate governance",
    "insider trading window",
    "media interview",
    "reg. 57",
    "reg. 74",
    "reg. 76",
    "reg. 30",
    "regulation 30",
    "change in directors",
    "appointment of director",
    "cessation of director",
    "annual report",
    "outcome of agm",
    "outcome of egm",
    "record date",
    "general update",
    "investor presentation",
    "concall",
    "conference call",
    "analyst meet",
)

# Must pass — these get classified by Gemini
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
    "sebi order",
    "usfda",
    "fda approval",
    "fda",
    "order receipt",
    "order win",
    "work order",
    "project award",
    "export order",
    "letter of intent",
    "loi",
    "credit rating",
    "qip",
    "preferential allotment",
    "fundraise",
    "capacity expansion",
    "new plant",
    "capex",
    "joint venture",
    "partnership",
    "nclt",
    "default",
    "restructuring",
    "demerger",
    "disinvestment",
    "stake sale",
    "contract",
)

# Only fetch PDF for these — saves cost
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
    "goldman sachs",
    "morgan stanley",
    "jefferies",
    "nomura",
    "clsa",
    "macquarie",
    "ubs",
    "citigroup",
    "jp morgan",
    "upgrade",
    "downgrade",
    "block deal",
    "bulk deal",
    "msci rebalance",
    "index inclusion",
    "index exclusion",
    "financial result",
    "order win",
    "acquisition",
    "merger",
    "buyback",
    "open offer",
    "delisting",
    "fraud",
    "sebi",
    "usfda",
    "fda approval",
    "credit rating",
    "qip",
    "fundraise",
    "capex",
    "nclt",
    "default",
    "insolvency",
)

NEWS_FEEDS = (
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/results.xml",
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets",
)

# F&O universe for pattern scanner
FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "TITAN", "WIPRO", "ULTRACEMCO", "NESTLEIND", "TECHM",
    "SUNPHARMA", "POWERGRID", "NTPC", "ONGC", "COALINDIA",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL",
    "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
    "DIVISLAB", "DRREDDY", "CIPLA", "APOLLOHOSP", "MAXHEALTH",
    "ADANIENT", "ADANIPORTS", "ADANIGREEN", "ADANIPOWER",
    "GRASIM", "INDUSINDBK", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
    "M&M", "TATACONSUM", "BRITANNIA", "DABUR", "GODREJCP",
    "PIDILITIND", "HAVELLS", "VOLTAS", "DLF", "GODREJPROP",
    "ZOMATO", "NYKAA", "IRCTC", "INDIGO", "BANKBARODA",
    "PNB", "CANBK", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK",
    "CHOLAFIN", "MUTHOOTFIN", "LICHSGFIN", "SIEMENS", "ABB",
    "BHEL", "BEL", "HAL", "TATAPOWER", "TORNTPOWER",
    "DEEPAKNTR", "IGL", "JUBLFOOD", "ZYDUSLIFE", "BIOCON",
    "AUROPHARMA", "LUPIN", "TORNTPHARM", "PERSISTENT", "MPHASIS",
    "LTIM", "COFORGE", "KPITTECH", "BALKRISIND", "APOLLOTYRE",
    "MRF", "MOTHERSON", "BOSCHLTD", "BHARATFORG", "UPL",
    "PIIND", "SAIL", "NMDC", "GLENMARK", "ALKEM",
    "POWERINDIA",
]


def should_process_announcement(subject: str) -> bool:
    value = subject.lower()
    # Hard drop first — no API call
    if any(term in value for term in JUNK_SUBJECTS):
        return False
    # Must be explicitly important
    if any(term in value for term in IMPORTANT_SUBJECTS):
        return True
    # Default deny — if not whitelisted, skip
    return False


# =============================================================================
# GEMINI CLASSIFIER — COST-OPTIMISED
# =============================================================================


class GeminiClassifier:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.client = gemini_client

    def classify_announcement(self, company: str, subject: str, pdf_text: str = "") -> dict[str, Any] | None:
        body = f"Subject: {subject}\n\nPDF Content:\n{pdf_text}" if pdf_text else f"Subject: {subject}"
        prompt = f"""
You are an institutional-grade Indian stock market event filter with a zero-junk policy.

STRICT RULES:
- Return null for: SME/penny/suspended companies, scores below 82, NEUTRAL sentiment
- Return null for: routine updates, generic board meetings without outcomes, minor management changes
- Only alert for events that MOVE THE STOCK materially the same day or next session

Classify sentiment:
- BULLISH: strong results beat, large order win, dividend/bonus/buyback, M&A at premium, FDA approval, capacity expansion with clear ROI
- BEARISH: results miss, order cancellation, key promoter exit, downgrade, fraud/SEBI/NCLT/default
- NEUTRAL: return null — do not send neutral alerts

Focus on: Nifty 500, F&O stocks, large caps, mid caps with institutional ownership.
Ignore: SME board, micro caps under 500 Cr mcap, generic regulatory compliance.

Return exactly null when not material or score below 82.
For material filings return only raw JSON:
{{
  "score": 82,
  "tier": "EXTREME or HIGH or MEDIUM",
  "category": "RESULTS or ORDER or PROMOTER or CORPORATE_ACTION or MA or FUNDRAISE or REGULATORY or PHARMA or MANAGEMENT or CREDIT or CAPACITY or MONTHLY_UPDATE or OTHER",
  "sentiment": "BULLISH or BEARISH",
  "actionability": "ALERT_IMMEDIATELY or WATCHLIST",
  "institutional_interest": "HIGH or MEDIUM or LOW",
  "summary": "one specific line with numbers — include % growth, order size, rating change",
  "why_it_matters": "direct earnings/growth/balance sheet/valuation impact",
  "expected_impact": "Medium or High or Extreme",
  "estimated_earnings_impact": "quantified if possible, else NA",
  "market_reaction": "expected direction with reason",
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
You are an institutional Indian stock market news filter with a zero-junk policy.

STRICT RULES:
- Return null for generic commentary, opinion pieces, tips, crypto, global macro without direct India equity impact
- Return null for NEUTRAL sentiment — only BULLISH or BEARISH
- Only alert for: major orders, results surprises, M&A, corporate actions, FDA events, broker rating changes with targets,
  block/bulk deals above 50 Cr, index inclusion/exclusion, major regulatory policy, earnings estimate revisions

Return exactly null if not worth immediate action.
For material news return only raw JSON:
{{
  "score": 82,
  "category": "ORDER or RESULTS or MA or CORPORATE_ACTION or REGULATORY or BROKER_RATING or BLOCK_DEAL or INDEX_CHANGE or POLICY or SECTOR or OTHER",
  "sentiment": "BULLISH or BEARISH",
  "broker": null,
  "rating": null,
  "target_price": 0,
  "upside_pct": 0,
  "company_name": null,
  "ticker": null,
  "summary": "one line with key numbers",
  "market_reaction": "why the stock moves and direction"
}}

{body}
""".strip()
        return self._generate_json(prompt)

    def _generate_json(self, prompt: str) -> dict[str, Any] | None:
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                text = (response.text or "").strip()
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
# PATTERN SCANNER — MODULES 1-5 INTEGRATED
# =============================================================================

# In-memory OHLCV store — refreshed at 3:45 PM daily
scanner_data: dict = {}


def _nse_ticker(symbol: str) -> str:
    special = {"M&M": "M%26M.NS", "BAJAJ-AUTO": "BAJAJ-AUTO.NS"}
    return special.get(symbol, f"{symbol}.NS")


def fetch_ohlcv_all() -> dict:
    """Module 1: Fetch 60-day OHLCV for all F&O stocks via yfinance."""
    try:
        import yfinance as yf
        import pandas as pd

        end = datetime.today()
        start = end - timedelta(days=65)
        results = {}
        failed = []

        logger.info("📊 OHLCV fetch starting for %s F&O stocks...", len(FNO_SYMBOLS))

        for symbol in FNO_SYMBOLS:
            try:
                ticker = _nse_ticker(symbol)
                df = yf.download(
                    ticker,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                    actions=False,
                )
                if df.empty or len(df) < 10:
                    failed.append(symbol)
                    continue

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.sort_index(inplace=True)

                # Derived columns for pattern detection
                df["Body"] = abs(df["Close"] - df["Open"])
                df["Range"] = df["High"] - df["Low"]
                df["UpperWick"] = df["High"] - df[["Open", "Close"]].max(axis=1)
                df["LowerWick"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
                df["Bullish"] = df["Close"] > df["Open"]
                df["VolMA20"] = df["Volume"].rolling(20).mean()
                df["VolRatio"] = df["Volume"] / df["VolMA20"]
                df["Returns"] = df["Close"].pct_change()

                results[symbol] = df
                time.sleep(0.08)

            except Exception as e:
                failed.append(symbol)
                logger.debug("OHLCV failed %s: %s", symbol, e)

        logger.info("✅ OHLCV done: %s fetched, %s failed", len(results), len(failed))
        return results

    except ImportError:
        logger.error("yfinance not installed. Add 'yfinance' to requirements.txt")
        return {}
    except Exception as e:
        logger.error("fetch_ohlcv_all failed: %s", e)
        return {}


def detect_patterns(data: dict) -> list[dict]:
    """
    Module 2: Detect 5 high-reliability patterns.
    Returns list of pattern signals sorted by confidence score.
    """
    import pandas as pd

    signals = []

    for symbol, df in data.items():
        if len(df) < 10:
            continue

        try:
            last = df.iloc[-1]
            prev = df.iloc[-2]
            prev2 = df.iloc[-3] if len(df) >= 3 else prev

            score = 0
            patterns_found = []
            direction = None

            close = float(last["Close"])
            high = float(last["High"])
            low = float(last["Low"])
            body = float(last["Body"])
            rng = float(last["Range"])
            vol_ratio = float(last["VolRatio"]) if not pd.isna(last["VolRatio"]) else 1.0
            is_bull = bool(last["Bullish"])

            # ── Pattern 1: Bullish / Bearish Engulfing ──
            prev_body = float(prev["Body"])
            prev_bull = bool(prev["Bullish"])
            if (
                not prev_bull and is_bull
                and body > prev_body * 1.1
                and float(last["Open"]) <= float(prev["Close"])
                and float(last["Close"]) >= float(prev["Open"])
            ):
                patterns_found.append("Bullish Engulfing")
                score += 2
                direction = "BULLISH"

            elif (
                prev_bull and not is_bull
                and body > prev_body * 1.1
                and float(last["Open"]) >= float(prev["Close"])
                and float(last["Close"]) <= float(prev["Open"])
            ):
                patterns_found.append("Bearish Engulfing")
                score += 2
                direction = "BEARISH"

            # ── Pattern 2: Inside Bar (NR4/NR7) ──
            mother_high = float(prev["High"])
            mother_low = float(prev["Low"])
            if high <= mother_high and low >= mother_low:
                patterns_found.append("Inside Bar")
                score += 1
                # Direction from mother candle
                if direction is None:
                    direction = "BULLISH" if prev_bull else "BEARISH"

            # ── Pattern 3: Morning Star / Evening Star ──
            if len(df) >= 3:
                d1 = df.iloc[-3]
                d2 = df.iloc[-2]
                d3 = df.iloc[-1]
                d1_body = float(d1["Body"])
                d2_body = float(d2["Body"])
                d3_body = float(d3["Body"])

                # Morning star: big red → tiny → big green
                if (
                    not bool(d1["Bullish"])
                    and d1_body > df["Body"].rolling(10).mean().iloc[-3] * 0.8
                    and d2_body < d1_body * 0.35
                    and bool(d3["Bullish"])
                    and d3_body > d1_body * 0.5
                ):
                    patterns_found.append("Morning Star")
                    score += 2
                    direction = "BULLISH"

                # Evening star: big green → tiny → big red
                elif (
                    bool(d1["Bullish"])
                    and d1_body > df["Body"].rolling(10).mean().iloc[-3] * 0.8
                    and d2_body < d1_body * 0.35
                    and not bool(d3["Bullish"])
                    and d3_body > d1_body * 0.5
                ):
                    patterns_found.append("Evening Star")
                    score += 2
                    direction = "BEARISH"

            # ── Pattern 4: Bull Flag / Bear Flag ──
            if len(df) >= 8:
                pole_start = df.iloc[-8]["Close"]
                pole_end = df.iloc[-5]["Close"]
                pole_move = (float(pole_end) - float(pole_start)) / float(pole_start)

                if abs(pole_move) > 0.04:  # 4%+ pole move
                    flag_high = df.iloc[-5:-1]["High"].max()
                    flag_low = df.iloc[-5:-1]["Low"].min()
                    flag_range = (float(flag_high) - float(flag_low)) / float(pole_end)

                    if flag_range < 0.025:  # tight consolidation
                        if pole_move > 0 and close > float(flag_high):
                            patterns_found.append("Bull Flag Breakout")
                            score += 2
                            direction = "BULLISH"
                        elif pole_move < 0 and close < float(flag_low):
                            patterns_found.append("Bear Flag Breakdown")
                            score += 2
                            direction = "BEARISH"

            # ── Volume confirmation ──
            if vol_ratio > 1.5:
                score += 1

            # ── Support/Resistance proximity ──
            recent_high = df.iloc[-20:]["High"].max()
            recent_low = df.iloc[-20:]["Low"].min()
            near_resistance = abs(close - float(recent_high)) / float(recent_high) < 0.012
            near_support = abs(close - float(recent_low)) / float(recent_low) < 0.012

            if direction == "BULLISH" and near_support:
                score += 1
            elif direction == "BEARISH" and near_resistance:
                score += 1

            # Only keep score 3+ with at least one pattern
            if score >= 3 and patterns_found and direction:
                signals.append({
                    "symbol": symbol,
                    "direction": direction,
                    "patterns": patterns_found,
                    "score": score,
                    "close": round(close, 2),
                    "vol_ratio": round(vol_ratio, 2),
                    "near_support": near_support,
                    "near_resistance": near_resistance,
                })

        except Exception as e:
            logger.debug("Pattern detection failed %s: %s", symbol, e)
            continue

    # Sort by score descending
    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def fetch_oi_data() -> dict:
    """Module 3: Fetch OI data from NSE for F&O stocks."""
    try:
        nse = NSEClient()
        oi_spurts = nse.oi_spurts()
        oi_map = {}
        for item in oi_spurts:
            sym = str(item.get("symbol", "")).strip()
            if sym:
                oi_map[sym] = {
                    "oi_change_pct": number(item.get("oiChange") or item.get("perOIChange")),
                    "ltp": number(item.get("lastPrice") or item.get("ltp")),
                }
        logger.info("OI data fetched: %s symbols", len(oi_map))
        return oi_map
    except Exception as e:
        logger.error("OI fetch failed: %s", e)
        return {}


def score_and_rank(signals: list[dict], oi_data: dict) -> list[dict]:
    """
    Module 4: Add OI confirmation layer and final ranking.
    OI confirmation = double weight signal.
    """
    ranked = []

    for sig in signals:
        sym = sig["symbol"]
        final_score = sig["score"]
        oi_confirmed = False
        oi_change = 0.0

        if sym in oi_data:
            oi_change = oi_data[sym]["oi_change_pct"]
            # OI confirms direction
            if sig["direction"] == "BULLISH" and oi_change > 10:
                final_score += 2
                oi_confirmed = True
            elif sig["direction"] == "BEARISH" and oi_change < -10:
                final_score += 2
                oi_confirmed = True

        sig["final_score"] = final_score
        sig["oi_confirmed"] = oi_confirmed
        sig["oi_change_pct"] = round(oi_change, 1)
        ranked.append(sig)

    ranked.sort(key=lambda x: x["final_score"], reverse=True)
    return ranked


def format_pattern_alert(signals: list[dict]) -> str:
    """Module 5: Format pattern scan results for Telegram."""
    if not signals:
        return ""

    bullish = [s for s in signals if s["direction"] == "BULLISH"]
    bearish = [s for s in signals if s["direction"] == "BEARISH"]

    lines = [
        "<b>📊 CAPITAL DECODE — PATTERN SCAN</b>",
        f"<b>{now_ist()}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if bullish:
        lines.append("")
        lines.append("<b>🟢 BULLISH SETUPS</b>")
        for s in bullish[:6]:
            oi_tag = " ✅OI" if s["oi_confirmed"] else ""
            vol_tag = f" | Vol {s['vol_ratio']}x" if s["vol_ratio"] > 1.5 else ""
            support_tag = " | At Support" if s["near_support"] else ""
            lines.append(
                f"<b>{esc(s['symbol'])}</b> — {esc(', '.join(s['patterns']))}{oi_tag}"
            )
            lines.append(
                f"  CMP: ₹{s['close']:,.2f} | Score: {s['final_score']}{vol_tag}{support_tag}"
            )

    if bearish:
        lines.append("")
        lines.append("<b>🔴 BEARISH SETUPS</b>")
        for s in bearish[:4]:
            oi_tag = " ✅OI" if s["oi_confirmed"] else ""
            vol_tag = f" | Vol {s['vol_ratio']}x" if s["vol_ratio"] > 1.5 else ""
            resist_tag = " | At Resistance" if s["near_resistance"] else ""
            lines.append(
                f"<b>{esc(s['symbol'])}</b> — {esc(', '.join(s['patterns']))}{oi_tag}"
            )
            lines.append(
                f"  CMP: ₹{s['close']:,.2f} | Score: {s['final_score']}{vol_tag}{resist_tag}"
            )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total setups: {len(bullish)} Bullish | {len(bearish)} Bearish",
        "✅OI = OI confirmed setup (higher conviction)",
        "",
        "<b>Risk note:</b> Pattern + OI is context, not guarantee. Use your own entry/SL.",
    ])

    return "\n".join(lines)


def run_pattern_scan() -> None:
    """Run full pattern scan pipeline and send results to Telegram."""
    global scanner_data
    if not ENABLE_PATTERN_SCAN:
        return

    logger.info("🔍 Pattern scan starting...")
    try:
        # Step 1: Fetch OHLCV
        scanner_data = fetch_ohlcv_all()
        if not scanner_data:
            logger.warning("Pattern scan: no OHLCV data fetched")
            return

        # Step 2: Detect patterns
        signals = detect_patterns(scanner_data)
        logger.info("Patterns found: %s raw signals", len(signals))

        if not signals:
            logger.info("No patterns above threshold today")
            return

        # Step 3: OI confirmation
        oi_data = fetch_oi_data()

        # Step 4: Score and rank
        ranked = score_and_rank(signals, oi_data)

        # Step 5: Send to Telegram
        msg = format_pattern_alert(ranked)
        if msg:
            telegram.send(msg)
            logger.info("✅ Pattern scan sent: %s setups", len(ranked))

    except Exception as exc:
        logger.error("Pattern scan failed: %s", exc)


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
    return f"{label} — {ratio:.0f}% of market cap"


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

    sentiment_icon = "🟢" if sentiment == "BULLISH" else "🔴"

    lines = [
        f"<b>{sentiment_icon} {esc(source)} FILING | SCORE {score}/100 | {esc(sentiment)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>{esc(company)}</b>{' | <code>' + esc(symbol) + '</code>' if symbol else ''}",
        f"<b>{esc(category)}</b> | Impact: <b>{esc(result.get('expected_impact', 'Medium'))}</b> | {esc(result.get('actionability', 'WATCHLIST'))}",
    ]

    if cmp_value > 0:
        lines.append(f"CMP: ₹{cmp_value:,.2f} | MCap: ₹{market_cap:,.0f} Cr | F&O: {esc(stock.get('fno', '?'))}")

    lines.extend([
        f"Inst. interest: <b>{esc(result.get('institutional_interest', 'LOW'))}</b>",
        "",
        f"<b>Summary:</b> {esc(result.get('summary', ''))}",
    ])

    if category == "CORPORATE_ACTION":
        if result.get("dividend_amount"):
            lines.append(f"Dividend: {esc(result.get('dividend_amount'))}")
        if result.get("dividend_exdate"):
            lines.append(f"Ex-date: {esc(result.get('dividend_exdate'))}")
        if number(result.get("buyback_price")) > 0:
            lines.append(f"Buyback price: ₹{number(result.get('buyback_price')):,.2f}")
        if number(result.get("buyback_size_cr")) > 0:
            lines.append(f"Buyback size: ₹{number(result.get('buyback_size_cr')):,.0f} Cr")
        if number(result.get("buyback_premium_pct")) > 0:
            lines.append(f"Buyback premium: {number(result.get('buyback_premium_pct')):.1f}%")

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
            lines.append(f"Order value: ₹{order_value:,.0f} Cr")
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

    lines.extend([
        "",
        "<b>⚠️ Risk note:</b> Not a trade signal. Confirm with your setup. Max daily loss: ₹5,000.",
        f"Time: {esc(ann_time)}",
    ])
    return "\n".join(lines)


def format_news_alert(result: dict[str, Any], link: str = "") -> str:
    sentiment = result.get("sentiment", "NEUTRAL")
    sentiment_icon = "🟢" if sentiment == "BULLISH" else "🔴"

    lines = [
        f"<b>{sentiment_icon} MARKET NEWS | SCORE {int(number(result.get('score'))):d}/100 | {esc(sentiment)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
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
            lines.append(f"{label}: ₹{number(value):,.0f}")
        elif key == "upside_pct" and number(value) != 0:
            lines.append(f"{label}: {number(value):+.1f}%")
        elif value and str(value).lower() != "null":
            lines.append(f"{label}: {esc(value)}")

    lines.extend([
        "",
        f"<b>Summary:</b> {esc(result.get('summary', ''))}",
        "",
        f"<b>Market reaction:</b> {esc(result.get('market_reaction', ''))}",
        "",
        "<b>⚠️ Risk note:</b> Not a trade signal. Confirm with your setup. Max daily loss: ₹5,000.",
        f"Time: {now_ist()}",
    ])
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
            f"{ALERT_LOGIC_VERSION}|NSE|{symbol}|{pdf_path or subject}|{ann_time}".encode()
        ).hexdigest()

        if not subject or seen_ann.contains(ann_id):
            continue
        seen_ann.add(ann_id)

        if not should_process_announcement(subject):
            logger.info("JUNK | %s", subject[:80])
            continue

        stock = nse.stock_info(symbol)

        # Hard filters — no Gemini call if fails
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
        logger.info("SENT NSE alert | %s | %s/100", company, score)
        # Track for accuracy
        track_alert("NSE", symbol, company, result.get("category","OTHER"), result.get("sentiment",""), score, number(nse.stock_info(symbol).get("cmp",0)))
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
            f"{ALERT_LOGIC_VERSION}|BSE|{symbol}|{attachment or subject}|{ann_time}".encode()
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
        logger.info("SENT BSE alert | %s | %s/100", company, score)
        # Track for accuracy
        track_alert("BSE", symbol, company, result.get("category","OTHER"), result.get("sentiment",""), score, number(nse.stock_info(symbol).get("cmp",0)))
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
                news_id = hashlib.sha256(
                    f"{ALERT_LOGIC_VERSION}|NEWS|{title[:120].lower()}".encode()
                ).hexdigest()

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
                # Track for accuracy
                ticker = str(result.get("ticker") or "").strip()
                if ticker:
                    track_alert("NEWS", ticker, str(result.get("company_name",""))[:100], result.get("category","OTHER"), result.get("sentiment",""), score, 0.0)
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
            stock for stock in stocks
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

    lines = [
        "<b>⚡ OI BUILDUP ALERT</b>",
        f"Time: {now_ist()}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for stock in significant[:8]:
        oi = number(stock.get("oiChange") or stock.get("perOIChange"))
        ltp = number(stock.get("lastPrice") or stock.get("ltp"))
        lines.append(f"<b>{esc(stock.get('symbol', '?'))}</b> — OI +{oi:.1f}% | ₹{ltp:,.2f}")

    lines.extend(["", "Data: NSE live F&O"])
    telegram.send("\n".join(lines), chat_id)


def morning_briefing() -> None:
    logger.info("Sending morning briefing")
    nse = NSEClient()
    earnings_count = 0
    ban_count = 0

    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        earnings_count = len([
            event for event in nse.earnings_events()
            if "Financial Results" in str(event.get("purpose", ""))
            and today in str(event.get("date", ""))
        ])
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

    telegram.send("\n".join([
        "<b>🌅 CAPITAL DECODE — MORNING BRIEFING</b>",
        datetime.now(IST).strftime("%A, %d %b %Y"),
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 Results today: <b>{earnings_count}</b>",
        f"🚫 F&O ban list: <b>{ban_count}</b>",
        "",
        "Auto alerts active:",
        "• NSE/BSE filings — every 10 min",
        "• Broker ratings/news — every 15 min",
        "• Pattern scan — 3:45 PM daily",
        "",
        "Type /earnings | /ban | /oi | /nifty",
    ]))


def market_survival_protocol() -> None:
    logger.info("Sending market survival protocol")
    telegram.send("\n".join([
        "<b>🛡️ MARKET SURVIVAL PROTOCOL</b>",
        "<b>Capital Decode — Read before market open</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "First job: survival, not prediction.",
        "",
        "<b>Rules today:</b>",
        "1. Max daily loss: ₹5,000. Stop immediately.",
        "2. No revenge trading, averaging losers, or doubling quantity.",
        "3. Close all positions by 3:00 PM. No overnight hope.",
        "4. Debt zero mission first. Protect capital before chasing returns.",
        "5. Daily target ₹10,000 is a guideline. No forced trades.",
        "6. Stop loss hit means exit. Small loss is business expense.",
        "7. Position size and risk fixed before entry. Not after.",
        "8. Process over prediction. News does not guarantee direction.",
        "",
        "One ₹50,000 loss needs 10 good days to recover.",
        "One ₹5,000 controlled loss keeps you alive tomorrow.",
        "",
        "<b>CAPITAL FIRST. RISK SECOND. PROFIT THIRD.</b>",
    ]))


# =============================================================================
# TELEGRAM COMMANDS
# =============================================================================


def cmd_nifty(chat_id: str | int) -> None:
    nse = NSEClient()
    try:
        targets = {"NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX", "NIFTY MIDCAP 100"}
        lines = [f"<b>📈 LIVE MARKET</b>", f"Time: {now_ist()}", ""]
        for item in nse.indices():
            if item.get("index") in targets:
                last = number(item.get("last"))
                pct = number(item.get("percentChange"))
                sign = "+" if pct >= 0 else ""
                icon = "🟢" if pct >= 0 else "🔴"
                lines.append(f"{icon} <b>{esc(item.get('index'))}</b>: ₹{last:,.2f} ({sign}{pct:.2f}%)")
        telegram.send("\n".join(lines), chat_id)
    except Exception as exc:
        telegram.send(f"Error fetching live market: {esc(exc)}", chat_id)


def cmd_holiday(chat_id: str | int) -> None:
    nse = NSEClient()
    try:
        today = datetime.now(IST).date()
        month = today.strftime("%b").upper()
        month_holidays = [h for h in nse.holidays() if month in str(h.get("tradingDate", "")).upper()]
        lines = [f"<b>📅 MARKET HOLIDAYS — {today.strftime('%B %Y')}</b>", ""]
        if month_holidays:
            for holiday in month_holidays:
                lines.append(f"{esc(holiday.get('tradingDate', ''))} — {esc(holiday.get('description', ''))}")
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
            event for event in nse.earnings_events()
            if "Financial Results" in str(event.get("purpose", ""))
            and today_fmt in str(event.get("date", ""))
        ]
        lines = [f"<b>📊 TODAY'S EARNINGS</b>", datetime.now(IST).strftime("%d %b %Y"), ""]
        if results:
            lines.append(f"{len(results)} companies reporting:")
            lines.append("")
            for item in results[:30]:
                lines.append(f"<b>{esc(item.get('symbol', '?'))}</b> — {esc(item.get('companyName', ''))}")
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
        lines = [f"<b>🚫 F&O BAN LIST</b>", datetime.now(IST).strftime("%d %b %Y"), ""]
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


def cmd_scan(chat_id: str | int) -> None:
    """Manual pattern scan trigger via /scan command."""
    telegram.send("<b>🔍 Running pattern scan...</b>\nThis takes 2-3 minutes. Results coming shortly.", chat_id)
    threading.Thread(target=run_pattern_scan, daemon=True).start()


def cmd_help(chat_id: str | int) -> None:
    telegram.send("\n".join([
        "<b>📡 Capital Decode — Market Intelligence</b>",
        "",
        "<b>Commands:</b>",
        "/nifty — Live indices",
        "/holiday — Market holidays",
        "/earnings — Today's results calendar",
        "/ban — F&O ban list",
        "/oi — OI buildup report",
        "/scan — Run pattern scanner now",
        "/accuracy — Alert accuracy stats",
        "/help — This menu",
        "",
        "<b>Auto alerts:</b>",
        "• NSE filings — every 10 min",
        f"• BSE filings — {'every 10 min' if ENABLE_BSE_ANNOUNCEMENTS else 'OFF'}",
        f"• Broker ratings/news — {'every 15 min' if ENABLE_BROKER_NEWS else 'OFF'}",
        f"• OI auto report — {'every 30 min' if ENABLE_OI_AUTO else 'OFF'}",
        f"• Morning briefing — {'8:30 AM IST' if ENABLE_MORNING_BRIEFING else 'OFF'}",
        f"• Pattern scan — {'3:45 PM IST' if ENABLE_PATTERN_SCAN else 'OFF'}",
        "• Survival protocol — 9:00 AM IST",
        "",
        f"Model: {CONFIG.model_name}",
        f"Min alert score: {CONFIG.min_score}/100",
        f"Max alerts/day: {MAX_DAILY_ALERTS if MAX_DAILY_ALERTS > 0 else 'unlimited'}",
    ]), chat_id)


COMMANDS: dict[str, Callable[[str | int], None]] = {
    "/nifty": cmd_nifty,
    "/holiday": cmd_holiday,
    "/earnings": cmd_earnings,
    "/ban": cmd_ban,
    "/oi": lambda chat_id: check_oi_auto(chat_id=chat_id, threshold=15),
    "/scan": cmd_scan,
    "/accuracy": cmd_accuracy,
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
        self.wfile.write(b"Capital Decode - ACTIVE")

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
    if ENABLE_PATTERN_SCAN:
        schedule.every().day.at("15:45", "Asia/Kolkata").do(run_pattern_scan)
    # Accuracy tracker — price check daily + weekly report
    schedule.every().day.at("16:00", "Asia/Kolkata").do(update_alert_prices)
    schedule.every().friday.at("18:00", "Asia/Kolkata").do(weekly_accuracy_report)

    while not stop_event.is_set():
        schedule.run_pending()
        stop_event.wait(10)


def main() -> None:
    logger.info("Capital Decode starting")
    if not CONFIG.telegram_token or not CONFIG.telegram_chat_id:
        logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing; alerts will not be delivered.")

    seen_ann.load()
    seen_news.load()
    init_tracker_db()

    stop_event = threading.Event()
    scheduler_thread = threading.Thread(target=run_scheduler, args=(stop_event,), daemon=True)
    scheduler_thread.start()

    if SEND_STARTUP_ALERT:
        telegram.send("\n".join([
            "<b>📡 Capital Decode LIVE</b>",
            "",
            "• NSE filings — every 10 min",
            f"• BSE filings — {'ON' if ENABLE_BSE_ANNOUNCEMENTS else 'OFF'}",
            f"• Broker ratings/news — {'ON' if ENABLE_BROKER_NEWS else 'OFF'}",
            f"• OI auto alerts — {'ON' if ENABLE_OI_AUTO else 'OFF'}",
            f"• Morning briefing — {'ON' if ENABLE_MORNING_BRIEFING else 'OFF'}",
            f"• Pattern scan — {'3:45 PM daily' if ENABLE_PATTERN_SCAN else 'OFF'}",
            "• Survival protocol — 9:00 AM",
            "",
            f"Model: {CONFIG.model_name}",
            f"Min score: {CONFIG.min_score}/100",
            "",
            "Type /help for commands.",
        ]))

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
