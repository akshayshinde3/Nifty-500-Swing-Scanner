"""
Swing Trading Scanner — Upstox Edition
----------------------------------------
Runs locally on your PC. Logs into your Upstox account (OAuth),
polls your watchlist during market hours, evaluates 5 swing-trading
pattern strategies, and shows live BUY signals on a
dashboard at http://localhost:5000

⚠️ Educational tool only. Not investment advice. No strategy here
guarantees any win rate — always paper-trade before risking capital.
"""

import os
import json
import time
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import io

import requests
import pandas as pd
import yfinance as yf
from flask import Flask, request, redirect, jsonify, render_template, session, url_for, send_file
from dotenv import load_dotenv

import strategies
from excel_export import create_prebreakout_excel

# Load environment variables from .env file
load_dotenv()

# PostgreSQL DB Configuration for paper journal persistence
DATABASE_URL = os.environ.get("DATABASE_URL", "")
HAS_POSTGRES = False
if DATABASE_URL:
    try:
        import psycopg2
        HAS_POSTGRES = True
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")

POLL_INTERVAL_SECONDS = 180         # how often to re-scan (Nifty 500 is heavy — keep this >= 120s)
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

MIN_AVG_DAILY_TURNOVER_CR = 5

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

app = Flask(__name__)
app.secret_key = os.urandom(24)

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_journal.json")
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".upstox_token")


def save_token(token):
    """Persist the access token to disk."""
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": token, "saved_at": dt.datetime.now().isoformat()}, f)
    except Exception as e:
        app.logger.warning(f"Could not save token to disk: {e}")


def load_token():
    """Load a previously saved token from disk. Returns None if not found."""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        return data.get("access_token")
    except Exception:
        return None


def clear_token():
    """Delete the saved token file."""
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
    except Exception:
        pass

# In-memory state (single-user local app — no DB needed)
STATE = {
    "universe": [],           # Nifty 500 symbols being scanned
    "confirmed_alerts": [],   # breakouts that already triggered
    "watch_alerts": [],       # pre-breakout candidates (not yet triggered)
    "weekly_confirmed_alerts": [], # weekly breakouts
    "weekly_watch_alerts": [],     # weekly pre-breakout candidates
    "last_scan": None,
    "scan_progress": None,    # "120/500" while warming the candle cache
    "scanning": False,
    "error": None,
    # Paper trading
    "paper_balance":  100000.0,
    "paper_initial":  100000.0,
    "paper_trades":   [],      # open positions
    "paper_history":  [],      # closed trades
}


def init_db():
    """Initialize the PostgreSQL database table if database connectivity is available."""
    if not DATABASE_URL or not HAS_POSTGRES:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scanner_state (
                key VARCHAR(50) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        app.logger.error(f"Failed to initialize PostgreSQL database: {e}")


def _load_paper_state():
    """Restore paper trading state and journal history from disk or database."""
    if DATABASE_URL and HAS_POSTGRES:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT value FROM scanner_state WHERE key = 'paper_trading'")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                data = json.loads(row[0])
                STATE["paper_balance"] = float(data.get("paper_balance", STATE["paper_balance"]))
                STATE["paper_initial"] = float(data.get("paper_initial", STATE["paper_initial"]))
                STATE["paper_trades"] = data.get("paper_trades", [])
                STATE["paper_history"] = data.get("paper_history", [])
                app.logger.info("Loaded paper journal from PostgreSQL database.")
                return
        except Exception as e:
            app.logger.error(f"Could not load paper journal from PostgreSQL: {e}")

    # Fallback to local file storage
    if not os.path.exists(JOURNAL_FILE):
        return
    try:
        with open(JOURNAL_FILE) as f:
            data = json.load(f)
        STATE["paper_balance"] = float(data.get("paper_balance", STATE["paper_balance"]))
        STATE["paper_initial"] = float(data.get("paper_initial", STATE["paper_initial"]))
        STATE["paper_trades"] = data.get("paper_trades", [])
        STATE["paper_history"] = data.get("paper_history", [])
    except Exception as e:
        app.logger.warning(f"Could not load paper journal: {e}")


def _save_paper_state():
    """Persist paper trading state and journal history to disk or database."""
    if DATABASE_URL and HAS_POSTGRES:
        try:
            data_str = json.dumps({
                "paper_balance": STATE["paper_balance"],
                "paper_initial": STATE["paper_initial"],
                "paper_trades": STATE["paper_trades"],
                "paper_history": STATE["paper_history"],
            })
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scanner_state (key, value, updated_at) 
                VALUES ('paper_trading', %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) 
                DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            """, (data_str,))
            conn.commit()
            cur.close()
            conn.close()
            app.logger.info("Saved paper journal to PostgreSQL database.")
            return
        except Exception as e:
            app.logger.error(f"Could not save paper journal to PostgreSQL: {e}")

    # Fallback to local file storage
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump({
                "paper_balance": STATE["paper_balance"],
                "paper_initial": STATE["paper_initial"],
                "paper_trades": STATE["paper_trades"],
                "paper_history": STATE["paper_history"],
            }, f, indent=2)
    except Exception as e:
        app.logger.warning(f"Could not save paper journal: {e}")


init_db()
_load_paper_state()


def load_nifty500_symbols():
    """Fetch the official Nifty 500 constituent list from NSE. Falls back to
    a local nifty500.txt (one symbol per line) if NSE blocks/changes the URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
        r = requests.get(NIFTY500_URL, headers=headers, timeout=20)
        r.raise_for_status()
        lines = r.text.splitlines()
        symbols = []
        for line in lines[1:]:  # skip header row
            parts = line.split(",")
            if len(parts) >= 3:
                symbols.append(parts[2].strip().upper())  # "Symbol" column
        if symbols:
            return symbols
    except Exception as e:
        STATE["error"] = f"Could not fetch live Nifty 500 list ({e}); using local nifty500.txt fallback."

    path = os.path.join(os.path.dirname(__file__), "nifty500.txt")
    if os.path.exists(path):
        with open(path) as f:
            return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
    return []


@app.before_request
def require_login():
    allowed_routes = ["login", "static"]
    if request.endpoint and request.endpoint not in allowed_routes:
        if SITE_PASSWORD and not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not SITE_PASSWORD:
        return redirect(url_for("home"))
    
    if session.get("authenticated"):
        return redirect(url_for("home"))
        
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == SITE_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("home"))
        else:
            error = "Invalid password. Please try again."
            
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
def home():
    if not STATE["scanning"]:
        try:
            STATE["universe"] = load_nifty500_symbols()
            start_scanner_thread()
        except Exception as e:
            STATE["error"] = f"Failed to start scanner on load: {e}"
            app.logger.exception("start_scanner failed")
    try:
        return render_template("dashboard.html")
    except Exception as e:
        import traceback
        return f"<pre style='color:red;padding:20px'>Dashboard render error:\n{traceback.format_exc()}</pre>", 500


