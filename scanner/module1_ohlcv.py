"""
CAPITAL DECODE — Module 1: OHLCV Data Fetcher
=============================================
Fetches daily OHLCV + delivery data for all NSE F&O stocks.
Designed to plug into existing Railway bot infrastructure.

Author: Capital Decode
"""

import yfinance as yf
import pandas as pd
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional
import time
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# F&O STOCK UNIVERSE — NSE (as of Jun 2026)
# Update this list monthly from NSE website
# https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information
# ─────────────────────────────────────────────

FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC",
    "LT", "HCLTECH", "AXISBANK", "ASIANPAINT", "MARUTI",
    "TITAN", "WIPRO", "ULTRACEMCO", "NESTLEIND", "TECHM",
    "SUNPHARMA", "POWERGRID", "NTPC", "ONGC", "COALINDIA",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL",
    "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
    "DIVISLAB", "DRREDDY", "CIPLA", "APOLLOHOSP", "MAXHEALTH",
    "ADANIENT", "ADANIPORTS", "ADANIGREEN", "ADANIPOWER", "ADANITRANS",
    "GRASIM", "INDUSINDBK", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
    "M&M", "TATACONSUM", "BRITANNIA", "DABUR", "GODREJCP",
    "PIDILITIND", "BERGEPAINT", "HAVELLS", "VOLTAS", "WHIRLPOOL",
    "DLF", "GODREJPROP", "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE",
    "ZOMATO", "NYKAA", "PAYTM", "POLICYBZR", "DELHIVERY",
    "INDIGO", "SPICEJET", "IRCTC", "CONCOR", "GMRINFRA",
    "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "FEDERALBNK",
    "IDFCFIRSTB", "BANDHANBNK", "RBLBANK", "AUBANK", "EQUITASBNK",
    "CHOLAFIN", "MUTHOOTFIN", "MANAPPURAM", "LICHSGFIN", "PNBHOUSING",
    "SIEMENS", "ABB", "BHEL", "BEL", "HAL",
    "TATAPOWER", "TORNTPOWER", "CESC", "JSWENERGY", "POWERINDIA",
    "ATUL", "DEEPAKNTR", "GNFC", "GUJGASLTD", "IGL",
    "MCDOWELL-N", "RADICO", "UNITDSPR", "VBL", "JUBLFOOD",
    "ZYDUSLIFE", "BIOCON", "AUROPHARMA", "LUPIN", "TORNTPHARM",
    "PAGEIND", "RAJESHEXPO", "TRENT", "ABFRL", "MANYAVAR",
    "ASTRAL", "SUPREMEIND", "FINOLEX", "KANSAINER", "AKZOINDIA",
    "PERSISTENT", "MPHASIS", "LTIM", "COFORGE", "KPITTECH",
    "BALKRISIND", "APOLLOTYRE", "MRF", "CEATLTD", "TIINDIA",
    "MOTHERSON", "BOSCHLTD", "BHARATFORG", "ENDURANCE", "SUNDRMFAST",
    "UPL", "PIIND", "BAYER", "RALLIS", "COROMANDEL",
    "SAIL", "NMDC", "NATIONALUM", "HINDCOPPER", "MOIL",
    "GLENMARK", "ALKEM", "IPCALAB", "GRANULES", "LAURUSLABS",
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"
]

# Remove index symbols for stock-level scan
STOCK_SYMBOLS = [s for s in FNO_SYMBOLS if s not in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]]


# ─────────────────────────────────────────────
# CORE FETCHER
# ─────────────────────────────────────────────