def save_env_keys(newsapi_key, site_password=None):
    global NEWSAPI_KEY, SITE_PASSWORD
    NEWSAPI_KEY = newsapi_key
    if site_password is not None:
        SITE_PASSWORD = site_password

    # Save to .env file
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    with open(env_path, "w") as f:
        f.write(f"NEWSAPI_KEY={NEWSAPI_KEY}\n")
        f.write(f"SITE_PASSWORD={SITE_PASSWORD}\n")


@app.route("/get-settings")
def get_settings():
    return jsonify({
        "NEWSAPI_KEY": NEWSAPI_KEY,
        "SITE_PASSWORD": SITE_PASSWORD
    })


@app.route("/update-settings", methods=["POST"])
def update_settings():
    data = request.get_json() or {}
    newsapi_key = data.get("NEWSAPI_KEY", "").strip()
    site_password = data.get("SITE_PASSWORD", "").strip()

    try:
        save_env_keys(newsapi_key, site_password)
        if site_password:
            session["authenticated"] = True
        else:
            session.pop("authenticated", None)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def get_daily_candles(symbol, days=365):
    ticker_symbol = f"{symbol.upper()}.NS"
    df = yf.download([ticker_symbol], period="1y", interval="1d", ignore_tz=True, progress=False)
    df = df.xs(ticker_symbol, axis=1, level=1).dropna(subset=["Close"])
    
    candles = []
    for index, row in df.iterrows():
        ts = index.strftime("%Y-%m-%dT00:00:00+05:30")
        candles.append([
            ts,
            float(row["Open"]),
            float(row["High"]),
            float(row["Low"]),
            float(row["Close"]),
            int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            0
        ])
    return candles


def get_weekly_candles(symbol, weeks=120):
    ticker_symbol = f"{symbol.upper()}.NS"
    df = yf.download([ticker_symbol], period="3y", interval="1wk", ignore_tz=True, progress=False)
    df = df.xs(ticker_symbol, axis=1, level=1).dropna(subset=["Close"])
    
    candles = []
    for index, row in df.iterrows():
        ts = index.strftime("%Y-%m-%dT00:00:00+05:30")
        candles.append([
            ts,
            float(row["Open"]),
            float(row["High"]),
            float(row["Low"]),
            float(row["Close"]),
            int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            0
        ])
    return candles


def get_yfinance_quotes(symbols):
    """Fetch current price, net change, change percentage, and volume for a list of symbols in bulk."""
    if not symbols:
        return {}
    
    tickers = [f"{sym.upper()}.NS" for sym in symbols]
    df = yf.download(tickers, period="5d", ignore_tz=True, progress=False)
    
    result = {}
    for sym in symbols:
        ticker_ns = f"{sym.upper()}.NS"
        try:
            ticker_df = df.xs(ticker_ns, axis=1, level=1)
            close_col = ticker_df['Close'].dropna()
            vol_col = ticker_df['Volume'].dropna()
            
            if len(close_col) >= 1:
                ltp = float(close_col.iloc[-1])
                volume = int(vol_col.iloc[-1]) if len(vol_col) >= 1 else 0
                
                if len(close_col) >= 2:
                    prev_close = float(close_col.iloc[-2])
                    net_change = ltp - prev_close
                    change_pct = (net_change / prev_close) * 100
                else:
                    prev_close = ltp
                    net_change = 0.0
                    change_pct = 0.0
                    
                result[sym.upper()] = {
                    "ltp": round(ltp, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": volume,
                    "net_change": round(net_change, 2)
                }
        except Exception as e:
            app.logger.warning(f"Error fetching yfinance quote for {sym}: {e}")
            
    return result


def merge_candles(existing_candles, new_candles):
    """Merge new candles into existing candles list, avoiding duplicates by date."""
    candle_dict = {c[0][:10]: c for c in existing_candles}
    for nc in new_candles:
        date_str = nc[0][:10]
        candle_dict[date_str] = nc
    sorted_dates = sorted(candle_dict.keys())
    return [candle_dict[d] for d in sorted_dates]


def passes_liquidity_filter(candles):
    if len(candles) < 20:
        return False
    turnovers = [c[4] * c[5] for c in candles[-20:]]  # close * volume, in INR
    avg_turnover_cr = (sum(turnovers) / len(turnovers)) / 1e7  # convert to crore
    return avg_turnover_cr >= MIN_AVG_DAILY_TURNOVER_CR


def evaluate_symbol(symbol, candles, live_ltp):
    """Liquidity gate, then hand off to the enabled chart-pattern scanner."""
    if not passes_liquidity_filter(candles):
        return None
    try:
        return strategies.build_alert(symbol, candles, live_ltp)
    except Exception:
        return None


def evaluate_symbol_weekly(symbol, candles, live_ltp):
    """Hand off weekly candles to the chart-pattern scanner."""
    if len(candles) < 45:
        return None
    try:
        return strategies.build_alert(symbol, candles, live_ltp)
    except Exception:
        return None


def get_news_flag(symbol):
    """Optional: recent news headline count via NewsAPI.org (free tier)."""
    if not NEWSAPI_KEY:
        return None
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": symbol, "language": "en", "sortBy": "publishedAt", "pageSize": 3,
                    "apiKey": NEWSAPI_KEY},
            timeout=8,
        )
        arts = r.json().get("articles", [])
        return [{"title": a["title"], "source": a["source"]["name"], "url": a["url"]} for a in arts]
    except Exception:
        return None


_candle_cache = {}        # symbol -> candles
_weekly_candle_cache = {}  # symbol -> weekly candles
_candle_cache_date   = None
_weekly_cache_date   = None

CANDLE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candle_cache.json.gz")


def save_candle_cache_to_disk():
    """Save daily and weekly candle caches to a compressed disk file."""
    try:
        data = {
            "date": _candle_cache_date.isoformat() if _candle_cache_date else None,
            "weekly_date": _weekly_cache_date.isoformat() if _weekly_cache_date else None,
            "daily": _candle_cache,
            "weekly": _weekly_candle_cache
        }
        with gzip.open(CANDLE_CACHE_FILE, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        app.logger.info("Candle cache saved to disk.")
    except Exception as e:
        app.logger.warning(f"Could not save candle cache to disk: {e}")


def load_candle_cache_from_disk():
    """Load daily and weekly candle caches from a compressed disk file if date matches today."""
    global _candle_cache, _weekly_candle_cache, _candle_cache_date, _weekly_cache_date
    if not os.path.exists(CANDLE_CACHE_FILE):
        return
    try:
        with gzip.open(CANDLE_CACHE_FILE, "rt", encoding="utf-8") as f:
            data = json.load(f)
        d_str = data.get("date")
        w_str = data.get("weekly_date")
        today = dt.date.today()
        if d_str and dt.date.fromisoformat(d_str) == today:
            _candle_cache = data.get("daily", {})
            _candle_cache_date = today
            app.logger.info(f"Loaded daily candle cache from disk for {today} (contains {len(_candle_cache)} symbols)")
        if w_str and dt.date.fromisoformat(w_str) == today:
            _weekly_candle_cache = data.get("weekly", {})
            _weekly_cache_date = today
            app.logger.info(f"Loaded weekly candle cache from disk for {today} (contains {len(_weekly_candle_cache)} symbols)")
    except Exception as e:
        app.logger.warning(f"Could not load candle cache from disk: {e}")


def _maybe_invalidate_candle_cache():
    global _candle_cache, _weekly_candle_cache, _candle_cache_date, _weekly_cache_date
    today = dt.date.today()
    if _candle_cache_date != today:
        _candle_cache.clear()
        _candle_cache_date = today
        app.logger.info(f"Daily candle cache invalidated for {today}")
    if _weekly_cache_date != today:
        _weekly_candle_cache.clear()
        _weekly_cache_date = today
        app.logger.info(f"Weekly candle cache invalidated for {today}")


def warm_cache_for_all_symbols():
    universe = STATE["universe"]
    if not universe:
        return

    _maybe_invalidate_candle_cache()

    missing_daily = [sym for sym in universe if sym not in _candle_cache]
    total_missing = len(missing_daily)

    if total_missing > 0:
        app.logger.info(f"Warming daily candles for {total_missing} symbols using yfinance batches...")
        
        batch_size = 100
        completed = 0
        STATE["scan_progress"] = f"0/{total_missing}"
        
        for i in range(0, total_missing, batch_size):
            batch = missing_daily[i:i+batch_size]
            tickers_ns = [f"{sym}.NS" for sym in batch]
            try:
                df = yf.download(tickers_ns, period="1y", interval="1d", ignore_tz=True, progress=False)
                
                for sym in batch:
                    ticker_ns = f"{sym}.NS"
                    try:
                        if len(tickers_ns) == 1:
                            ticker_df = df
                        else:
                            ticker_df = df.xs(ticker_ns, axis=1, level=1)
                            
                        ticker_df = ticker_df.dropna(subset=["Close"])
                        
                        candles = []
                        for index, row in ticker_df.iterrows():
                            ts = index.strftime("%Y-%m-%dT00:00:00+05:30")
                            candles.append([
                                ts,
                                float(row["Open"]),
                                float(row["High"]),
                                float(row["Low"]),
                                float(row["Close"]),
                                int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                                0
                            ])
                        if candles:
                            _candle_cache[sym] = candles
                    except Exception as e:
                        app.logger.warning(f"Error parsing daily candles for {sym}: {e}")
            except Exception as e:
                app.logger.error(f"Error downloading daily batch: {e}")
            
            completed += len(batch)
            STATE["scan_progress"] = f"{completed}/{total_missing}"
            save_candle_cache_to_disk()
            time.sleep(0.5)
            
        app.logger.info("Finished warming daily candles.")

    missing_weekly = []
    for sym in universe:
        if sym not in _weekly_candle_cache:
            candles = _candle_cache.get(sym)
            if candles and passes_liquidity_filter(candles):
                missing_weekly.append(sym)

    total_missing_weekly = len(missing_weekly)
    if total_missing_weekly > 0:
        app.logger.info(f"Warming weekly candles for {total_missing_weekly} liquid symbols...")
        
        batch_size = 100
        completed = 0
        STATE["scan_progress"] = f"Weekly: 0/{total_missing_weekly}"
        
        for i in range(0, total_missing_weekly, batch_size):
            batch = missing_weekly[i:i+batch_size]
            tickers_ns = [f"{sym}.NS" for sym in batch]
            try:
                df = yf.download(tickers_ns, period="3y", interval="1wk", ignore_tz=True, progress=False)
                
                for sym in batch:
                    ticker_ns = f"{sym}.NS"
                    try:
                        if len(tickers_ns) == 1:
                            ticker_df = df
                        else:
                            ticker_df = df.xs(ticker_ns, axis=1, level=1)
                            
                        ticker_df = ticker_df.dropna(subset=["Close"])
                        
                        candles = []
                        for index, row in ticker_df.iterrows():
                            ts = index.strftime("%Y-%m-%dT00:00:00+05:30")
                            candles.append([
                                ts,
                                float(row["Open"]),
                                float(row["High"]),
                                float(row["Low"]),
                                float(row["Close"]),
                                int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                                0
                            ])
                        if candles:
                            _weekly_candle_cache[sym] = candles
                    except Exception as e:
                        app.logger.warning(f"Error parsing weekly candles for {sym}: {e}")
            except Exception as e:
                app.logger.error(f"Error downloading weekly batch: {e}")
                
            completed += len(batch)
            STATE["scan_progress"] = f"Weekly: {completed}/{total_missing_weekly}"
            save_candle_cache_to_disk()
            time.sleep(0.5)
            
        app.logger.info("Finished warming weekly candles.")


def is_market_open():
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def scan_loop():
    STATE["scanning"] = True
    load_candle_cache_from_disk()

    while True:
        try:
            universe = STATE["universe"]
            if not universe:
                STATE["error"] = "Nifty 500 list not loaded yet."
                time.sleep(10)
                continue

            market_open = is_market_open()
            STATE["error"] = None

            warm_cache_for_all_symbols()

            if market_open:
                app.logger.info("Market is open: fetching live updates for all symbols...")
                batch_size = 100
                total_batches = (len(universe) + batch_size - 1) // batch_size
                
                for i in range(0, len(universe), batch_size):
                    batch = universe[i:i+batch_size]
                    tickers_ns = [f"{sym}.NS" for sym in batch]
                    STATE["scan_progress"] = f"Live Batch {i//batch_size + 1}/{total_batches}"
                    try:
                        df = yf.download(tickers_ns, period="5d", interval="1d", ignore_tz=True, progress=False)
                        for sym in batch:
                            ticker_ns = f"{sym}.NS"
                            try:
                                if len(tickers_ns) == 1:
                                    ticker_df = df
                                else:
                                    ticker_df = df.xs(ticker_ns, axis=1, level=1)
                                    
                                ticker_df = ticker_df.dropna(subset=["Close"])
                                new_candles = []
                                for index, row in ticker_df.iterrows():
                                    ts = index.strftime("%Y-%m-%dT00:00:00+05:30")
                                    new_candles.append([
                                        ts,
                                        float(row["Open"]),
                                        float(row["High"]),
                                        float(row["Low"]),
                                        float(row["Close"]),
                                        int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                                        0
                                    ])
                                if new_candles:
                                    if sym in _candle_cache:
                                        _candle_cache[sym] = merge_candles(_candle_cache[sym], new_candles)
                                    else:
                                        _candle_cache[sym] = new_candles
                            except Exception as e:
                                app.logger.warning(f"Error parsing live daily for {sym}: {e}")
                    except Exception as e:
                        app.logger.error(f"Error downloading live batch: {e}")
                    time.sleep(0.5)
                save_candle_cache_to_disk()

            confirmed, watch = [], []
            w_confirmed, w_watch = [], []
            
            for i, sym in enumerate(universe):
                STATE["scan_progress"] = f"Scanning {i + 1}/{len(universe)}"
                candles = _candle_cache.get(sym)
                
                if candles:
                    live_price = candles[-1][4]
                    res = evaluate_symbol(sym, candles, live_price)
                    if res:
                        res["timestamp"] = dt.datetime.now().strftime("%H:%M:%S")
                        (confirmed if res["status"] == "confirmed" else watch).append(res)
                        
                    if passes_liquidity_filter(candles):
                        w_candles = _weekly_candle_cache.get(sym)
                        if w_candles:
                            last_close_w = w_candles[-1][4]
                            w_res = evaluate_symbol_weekly(sym, w_candles, last_close_w)
                            if w_res:
                                w_res["timestamp"] = dt.datetime.now().strftime("%H:%M:%S")
                                (w_confirmed if w_res["status"] == "confirmed" else w_watch).append(w_res)
            
            STATE["confirmed_alerts"] = sorted(confirmed, key=lambda x: -x["confidence"])
            STATE["watch_alerts"] = sorted(watch, key=lambda x: -x["confidence"])
            STATE["weekly_confirmed_alerts"] = sorted(w_confirmed, key=lambda x: -x["confidence"])
            STATE["weekly_watch_alerts"] = sorted(w_watch, key=lambda x: -x["confidence"])
            STATE["scan_progress"] = None
            
            if market_open:
                STATE["last_scan"] = dt.datetime.now().strftime("%H:%M:%S")
            else:
                STATE["last_scan"] = dt.datetime.now().strftime("%H:%M:%S") + " (EOD)"
                STATE["error"] = "Market closed — showing EOD pattern signals. Live scanning resumes at 9:15 IST."

        except Exception as e:
            STATE["error"] = f"Scan error: {e}"
            app.logger.exception("Error in scan_loop")

        sleep_secs = POLL_INTERVAL_SECONDS if is_market_open() else 3600
        time.sleep(sleep_secs)


_thread_started = False


def start_scanner_thread():
    global _thread_started
    if not _thread_started:
        t = threading.Thread(target=scan_loop, daemon=True)
        t.start()
        _thread_started = True


@app.route("/api/signals")
def api_signals():
    return jsonify({
        "confirmed_alerts": STATE["confirmed_alerts"],
        "watch_alerts": STATE["watch_alerts"],
        "last_scan": STATE["last_scan"],
        "scan_progress": STATE["scan_progress"],
        "market_open": is_market_open(),
        "error": STATE["error"],
        "universe_size": len(STATE["universe"]),
    })


@app.route("/api/signals/weekly")
def api_signals_weekly():
    return jsonify({
        "confirmed_alerts": STATE["weekly_confirmed_alerts"],
        "watch_alerts": STATE["weekly_watch_alerts"],
        "last_scan": STATE["last_scan"],
        "scan_progress": STATE["scan_progress"],
        "market_open": is_market_open(),
        "error": STATE["error"],
        "universe_size": len(STATE["universe"]),
    })


@app.route("/api/export/prebreakout")
@app.route("/api/export/excel")
def api_export_prebreakout():
    timeframe = request.args.get("timeframe", "both").lower()

    daily_watch = list(STATE.get("watch_alerts", []))
    weekly_watch = list(STATE.get("weekly_watch_alerts", []))
    confirmed_daily = list(STATE.get("confirmed_alerts", []))
    confirmed_weekly = list(STATE.get("weekly_confirmed_alerts", []))

    if not daily_watch and not weekly_watch:
        load_candle_cache_from_disk()
        cache_file = os.path.join(os.path.dirname(__file__), "candle_cache.json.gz")
        if os.path.exists(cache_file):
            try:
                with gzip.open(cache_file, "rt") as f:
                    c_data = json.load(f)
                d_cache = c_data.get("daily", {})
                w_cache = c_data.get("weekly", {})
                for sym, candles in d_cache.items():
                    if candles:
                        res = evaluate_symbol(sym, candles, candles[-1][4])
                        if res:
                            if res["status"] == "pre_breakout_watch":
                                daily_watch.append(res)
                            elif res["status"] == "confirmed":
                                confirmed_daily.append(res)
                for sym, candles in w_cache.items():
                    if candles and passes_liquidity_filter(d_cache.get(sym, [])):
                        w_res = evaluate_symbol_weekly(sym, candles, candles[-1][4])
                        if w_res:
                            if w_res["status"] == "pre_breakout_watch":
                                weekly_watch.append(w_res)
                            elif w_res["status"] == "confirmed":
                                confirmed_weekly.append(w_res)
            except Exception as e:
                app.logger.warning(f"Error reading candle cache for Excel export: {e}")

    if timeframe == "daily":
        target_daily = daily_watch
        target_weekly = []
    elif timeframe == "weekly":
        target_daily = []
        target_weekly = weekly_watch
    else:
        target_daily = daily_watch
        target_weekly = weekly_watch

    wb = create_prebreakout_excel(target_daily, target_weekly, confirmed_daily, confirmed_weekly)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    date_str = dt.date.today().strftime("%Y-%m-%d")
    filename = f"PreBreakout_Stocks_{timeframe}_{date_str}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/news/<symbol>")