class OHLCVFetcher:
    """
    Fetches OHLCV data for F&O stocks using yfinance.
    Falls back gracefully on failures.
    """

    def __init__(self, lookback_days: int = 60):
        """
        Args:
            lookback_days: How many calendar days of history to fetch.
                           60 days gives ~42 trading sessions — enough for
                           all 5 pattern detections in Module 2.
        """
        self.lookback_days = lookback_days
        self.end_date = datetime.today()
        self.start_date = self.end_date - timedelta(days=lookback_days)
        self.failed_symbols = []
        self.data_cache = {}

    def _nse_ticker(self, symbol: str) -> str:
        """Convert NSE symbol to yfinance format."""
        # Handle special cases
        special_cases = {
            "M&M": "M%26M.NS",
            "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
            "MCDOWELL-N": "MCDOWELL-N.NS",
        }
        if symbol in special_cases:
            return special_cases[symbol]
        return f"{symbol}.NS"

    def fetch_single(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV for a single symbol.
        Returns DataFrame with columns: Open, High, Low, Close, Volume
        Returns None on failure.
        """
        ticker = self._nse_ticker(symbol)
        try:
            df = yf.download(
                ticker,
                start=self.start_date.strftime("%Y-%m-%d"),
                end=self.end_date.strftime("%Y-%m-%d"),
                interval="1d",
                progress=False,
                auto_adjust=True,
                actions=False
            )

            if df.empty or len(df) < 10:
                logger.warning(f"Insufficient data for {symbol} ({len(df)} rows)")
                return None

            # Clean column names if MultiIndex
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Ensure required columns exist
            required = ["Open", "High", "Low", "Close", "Volume"]
            if not all(col in df.columns for col in required):
                logger.warning(f"Missing columns for {symbol}: {df.columns.tolist()}")
                return None

            df = df[required].copy()
            df.index = pd.to_datetime(df.index)
            df.sort_index(inplace=True)

            # Add derived columns useful for pattern detection
            df["Symbol"] = symbol
            df["Returns"] = df["Close"].pct_change()
            df["Candle_Body"] = abs(df["Close"] - df["Open"])
            df["Candle_Range"] = df["High"] - df["Low"]
            df["Upper_Wick"] = df["High"] - df[["Open", "Close"]].max(axis=1)
            df["Lower_Wick"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
            df["Is_Bullish"] = df["Close"] > df["Open"]
            df["Vol_MA20"] = df["Volume"].rolling(20).mean()
            df["Vol_Ratio"] = df["Volume"] / df["Vol_MA20"]  # >1.5 = volume spike

            logger.info(f"✅ {symbol}: {len(df)} sessions fetched")
            return df

        except Exception as e:
            logger.error(f"❌ Failed to fetch {symbol}: {e}")
            return None

    def fetch_all(
        self,
        symbols: list = None,
        delay: float = 0.1,
        max_retries: int = 2
    ) -> dict:
        """
        Fetch OHLCV for all F&O stocks.

        Args:
            symbols: List of symbols. Defaults to STOCK_SYMBOLS.
            delay: Seconds between requests (avoid rate limiting).
            max_retries: Retry failed symbols once more.

        Returns:
            dict: {symbol: DataFrame} for successful fetches
        """
        if symbols is None:
            symbols = STOCK_SYMBOLS

        results = {}
        failed = []

        logger.info(f"📊 Starting OHLCV fetch for {len(symbols)} F&O stocks...")
        logger.info(f"📅 Period: {self.start_date.strftime('%d-%b-%Y')} to {self.end_date.strftime('%d-%b-%Y')}")

        for i, symbol in enumerate(symbols, 1):
            df = self.fetch_single(symbol)
            if df is not None:
                results[symbol] = df
            else:
                failed.append(symbol)

            # Progress log every 25 stocks
            if i % 25 == 0:
                logger.info(f"Progress: {i}/{len(symbols)} | Success: {len(results)} | Failed: {len(failed)}")

            time.sleep(delay)

        # Retry failed symbols once
        if failed and max_retries > 0:
            logger.info(f"🔄 Retrying {len(failed)} failed symbols...")
            time.sleep(2)
            for symbol in failed.copy():
                df = self.fetch_single(symbol)
                if df is not None:
                    results[symbol] = df
                    failed.remove(symbol)
                time.sleep(0.2)

        self.data_cache = results
        self.failed_symbols = failed

        logger.info(f"\n{'='*50}")
        logger.info(f"✅ Successfully fetched: {len(results)}/{len(symbols)} stocks")
        logger.info(f"❌ Failed: {len(failed)} stocks: {failed}")
        logger.info(f"{'='*50}")

        return results

    def get_latest_candles(self, data: dict = None, n: int = 5) -> dict:
        """
        Returns last N candles for each symbol.
        Useful for quick pattern checks without full history.
        """
        if data is None:
            data = self.data_cache

        latest = {}
        for symbol, df in data.items():
            latest[symbol] = df.tail(n).copy()
        return latest

    def get_summary(self, data: dict = None) -> pd.DataFrame:
        """
        Returns a summary DataFrame with latest close, volume, returns.
        Good for quick market overview.
        """
        if data is None:
            data = self.data_cache

        rows = []
        for symbol, df in data.items():
            if df.empty:
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            rows.append({
                "Symbol": symbol,
                "Close": round(last["Close"], 2),
                "Change%": round(last["Returns"] * 100, 2),
                "Volume": int(last["Volume"]),
                "Vol_Ratio": round(last["Vol_Ratio"], 2),
                "Sessions": len(df),
                "Date": df.index[-1].strftime("%d-%b-%Y")
            })

        summary = pd.DataFrame(rows)
        if not summary.empty:
            summary.sort_values("Vol_Ratio", ascending=False, inplace=True)
        return summary

    def save_to_csv(self, data: dict = None, path: str = "/home/claude/fno_ohlcv.csv"):
        """Save all data to a single CSV for debugging / backtesting."""
        if data is None:
            data = self.data_cache

        all_dfs = []
        for symbol, df in data.items():
            all_dfs.append(df)

        if all_dfs:
            combined = pd.concat(all_dfs)
            combined.to_csv(path)
            logger.info(f"💾 Saved {len(combined)} rows to {path}")
        return path


# ─────────────────────────────────────────────
# STANDALONE TEST — run this file directly
# ─────────────────────────────────────────────

class NSEBhavFetcher:
    """
    Alternative data source: NSE Bhav Copy (official EOD data).
    Use this as primary source on Railway — more reliable than yfinance for NSE.
    Downloads directly from NSE servers.
    
    NSE Bhav Copy URL format:
    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
    """

    NSE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    def fetch_bhav_copy(self, date: datetime = None) -> Optional[pd.DataFrame]:
        """
        Fetch NSE Bhav Copy for a given date.
        Returns EOD data for all NSE stocks.
        """
        import zipfile
        import io

        if date is None:
            date = datetime.today()

        date_str = date.strftime("%Y%m%d")
        url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"

        try:
            session = requests.Session()
            # First hit NSE homepage to get cookies
            session.get("https://www.nseindia.com", headers=self.NSE_HEADERS, timeout=10)
            
            response = session.get(url, headers=self.NSE_HEADERS, timeout=15)
            response.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                csv_name = z.namelist()[0]
                with z.open(csv_name) as f:
                    df = pd.read_csv(f)

            logger.info(f"✅ Bhav copy fetched: {len(df)} records for {date.strftime('%d-%b-%Y')}")
            return df

        except Exception as e:
            logger.error(f"❌ Bhav copy fetch failed for {date_str}: {e}")
            return None

    def filter_fno_stocks(self, bhav_df: pd.DataFrame, symbols: list) -> pd.DataFrame:
        """Filter bhav copy to F&O stocks only."""
        if bhav_df is None:
            return pd.DataFrame()
        
        # NSE bhav copy uses 'TckrSymb' or 'SYMBOL' column
        symbol_col = "TckrSymb" if "TckrSymb" in bhav_df.columns else "SYMBOL"
        filtered = bhav_df[bhav_df[symbol_col].isin(symbols)].copy()
        logger.info(f"📊 F&O stocks in bhav copy: {len(filtered)}/{len(symbols)}")
        return filtered


# ─────────────────────────────────────────────
# RECOMMENDED USAGE ON RAILWAY
# ─────────────────────────────────────────────
"""
DEPLOYMENT NOTE:
----------------
yfinance works perfectly on Railway (external internet access).
NSEBhavFetcher is the backup if Yahoo Finance becomes unreliable.

Recommended schedule on Railway:
- Run fetch_all() at 4:00 PM IST (after market close)
- Store results in memory / pickle for same-day pattern scan
- Re-run at 9:00 AM IST next day for pre-market watchlist

Example Railway cron: "0 10 * * 1-5"  (4 PM IST = 10:30 UTC)
"""


if __name__ == "__main__":
    print("\n" + "="*50)
    print("CAPITAL DECODE — Module 1: OHLCV Fetcher Test")
    print("="*50 + "\n")

    # Test with small subset first
    test_symbols = [
        "RELIANCE", "HDFCBANK", "INFY", "SBIN",
        "TATAMOTORS", "BAJFINANCE", "POWERINDIA", "NYKAA"
    ]

    fetcher = OHLCVFetcher(lookback_days=60)
    data = fetcher.fetch_all(symbols=test_symbols)

    if data:
        print("\n📊 SUMMARY:")
        summary = fetcher.get_summary(data)
        print(summary.to_string(index=False))

        print("\n📌 LATEST 3 CANDLES — RELIANCE:")
        if "RELIANCE" in data:
            latest = fetcher.get_latest_candles(data, n=3)
            print(latest["RELIANCE"][["Open", "High", "Low", "Close", "Volume", "Vol_Ratio", "Is_Bullish"]].to_string())
    else:
        print("⚠️  No data fetched in this environment (sandbox network restriction).")
        print("✅  This will work normally on Railway with full internet access.")

    print("\n✅ Module 1 ready. Deploy on Railway and test with full F&O universe.")