def api_news(symbol):
    return jsonify({"news": get_news_flag(symbol)})


@app.route("/api/ltp")
def api_ltp():
    raw_symbols = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    if not symbols:
        return jsonify({}), 200

    try:
        result = get_yfinance_quotes(symbols)
        return jsonify(result)
    except Exception as e:
        app.logger.warning(f"/api/ltp error: {e}")
        return jsonify({}), 500


@app.route("/api/stocks")
def api_stocks():
    universe = STATE["universe"]

    alert_map = {}
    for a in STATE["confirmed_alerts"]:
        alert_map[a["symbol"]] = {"pattern": a["strategy_used"], "status": "confirmed", "confidence": a["confidence"]}
    for a in STATE["watch_alerts"]:
        if a["symbol"] not in alert_map:
            alert_map[a["symbol"]] = {"pattern": a["strategy_used"], "status": "watch", "confidence": a["confidence"]}

    stocks = []
    for sym in universe:
        candles = _candle_cache.get(sym)
        
        if not candles or len(candles) < 2:
            stocks.append({
                "symbol": sym,
                "close": None,
                "prev_close": None,
                "change_pct": None,
                "volume": None,
                "high_52w": None,
                "low_52w": None,
                "pattern": alert_map.get(sym, {}).get("pattern", ""),
                "status": alert_map.get(sym, {}).get("status", ""),
                "confidence": alert_map.get(sym, {}).get("confidence", 0),
            })
            continue

        ltp = candles[-1][4]
        prev_close = candles[-2][4]
        volume = candles[-1][5]

        change_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        highs = [c[2] for c in candles[-252:]]
        lows  = [c[3] for c in candles[-252:]]

        stocks.append({
            "symbol":     sym,
            "close":      round(ltp, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": change_pct,
            "volume":     int(volume),
            "high_52w":   round(max(highs), 2) if highs else None,
            "low_52w":    round(min(lows), 2) if lows else None,
            "pattern":    alert_map.get(sym, {}).get("pattern", ""),
            "status":     alert_map.get(sym, {}).get("status", ""),
            "confidence": alert_map.get(sym, {}).get("confidence", 0),
        })

    stocks.sort(key=lambda x: (-x["confidence"], x["symbol"]))
    return jsonify({
        "stocks": stocks,
        "total": len(stocks),
        "loaded": sum(1 for s in stocks if s["close"] is not None),
        "last_scan": STATE["last_scan"],
        "scan_progress": STATE["scan_progress"],
    })


def get_or_fetch_candles(symbol):
    _maybe_invalidate_candle_cache()
    sym = symbol.upper()
    candles = _candle_cache.get(sym)
    if not candles:
        try:
            candles = get_daily_candles(sym)
            _candle_cache[sym] = candles
        except Exception:
            pass
    return candles


def get_or_fetch_weekly_candles(symbol):
    _maybe_invalidate_candle_cache()
    sym = symbol.upper()
    candles = _weekly_candle_cache.get(sym)
    if not candles:
        try:
            candles = get_weekly_candles(sym)
            _weekly_candle_cache[sym] = candles
        except Exception:
            pass
    return candles


@app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    global _candle_cache, _weekly_candle_cache, _candle_cache_date, _weekly_cache_date
    count = len(_candle_cache) + len(_weekly_candle_cache)
    _candle_cache.clear()
    _weekly_candle_cache.clear()
    _candle_cache_date  = None
    _weekly_cache_date  = None
    app.logger.info("Candle cache manually cleared via /api/cache/clear")
    return jsonify({"success": True, "cleared_symbols": count})


@app.route("/api/candles/<symbol>")
def api_candles(symbol):
    sym = symbol.upper()
    candles = get_or_fetch_candles(sym)
    if not candles:
        return jsonify({"error": "Symbol candle data not loaded or unavailable."}), 404

    result = []
    for c in candles[-120:]:
        ts = c[0]
        date_str = ts[:10] if isinstance(ts, str) else str(ts)[:10]
        result.append({
            "time":   date_str,
            "open":   round(c[1], 2),
            "high":   round(c[2], 2),
            "low":    round(c[3], 2),
            "close":  round(c[4], 2),
            "volume": int(c[5]),
        })

    return jsonify({"symbol": sym, "candles": result})


@app.route("/api/candles/<symbol>/weekly")
def api_candles_weekly(symbol):
    sym = symbol.upper()
    candles = get_or_fetch_weekly_candles(sym)
    if not candles:
        return jsonify({"error": "Symbol weekly candle data not loaded or unavailable."}), 404

    result = []
    for c in candles[-120:]:
        ts = c[0]
        date_str = ts[:10] if isinstance(ts, str) else str(ts)[:10]
        result.append({
            "time":   date_str,
            "open":   round(c[1], 2),
            "high":   round(c[2], 2),
            "low":    round(c[3], 2),
            "close":  round(c[4], 2),
            "volume": int(c[5]),
        })

    return jsonify({"symbol": sym, "candles": result})


@app.route("/api/pattern/<symbol>")
def api_pattern(symbol):
    sym = symbol.upper()
    candles = get_or_fetch_candles(sym)
    if not candles:
        return jsonify({"error": "No candle data"}), 404

    window = candles[-120:]
    dates = []
    for c in window:
        ts = c[0]
        dates.append(ts[:10] if isinstance(ts, str) else str(ts)[:10])

    n = len(window)
    patterns = {}

    def trendline_pts(slope, intercept, x_start, x_end, bar_offset=0):
        pts = []
        for xi in (x_start, x_end):
            idx = bar_offset + xi
            if 0 <= idx < len(dates):
                pts.append({"time": dates[idx], "value": round(slope * xi + intercept, 2)})
        return pts if len(pts) == 2 else []

    try:
        tri = strategies.detect_triangle(candles)
        if tri:
            LOOKBACK = 40
            w = candles[-LOOKBACK:]
            highs = [c[2] for c in w]
            lows  = [c[3] for c in w]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=2)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - LOOKBACK
            lx  = LOOKBACK - 1
            patterns["triangle"] = {
                "type": tri["type"],
                "resistance_line": trendline_pts(rs, ri, 0, lx, off),
                "support_line":    trendline_pts(ss, si_, 0, lx, off),
                "resistance_level": tri["resistance_now"],
                "support_level":    tri["support_now"],
                "swing_highs": [{"time": dates[off + p[0]], "value": round(p[1], 2)}
                                 for p in sh if 0 <= off + p[0] < len(dates)],
                "swing_lows":  [{"time": dates[off + p[0]], "value": round(p[1], 2)}
                                 for p in sl_ if 0 <= off + p[0] < len(dates)],
            }
    except Exception:
        pass

    try:
        flag = strategies.detect_flag(candles)
        if flag:
            pole_s = n - 30
            pole_e = n - 10
            flag_s = n - 10
            flag_e = n - 1
            patterns["flag"] = {
                "type": flag["type"],
                "pole": [
                    {"time": dates[pole_s], "value": round(window[pole_s][4], 2)},
                    {"time": dates[pole_e], "value": round(window[pole_e][4], 2)},
                ],
                "flag_resistance": [
                    {"time": dates[flag_s], "value": flag["flag_resistance"]},
                    {"time": dates[flag_e], "value": flag["flag_resistance"]},
                ],
                "flag_support": [
                    {"time": dates[flag_s], "value": flag["flag_support"]},
                    {"time": dates[flag_e], "value": flag["flag_support"]},
                ],
                "resistance_level": flag["flag_resistance"],
                "support_level":    flag["flag_support"],
                "pole_move_pct":    flag["pole_move_pct"],
            }
    except Exception:
        pass

    try:
        wedge = strategies.detect_wedge(candles)
        if wedge:
            w30 = candles[-30:]
            highs = [c[2] for c in w30]
            lows  = [c[3] for c in w30]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=2)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - 30
            patterns["wedge"] = {
                "type": wedge["type"],
                "resistance_line": trendline_pts(rs, ri, 0, 29, off),
                "support_line":    trendline_pts(ss, si_, 0, 29, off),
                "resistance_level": wedge["resistance"],
                "support_level":    wedge["support"],
                "swing_highs": [{"time": dates[off + p[0]], "value": round(p[1], 2)}
                                 for p in sh if 0 <= off + p[0] < len(dates)],
                "swing_lows":  [{"time": dates[off + p[0]], "value": round(p[1], 2)}
                                 for p in sl_ if 0 <= off + p[0] < len(dates)],
            }
    except Exception:
        pass

    try:
        pennant = strategies.detect_pennant(candles)
        if pennant:
            closes = [c[4] for c in window]
            pole_si, pole_ei, consol_si = 0, 0, 0
            for i in range(max(0, n - 25), n - 8):
                move = (closes[i + 8] - closes[i]) / closes[i] * 100
                if move >= 7.0:
                    pole_si   = i
                    pole_ei   = i + 8
                    consol_si = pole_ei
                    break
            consol_w = window[consol_si:]
            c_highs = [c[2] for c in consol_w]
            c_lows  = [c[3] for c in consol_w]
            if len(consol_w) >= 2:
                rs, ri, _ = strategies.linreg([(xi, v) for xi, v in enumerate(c_highs)])
                ss, si_v, _ = strategies.linreg([(xi, v) for xi, v in enumerate(c_lows)])
                cx = len(consol_w) - 1
                patterns["pennant"] = {
                    "type": pennant["type"],
                    "pole": [
                        {"time": dates[pole_si],     "value": round(window[pole_si][4],     2)},
                        {"time": dates[pole_ei - 1], "value": round(window[pole_ei - 1][4], 2)},
                    ],
                    "resistance_line": trendline_pts(rs, ri, 0, cx, consol_si),
                    "support_line":    trendline_pts(ss, si_v, 0, cx, consol_si),
                    "resistance_level": pennant["resistance"],
                    "support_level":    pennant["support"],
                    "pole_move_pct":    pennant["pole_move_pct"],
                }
    except Exception:
        pass

    try:
        channel = strategies.detect_channel(candles)
        if channel:
            w40 = candles[-40:]
            highs = [c[2] for c in w40]
            lows  = [c[3] for c in w40]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=3)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - 40
            patterns["channel"] = {
                "type": channel["type"],
                "resistance_line": trendline_pts(rs, ri, 0, 39, off),
                "support_line":    trendline_pts(ss, si_, 0, 39, off),
                "resistance_level": channel["resistance"],
                "support_level":    channel["support"],
            }
    except Exception:
        pass

    return jsonify({"symbol": sym, "patterns": patterns})


@app.route("/api/pattern/<symbol>/weekly")
def api_pattern_weekly(symbol):
    sym = symbol.upper()
    candles = get_or_fetch_weekly_candles(sym)
    if not candles:
        return jsonify({"error": "No candle data"}), 404

    window = candles[-120:]
    dates = []
    for c in window:
        ts = c[0]
        dates.append(ts[:10] if isinstance(ts, str) else str(ts)[:10])

    n = len(window)
    patterns = {}

    def trendline_pts(slope, intercept, x_start, x_end, bar_offset=0):
        pts = []
        for xi in (x_start, x_end):
            idx = bar_offset + xi
            if 0 <= idx < len(dates):
                pts.append({"time": dates[idx], "value": round(slope * xi + intercept, 2)})
        return pts if len(pts) == 2 else []

    try:
        tri = strategies.detect_triangle(candles)
        if tri:
            LOOKBACK = 40
            w = candles[-LOOKBACK:]
            highs = [c[2] for c in w]
            lows  = [c[3] for c in w]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=2)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - LOOKBACK
            lx  = LOOKBACK - 1
            patterns["triangle"] = {
                "type": tri["type"],
                "resistance_line": trendline_pts(rs, ri, 0, lx, off),
                "support_line":    trendline_pts(ss, si_, 0, lx, off),
                "resistance_level": tri["resistance_now"],
                "support_level":    tri["support_now"],
            }
    except Exception:
        pass

    try:
        flag = strategies.detect_flag(candles)
        if flag:
            pole_s = n - 30
            pole_e = n - 10
            flag_s = n - 10
            flag_e = n - 1
            patterns["flag"] = {
                "type": flag["type"],
                "pole": [
                    {"time": dates[pole_s], "value": round(window[pole_s][4], 2)},
                    {"time": dates[pole_e], "value": round(window[pole_e][4], 2)},
                ],
                "flag_resistance": [
                    {"time": dates[flag_s], "value": flag["flag_resistance"]},
                    {"time": dates[flag_e], "value": flag["flag_resistance"]},
                ],
                "flag_support": [
                    {"time": dates[flag_s], "value": flag["flag_support"]},
                    {"time": dates[flag_e], "value": flag["flag_support"]},
                ],
                "resistance_level": flag["flag_resistance"],
                "support_level":    flag["flag_support"],
                "pole_move_pct":    flag["pole_move_pct"],
            }
    except Exception:
        pass

    try:
        wedge = strategies.detect_wedge(candles)
        if wedge:
            w30 = candles[-30:]
            highs = [c[2] for c in w30]
            lows  = [c[3] for c in w30]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=2)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - 30
            patterns["wedge"] = {
                "type": wedge["type"],
                "resistance_line": trendline_pts(rs, ri, 0, 29, off),
                "support_line":    trendline_pts(ss, si_, 0, 29, off),
                "resistance_level": wedge["resistance"],
                "support_level":    wedge["support"],
            }
    except Exception:
        pass

    try:
        pennant = strategies.detect_pennant(candles)
        if pennant:
            closes = [c[4] for c in window]
            pole_si, pole_ei, consolidation_si = 0, 0, 0
            for i in range(max(0, n - 25), n - 8):
                move = (closes[i + 8] - closes[i]) / closes[i] * 100
                if move >= 7.0:
                    pole_si   = i
                    pole_ei   = i + 8
                    consolidation_si = pole_ei
                    break
            consol_w = window[consolidation_si:]
            c_highs = [c[2] for c in consol_w]
            c_lows  = [c[3] for c in consol_w]
            if len(consol_w) >= 2:
                rs, ri, _ = strategies.linreg([(xi, v) for xi, v in enumerate(c_highs)])
                ss, si_v, _ = strategies.linreg([(xi, v) for xi, v in enumerate(c_lows)])
                cx = len(consol_w) - 1
                patterns["pennant"] = {
                    "type": pennant["type"],
                    "pole": [
                        {"time": dates[pole_si],     "value": round(window[pole_si][4],     2)},
                        {"time": dates[pole_ei - 1], "value": round(window[pole_ei - 1][4], 2)},
                    ],
                    "resistance_line": trendline_pts(rs, ri, 0, cx, consolidation_si),
                    "support_line":    trendline_pts(ss, si_v, 0, cx, consolidation_si),
                    "resistance_level": pennant["resistance"],
                    "support_level":    pennant["support"],
                    "pole_move_pct":    pennant["pole_move_pct"],
                }
    except Exception:
        pass

    try:
        channel = strategies.detect_channel(candles)
        if channel:
            w40 = candles[-40:]
            highs = [c[2] for c in w40]
            lows  = [c[3] for c in w40]
            sh, sl_ = strategies.find_swing_points(highs, lows, window=3)
            rs, ri, _ = strategies.linreg(sh)
            ss, si_, _ = strategies.linreg(sl_)
            off = n - 40
            patterns["channel"] = {
                "type": channel["type"],
                "resistance_line": trendline_pts(rs, ri, 0, 39, off),
                "support_line":    trendline_pts(ss, si_, 0, 39, off),
                "resistance_level": channel["resistance"],
                "support_level":    channel["support"],
            }
    except Exception:
        pass

    return jsonify({"symbol": sym, "patterns": patterns})


def _ltp(symbol):
    sym = symbol.upper()
    if is_market_open():
        try:
            quotes = get_yfinance_quotes([sym])
            q = quotes.get(sym)
            if q and q.get("ltp"):
                return round(q["ltp"], 2)
        except Exception:
            pass
    candles = get_or_fetch_candles(sym)
    if candles:
        return round(candles[-1][4], 2)
    return None


def _closed_trade_record(trade, sell_price, pnl, exit_reason):
    return {
        **trade,
        "setup": trade.get("setup", ""),
        "notes": trade.get("notes", ""),
        "review": trade.get("review", ""),
        "tags": trade.get("tags", ""),
        "sell_price": round(sell_price, 2),
        "pnl": round(pnl, 2),
        "exit_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": "profit" if pnl >= 0 else "loss",
        "exit_reason": exit_reason,
    }


def _journal_summary(history):
    total = len(history)
    wins = [t for t in history if t.get("pnl", 0) >= 0]
    losses = [t for t in history if t.get("pnl", 0) < 0]
    gross_profit = round(sum(t.get("pnl", 0) for t in wins), 2)
    gross_loss = round(sum(t.get("pnl", 0) for t in losses), 2)
    net_pnl = round(gross_profit + gross_loss, 2)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 2) if total else 0,
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        "best_trade": round(max((t.get("pnl", 0) for t in history), default=0), 2),
        "worst_trade": round(min((t.get("pnl", 0) for t in history), default=0), 2),
    }


def _check_sl_target():
    still_open = []
    changed = False
    for t in STATE["paper_trades"]:
        price = _ltp(t["symbol"])
        if price is None:
            still_open.append(t)
            continue

        hit = None
        sell_price = price
        if t["stop_loss"] and price <= t["stop_loss"]:
            hit = "SL Hit"
            sell_price = t["stop_loss"]
        elif t["target"] and price >= t["target"]:
            hit = "Target Hit"
            sell_price = t["target"]

        if hit:
            pnl = (sell_price - t["buy_price"]) * t["quantity"]
            STATE["paper_balance"] += t["buy_price"] * t["quantity"] + pnl
            STATE["paper_balance"]  = round(STATE["paper_balance"], 2)
            STATE["paper_history"].append(_closed_trade_record(t, sell_price, pnl, hit))
            changed = True
        else:
            still_open.append(t)

    STATE["paper_trades"] = still_open
    if changed:
        _save_paper_state()


@app.route("/api/paper/portfolio")
def api_paper_portfolio():
    _check_sl_target()

    trades = []
    for t in STATE["paper_trades"]:
        price = _ltp(t["symbol"]) or t["buy_price"]
        pnl      = round((price - t["buy_price"]) * t["quantity"], 2)
        pnl_pct  = round((price - t["buy_price"]) / t["buy_price"] * 100, 2)
        invested = round(t["buy_price"] * t["quantity"], 2)
        sl_dist = round((price - t["stop_loss"]) / t["stop_loss"] * 100, 2) if t["stop_loss"] else None
        tgt_dist = round((t["target"] - price)   / price * 100, 2)          if t["target"]    else None
        trades.append({
            **t,
            "ltp":       price,
            "pnl":       pnl,
            "pnl_pct":   pnl_pct,
            "invested":  invested,
            "sl_dist":   sl_dist,
            "tgt_dist":  tgt_dist,
        })

    total_invested = sum(t["invested"] for t in trades)
    total_pnl      = sum(t["pnl"] for t in trades)
    portfolio_val  = round(STATE["paper_balance"] + total_invested + total_pnl, 2)

    return jsonify({
        "balance":       round(STATE["paper_balance"], 2),
        "initial":       STATE["paper_initial"],
        "portfolio_val": portfolio_val,
        "total_pnl":     round(total_pnl, 2),
        "total_invested": round(total_invested, 2),
        "open_trades":   trades,
        "history_count":  len(STATE["paper_history"]),
        "history":       list(reversed(STATE["paper_history"]))[:30],
    })


@app.route("/api/paper/buy", methods=["POST"])
def api_paper_buy():
    d = request.json or {}
    symbol     = d.get("symbol", "").upper().strip()
    quantity   = max(1, int(d.get("quantity", 1)))
    buy_price  = float(d.get("buy_price",  0))
    stop_loss  = float(d.get("stop_loss",  0)) or None
    target     = float(d.get("target",     0)) or None
    setup      = str(d.get("setup") or "").strip()
    notes      = str(d.get("notes") or "").strip()

    if not symbol or buy_price <= 0:
        return jsonify({"error": "Symbol and buy price are required"}), 400

    cost = round(buy_price * quantity, 2)
    if cost > STATE["paper_balance"]:
        return jsonify({"error": f"Insufficient balance. Need ₹{cost:,.2f}, have ₹{STATE['paper_balance']:,.2f}"}), 400

    trade_id = int(dt.datetime.now().timestamp() * 1000) % 1_000_000
    trade = {
        "id":          trade_id,
        "symbol":      symbol,
        "quantity":    quantity,
        "buy_price":   buy_price,
        "stop_loss":   stop_loss,
        "target":      target,
        "cost":        cost,
        "setup":       setup,
        "notes":       notes,
        "review":      "",
        "tags":        "",
        "entry_time":  dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    STATE["paper_trades"].append(trade)
    STATE["paper_balance"] = round(STATE["paper_balance"] - cost, 2)
    _save_paper_state()
    return jsonify({"success": True, "trade": trade, "balance": STATE["paper_balance"]})


@app.route("/api/paper/sell/<int:trade_id>", methods=["POST"])
def api_paper_sell(trade_id):
    trade = next((t for t in STATE["paper_trades"] if t["id"] == trade_id), None)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404

    d = request.json or {}
    price = _ltp(trade["symbol"]) or trade["buy_price"]
    sell_price = float(d.get("sell_price", price))

    pnl = round((sell_price - trade["buy_price"]) * trade["quantity"], 2)
    STATE["paper_balance"] = round(STATE["paper_balance"] + trade["buy_price"] * trade["quantity"] + pnl, 2)
    STATE["paper_trades"]  = [t for t in STATE["paper_trades"] if t["id"] != trade_id]
    STATE["paper_history"].append(_closed_trade_record(trade, sell_price, pnl, "Manual Exit"))
    _save_paper_state()
    return jsonify({"success": True, "pnl": pnl, "balance": STATE["paper_balance"]})


@app.route("/api/paper/journal")
def api_paper_journal():
    _check_sl_target()
    history = list(reversed(STATE["paper_history"]))
    return jsonify({
        "summary": _journal_summary(STATE["paper_history"]),
        "history": history,
    })


@app.route("/api/paper/journal/<int:trade_id>", methods=["PATCH"])
def api_paper_journal_update(trade_id):
    d = request.json or {}
    allowed = {"setup", "notes", "review", "tags"}
    for trade in STATE["paper_history"]:
        if trade.get("id") == trade_id:
            for key in allowed:
                if key in d:
                    trade[key] = str(d.get(key, "")).strip()
            _save_paper_state()
            return jsonify({"success": True, "trade": trade})
    return jsonify({"error": "Journal trade not found"}), 404


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    d = request.json or {}
    initial = float(d.get("balance", 100000))
    STATE["paper_balance"] = initial
    STATE["paper_initial"] = initial
    STATE["paper_trades"]  = []
    STATE["paper_history"] = []
    _save_paper_state()
    return jsonify({"success": True, "balance": initial})


if __name__ == "__main__":
    try:
        STATE["universe"] = load_nifty500_symbols()
        start_scanner_thread()
    except Exception as e:
        print(f"Error starting scanner on startup: {e}")
        
    print("\nOpen http://127.0.0.1:5000 in your browser to view the scanner dashboard.\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
