import json
import logging
import os
import time
import urllib.request
from datetime import datetime

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    HRFlowable,
    Image as RLImage,
    PageBreak,
)
from binance.um_futures import UMFutures

from dotenv import load_dotenv
from xai_sdk import Client
from xai_sdk.chat import system, user, file as xai_file


# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# ENV + CONFIG
# ──────────────────────────────────────────────
load_dotenv()
logger.info("Loading configuration from config.json")

try:
    with open("config.json", "r") as f:
        config: dict = json.load(f)
    logger.info(
        "Configuration loaded (api_key_present=%s, api_secret_present=%s)",
        bool(config.get("api_key")),
        bool(config.get("api_secret")),
    )
except json.JSONDecodeError as e:
    logger.critical("Invalid JSON in config file: %s", e)
    raise
except FileNotFoundError:
    logger.critical("Config file not found: config.json")
    raise

trading_pairs: list[str] = config.get("trading_pair", None)
if not trading_pairs or not isinstance(trading_pairs, list):
    logger.critical(
        "Invalid or missing 'trading_pair' in config. Expected list, got: %s",
        trading_pairs,
    )
    raise ValueError("Config must contain 'trading_pair' as a list")

logger.info("Trading pairs loaded: %s", ", ".join(trading_pairs))


# ──────────────────────────────────────────────
# CLIENTS
# ──────────────────────────────────────────────
logger.info("Initializing Binance UM Futures client")
client_binance: UMFutures = UMFutures(
    key=config.get("api_key"), secret=config.get("api_secret")
)
logger.info("Binance client initialized successfully")

logger.info("Initializing xAI client")
client_xai = Client(api_key=config.get("XAI_API_KEY"))
logger.info("xAI client initialized successfully")


# ──────────────────────────────────────────────
# RETRY + CACHE HELPERS
# ──────────────────────────────────────────────
def binance_call_with_retry(
    fn, *args, retries: int = 4, base_delay: float = 1.5, **kwargs
):
    """
    Call a Binance API function with exponential backoff retry.
    Handles rate-limits (429/418) and transient errors gracefully.
    """
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            is_rate_limit = (
                "429" in err_str or "418" in err_str or "Too many" in err_str
            )
            delay = base_delay * (2 ** (attempt - 1))  # 1.5s → 3s → 6s → 12s
            if attempt == retries:
                logger.error("Binance call failed after %s attempts: %s", retries, e)
                raise
            if is_rate_limit:
                logger.warning(
                    "Rate limited — waiting %.1fs before retry %s/%s",
                    delay * 2,
                    attempt,
                    retries,
                )
                time.sleep(delay * 2)
            else:
                logger.warning(
                    "Binance call failed (attempt %s/%s): %s — retrying in %.1fs",
                    attempt,
                    retries,
                    e,
                    delay,
                )
                time.sleep(delay)


_API_CACHE: dict = {}  # in-memory cache for this run


def cached_call(cache_key: str, fn, *args, **kwargs):
    """Cache an API result in memory for the duration of this run."""
    if cache_key not in _API_CACHE:
        _API_CACHE[cache_key] = binance_call_with_retry(fn, *args, **kwargs)
        logger.info("Cache MISS → fetched: %s", cache_key)
    else:
        logger.info("Cache HIT: %s", cache_key)
    return _API_CACHE[cache_key]


def save_cache_to_disk(symbol: str, run_date: str):
    """Save the full API cache to JSON for debugging / replay."""
    path = f"{symbol}_{run_date}_api_cache.json"
    try:
        with open(path, "w") as f:
            json.dump(_API_CACHE, f, indent=2, default=str)
        logger.info("API cache saved: %s", path)
    except Exception as e:
        logger.warning("Could not save cache: %s", e)


# ──────────────────────────────────────────────
# POSITION SIZE CALCULATOR
# ──────────────────────────────────────────────
def calculate_position_size(
    entry: float, atr: float, account_size: float = None, risk_pct: float = None
) -> dict:
    """
    Calculate suggested position size based on ATR stop distance.
    Uses config values if not overridden. Returns a dict of sizing metrics.
    """
    account_size = account_size or config.get("account_size", 1000.0)
    risk_pct = risk_pct or config.get("risk_per_trade_pct", 1.0)

    stop_distance = atr * 1.5  # 1.5× ATR stop
    stop_price = entry - stop_distance  # long-side stop
    risk_amount = account_size * (risk_pct / 100)
    contracts = round(risk_amount / stop_distance, 4) if stop_distance else 0
    notional = round(contracts * entry, 2)
    leverage_needed = round(notional / account_size, 1) if account_size else 0

    return {
        "account_size": account_size,
        "risk_pct": risk_pct,
        "risk_amount_usd": round(risk_amount, 2),
        "atr": round(atr, 4),
        "stop_distance": round(stop_distance, 4),
        "stop_price_long": round(stop_price, 4),
        "contracts": contracts,
        "notional_usd": notional,
        "leverage_needed": leverage_needed,
    }


# ──────────────────────────────────────────────
# L/S RATIO CHANGE DETECTOR
# ──────────────────────────────────────────────
def detect_ls_flip(ls_1h_data: list, lookback: int = 4) -> dict:
    """
    Detect fast flips in the long/short ratio over the last N 1h periods.
    A flip = ratio crosses 1.0 (majority switches sides) or moves > 0.15 in one period.
    Returns a dict with flip details and interpretation.
    """
    result = {"flip_detected": False, "direction": None, "magnitude": 0.0, "note": ""}
    if not ls_1h_data or len(ls_1h_data) < 2:
        return result
    try:
        recent = ls_1h_data[-min(lookback, len(ls_1h_data)) :]
        ratios = [float(x.get("longShortRatio", 1.0)) for x in recent]
        delta = ratios[-1] - ratios[0]
        magnitude = abs(delta)

        # Check for crossover (crossed 1.0)
        crossed = any(
            (ratios[i] < 1.0 and ratios[i + 1] >= 1.0)
            or (ratios[i] >= 1.0 and ratios[i + 1] < 1.0)
            for i in range(len(ratios) - 1)
        )

        if magnitude > 0.15 or crossed:
            result["flip_detected"] = True
            result["magnitude"] = round(magnitude, 3)
            if delta > 0:
                result["direction"] = "FLIPPING LONG"
                result["note"] = (
                    f"Top traders rapidly adding longs (+{round(delta, 3)} in {lookback}h) — momentum building"
                )
            else:
                result["direction"] = "FLIPPING SHORT"
                result["note"] = (
                    f"Top traders rapidly cutting longs ({round(delta, 3)} in {lookback}h) — caution"
                )
            if crossed:
                result["note"] += " [MAJORITY SIDE SWITCHED]"
    except Exception as e:
        logger.warning("L/S flip detection failed: %s", e)
    return result


# Multi-timeframe: 15m for scalp, 1h for swing context, 4h for HTF bias
TIMEFRAMES: list[str] = ["15m", "30m","1h", "4h"]

TF_TO_MINUTES: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}

KLINE_KEYS: list[str] = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "num_trades",
    "taker_base_volume",
    "taker_quote_volume",
    "ignore",
]

NUMERIC_FIELDS: set[str] = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_base_volume",
    "taker_quote_volume",
}

# Colour constants (used by chart)
C_CHART_BG = "#0D1117"
C_CHART_UP = "#00C896"
C_CHART_DOWN = "#FF4D4D"
C_EMA1 = "#F0B90B"
C_EMA2 = "#A78BFA"


# ──────────────────────────────────────────────
# TECHNICAL INDICATOR CALCULATIONS
# ──────────────────────────────────────────────
def calculate_ema(values: list[float], period: int) -> list[float]:
    ema, k = [], 2 / (period + 1)
    for i, price in enumerate(values):
        ema.append(price if i == 0 else price * k + ema[i - 1] * (1 - k))
    return ema


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """Returns the most recent RSI value."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_vwap(candles: list[dict]) -> float:
    """Returns VWAP for the provided candles."""
    cumulative_pv, cumulative_vol = 0.0, 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cumulative_pv += typical * c["volume"]
        cumulative_vol += c["volume"]
    return round(cumulative_pv / cumulative_vol, 4) if cumulative_vol else 0.0


def calculate_atr(candles: list[dict], period: int = 14) -> float:
    """Returns the most recent ATR value."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = (
            candles[i]["high"],
            candles[i]["low"],
            candles[i - 1]["close"],
        )
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(sum(trs[-period:]) / period, 4)


def calculate_volume_ratio(candles: list[dict], lookback: int = 20) -> float:
    """Current candle volume vs average of last N candles."""
    if len(candles) < lookback + 1:
        return 1.0
    recent_vols = [c["volume"] for c in candles[-lookback - 1 : -1]]
    avg_vol = sum(recent_vols) / len(recent_vols)
    current_vol = candles[-1]["volume"]
    return round(current_vol / avg_vol, 2) if avg_vol else 1.0


def get_indicators(candles: list[dict]) -> dict:
    """Compute all key indicators for a given set of candles."""
    closes = [c["close"] for c in candles]
    ema_50 = calculate_ema(closes, 50)[-1]
    ema_200 = calculate_ema(closes, 200)[-1]
    rsi = calculate_rsi(closes, 14)
    vwap = calculate_vwap(candles)
    atr = calculate_atr(candles, 14)
    vol_ratio = calculate_volume_ratio(candles, 20)
    current_price = closes[-1]

    ema50_dist = round(((current_price - ema_50) / ema_50) * 100, 2)
    ema200_dist = round(((current_price - ema_200) / ema_200) * 100, 2)

    return {
        "price": round(current_price, 4),
        "ema_50": round(ema_50, 4),
        "ema_200": round(ema_200, 4),
        "ema50_dist": ema50_dist,
        "ema200_dist": ema200_dist,
        "rsi_14": rsi,
        "vwap": vwap,
        "atr_14": atr,
        "vol_ratio": vol_ratio,
        "ema_cross": "BULLISH" if ema_50 > ema_200 else "BEARISH",
    }


# ──────────────────────────────────────────────
# BINANCE DATA FUNCTIONS
# ──────────────────────────────────────────────
def generate_limits(base_tf, base_count, timeframes):
    if base_tf not in TF_TO_MINUTES:
        raise ValueError(f"Unknown timeframe: {base_tf}")
    base_minutes = TF_TO_MINUTES[base_tf]
    total_window_minutes = base_minutes * base_count
    BINANCE_MAX_LIMIT = 1000
    limits = {}
    for tf in timeframes:
        if tf not in TF_TO_MINUTES:
            raise ValueError(f"Unknown timeframe: {tf}")
        calculated = int(total_window_minutes // TF_TO_MINUTES[tf])
        limits[tf] = min(calculated, BINANCE_MAX_LIMIT)
        logger.info(
            "Timeframe %s -> %s candles (calculated=%s)", tf, limits[tf], calculated
        )
    return limits


def fetch_futures_context(symbol):
    """
    Fetch the maximum available market context from Binance Futures.
    Organised into layers: price, derivatives, sentiment, flow, structure.
    """
    ctx = {}
    s = symbol  # shorthand

    # ════════════════════════════════════════════════
    #  LAYER 1 — PRICE & MARKET SNAPSHOT
    # ════════════════════════════════════════════════
    ctx["mark_price"] = cached_call(
        f"{s}_mark_price", client_binance.mark_price, symbol=s
    )
    ctx["ticker_24h"] = cached_call(
        f"{s}_ticker_24h", client_binance.ticker_24hr_price_change, symbol=s
    )
    ctx["ticker_price"] = cached_call(
        f"{s}_ticker_price", client_binance.ticker_price, symbol=s
    )
    ctx["book_ticker"] = cached_call(
        f"{s}_book_ticker", client_binance.book_ticker, symbol=s
    )

    # ════════════════════════════════════════════════
    #  LAYER 2 — OPEN INTEREST (multi-period)
    # ════════════════════════════════════════════════
    ctx["oi_current"] = cached_call(
        f"{s}_oi_current", client_binance.open_interest, symbol=s
    )
    ctx["oi_hist_15m"] = cached_call(
        f"{s}_oi_hist_15m",
        client_binance.open_interest_hist,
        symbol=s,
        period="15m",
        limit=16,
    )
    ctx["oi_hist_1h"] = cached_call(
        f"{s}_oi_hist_1h",
        client_binance.open_interest_hist,
        symbol=s,
        period="1h",
        limit=24,
    )
    ctx["oi_hist_4h"] = cached_call(
        f"{s}_oi_hist_4h",
        client_binance.open_interest_hist,
        symbol=s,
        period="4h",
        limit=42,
    )

    # ════════════════════════════════════════════════
    #  LAYER 3 — FUNDING RATE (extended history)
    # ════════════════════════════════════════════════
    ctx["funding"] = cached_call(
        f"{s}_funding", client_binance.funding_rate, symbol=s, limit=30
    )

    # ════════════════════════════════════════════════
    #  LAYER 4 — SENTIMENT RATIOS (multi-period)
    # ════════════════════════════════════════════════
    ctx["ls_top_position_15m"] = cached_call(
        f"{s}_ls_top_pos_15m",
        client_binance.top_long_short_position_ratio,
        symbol=s,
        period="15m",
        limit=16,
    )
    ctx["ls_top_account_15m"] = cached_call(
        f"{s}_ls_top_acc_15m",
        client_binance.top_long_short_account_ratio,
        symbol=s,
        period="15m",
        limit=16,
    )
    ctx["ls_global_15m"] = cached_call(
        f"{s}_ls_global_15m",
        client_binance.long_short_account_ratio,
        symbol=s,
        period="15m",
        limit=16,
    )
    ctx["ls_top_position_1h"] = cached_call(
        f"{s}_ls_top_pos_1h",
        client_binance.top_long_short_position_ratio,
        symbol=s,
        period="1h",
        limit=24,
    )
    ctx["ls_top_account_1h"] = cached_call(
        f"{s}_ls_top_acc_1h",
        client_binance.top_long_short_account_ratio,
        symbol=s,
        period="1h",
        limit=24,
    )
    ctx["ls_global_1h"] = cached_call(
        f"{s}_ls_global_1h",
        client_binance.long_short_account_ratio,
        symbol=s,
        period="1h",
        limit=24,
    )
    ctx["taker_ratio_15m"] = cached_call(
        f"{s}_taker_15m",
        client_binance.taker_long_short_ratio,
        symbol=s,
        period="15m",
        limit=16,
    )
    ctx["taker_ratio_1h"] = cached_call(
        f"{s}_taker_1h",
        client_binance.taker_long_short_ratio,
        symbol=s,
        period="1h",
        limit=24,
    )

    # L/S flip detection — fast changes in top trader positioning
    ctx["ls_flip"] = detect_ls_flip(ctx["ls_top_position_1h"], lookback=4)

    # ════════════════════════════════════════════════
    #  LAYER 5 — ORDER BOOK & RECENT FLOW
    # ════════════════════════════════════════════════
    ctx["order_book"] = cached_call(
        f"{s}_order_book", client_binance.depth, symbol=s, limit=50
    )
    ctx["agg_trades"] = cached_call(
        f"{s}_agg_trades", client_binance.agg_trades, symbol=s, limit=50
    )

    # ════════════════════════════════════════════════
    #  LAYER 6 — MARK PRICE KLINES (premium/discount)
    # ════════════════════════════════════════════════
    ctx["mark_klines_15m"] = cached_call(
        f"{s}_mark_klines_15m",
        client_binance.mark_price_klines,
        symbol=s,
        interval="15m",
        limit=48,
    )

    logger.info("Futures context fully fetched for %s (%s data layers)", symbol, 6)
    return ctx


def fetch_fear_greed(limit: int = 10) -> dict:
    """
    Fetch the last N days of the Crypto Fear & Greed Index from alternative.me.
    Free, no API key required. Returns a dict with processed values.
    Score: 0-24 Extreme Fear, 25-49 Fear, 50-74 Greed, 75-100 Extreme Greed.
    """
    url = f"https://api.alternative.me/fng/?limit={limit}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        data = raw.get("data", [])
        if not data:
            logger.warning("Fear & Greed API returned empty data")
            return {}

        # Data comes newest-first — reverse for chronological order
        data = list(reversed(data))

        scores = [int(d["value"]) for d in data]
        labels = [d["value_classification"] for d in data]
        latest = scores[-1]
        latest_label = labels[-1]
        prev = scores[-2] if len(scores) >= 2 else latest
        week_avg = round(sum(scores) / len(scores), 1)

        # Trend over the period
        delta = latest - scores[0]
        if delta > 10:
            trend = "RAPIDLY IMPROVING (market becoming greedy)"
        elif delta > 4:
            trend = "IMPROVING"
        elif delta < -10:
            trend = "RAPIDLY DETERIORATING (market becoming fearful)"
        elif delta < -4:
            trend = "DETERIORATING"
        else:
            trend = "STABLE"

        # Trading implication
        if latest <= 24:
            implication = "EXTREME FEAR — historically strong contrarian buy zone, capitulation likely"
        elif latest <= 49:
            implication = "FEAR — cautious market, dips may be buying opportunities"
        elif latest <= 74:
            implication = "GREED — momentum favours longs but watch for exhaustion"
        else:
            implication = (
                "EXTREME GREED — crowded market, high reversal risk, tighten stops"
            )

        logger.info(
            "Fear & Greed fetched: %s (%s), 10d avg: %s", latest, latest_label, week_avg
        )
        return {
            "latest_score": latest,
            "latest_label": latest_label,
            "previous_score": prev,
            "week_avg": week_avg,
            "trend": trend,
            "implication": implication,
            "history": list(zip(scores, labels)),  # [(score, label), ...]
        }

    except Exception as e:
        logger.warning("Fear & Greed fetch failed: %s", e)
        return {}


def format_klines(data):
    formatted = []
    for item in data:
        d = dict(zip(KLINE_KEYS, item))
        for k in NUMERIC_FIELDS:
            d[k] = float(d[k])
        formatted.append(d)
    return formatted


def get_all_klines(symbol: str) -> dict[str, list[dict]]:
    """Returns klines grouped by timeframe: {"15m": [...], "1h": [...], "4h": [...]}"""
    logger.info("Collecting multi-timeframe klines for symbol=%s", symbol)
    klines_by_tf: dict[str, list[dict]] = {}

    limits = generate_limits(base_tf="4h", base_count=100, timeframes=TIMEFRAMES)

    for tf, limit in limits.items():
        logger.info(
            "Fetching klines (symbol=%s, timeframe=%s, limit=%s)", symbol, tf, limit
        )
        try:
            raw = binance_call_with_retry(
                client_binance.klines, symbol=symbol, interval=tf, limit=limit
            )
        except Exception as e:
            logger.error("Failed to fetch klines: %s", e, exc_info=True)
            raise
        formatted = format_klines(raw)
        for c in formatted:
            c["tf"] = tf
        klines_by_tf[tf] = formatted
        logger.info("Collected %s candles for %s %s", len(formatted), symbol, tf)

    return klines_by_tf


def build_context_summary(
    symbol: str, ctx: dict, indicators_by_tf: dict, fear_greed: dict = None
) -> str:
    """
    Build a comprehensive plain-English context block for the raw PDF.
    Gives Grok a full market dashboard before it touches the candle data.
    Covers 6 layers: price snapshot, OI multi-period, funding history,
    sentiment ratios multi-period, order book + flow, mark price premium.
    """

    # ── Helper: safe float ───────────────────────────
    def f(d, key, default=0.0):
        try:
            return float(d.get(key, default))
        except (TypeError, ValueError):
            return default

    def latest_ratio(data, key="longShortRatio"):
        if data and isinstance(data, list):
            try:
                return float(data[-1].get(key, 0))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def ratio_trend(data, key="longShortRatio", periods=8):
        """Returns 'RISING', 'FALLING', or 'FLAT' over last N periods."""
        if not data or len(data) < 2:
            return "UNKNOWN"
        slice_ = data[-min(periods, len(data)) :]
        try:
            vals = [float(x.get(key, 0)) for x in slice_]
            delta = vals[-1] - vals[0]
            if delta > 0.05:
                return "RISING"
            if delta < -0.05:
                return "FALLING"
            return "FLAT"
        except Exception:
            return "UNKNOWN"

    def oi_change_pct(hist):
        """% change from first to last entry in an OI history list."""
        if not hist or len(hist) < 2:
            return 0.0
        try:
            start = float(hist[0].get("sumOpenInterest", 0))
            end = float(hist[-1].get("sumOpenInterest", 0))
            return round(((end - start) / start) * 100, 2) if start else 0.0
        except Exception:
            return 0.0

    # ════════════════════════════════════════════════
    #  LAYER 1 — PRICE SNAPSHOT
    # ════════════════════════════════════════════════
    ticker = ctx.get("ticker_24h", {})
    mark = ctx.get("mark_price", {})
    book = ctx.get("book_ticker", {})
    spot_px = ctx.get("ticker_price", {})

    price_24h = f(ticker, "lastPrice")
    change_24h_p = f(ticker, "priceChangePercent")
    high_24h = f(ticker, "highPrice")
    low_24h = f(ticker, "lowPrice")
    volume_24h = f(ticker, "volume")
    quote_vol_24h = f(ticker, "quoteVolume")
    trade_count = int(ticker.get("count", 0))
    weighted_avg = f(ticker, "weightedAvgPrice")
    mark_px = f(mark, "markPrice")
    index_px = f(mark, "indexPrice")
    next_funding = f(mark, "lastFundingRate") * 100
    next_funding_time = mark.get("nextFundingTime", "N/A")
    spot_price = f(spot_px, "price")
    best_bid = f(book, "bidPrice")
    best_ask = f(book, "askPrice")
    bid_qty = f(book, "bidQty")
    ask_qty = f(book, "askQty")
    spread_pct = round(((best_ask - best_bid) / best_bid) * 100, 4) if best_bid else 0
    mark_vs_index = round(mark_px - index_px, 4) if index_px else 0
    mark_premium = round((mark_vs_index / index_px) * 100, 4) if index_px else 0

    # ════════════════════════════════════════════════
    #  LAYER 2 — OPEN INTEREST (multi-period analysis)
    # ════════════════════════════════════════════════
    oi_cur = ctx.get("oi_current", {})
    oi_value = f(oi_cur, "openInterest")

    oi_15m = ctx.get("oi_hist_15m", [])
    oi_1h = ctx.get("oi_hist_1h", [])
    oi_4h = ctx.get("oi_hist_4h", [])

    oi_chg_4h = oi_change_pct(oi_15m)  # last 4h via 15m buckets
    oi_chg_24h = oi_change_pct(oi_1h)  # last 24h via 1h buckets
    oi_chg_7d = oi_change_pct(oi_4h)  # last 7d via 4h buckets

    # OI vs price divergence (is OI growing while price falls = bearish buildup?)
    oi_latest_val = float(oi_1h[-1].get("sumOpenInterest", 0)) if oi_1h else 0
    oi_prev_val = (
        float(oi_1h[-4].get("sumOpenInterest", 0)) if len(oi_1h) >= 4 else oi_latest_val
    )
    oi_4h_direction = "UP" if oi_latest_val >= oi_prev_val else "DOWN"

    price_direction_4h = "UP" if change_24h_p >= 0 else "DOWN"

    oi_price_divergence = "NONE"
    if oi_4h_direction == "UP" and price_direction_4h == "DOWN":
        oi_price_divergence = "BEARISH (OI rising, price falling — shorts being added)"
    elif oi_4h_direction == "DOWN" and price_direction_4h == "UP":
        oi_price_divergence = (
            "BULLISH (OI falling, price rising — short squeeze / profit taking)"
        )
    elif oi_4h_direction == "UP" and price_direction_4h == "UP":
        oi_price_divergence = (
            "BULLISH CONFIRMATION (OI + price both rising — new longs entering)"
        )
    elif oi_4h_direction == "DOWN" and price_direction_4h == "DOWN":
        oi_price_divergence = (
            "BEARISH CONFIRMATION (OI + price both falling — longs capitulating)"
        )

    # ════════════════════════════════════════════════
    #  LAYER 3 — FUNDING RATE (extended analysis)
    # ════════════════════════════════════════════════
    funding_list = ctx.get("funding", [])
    funding_rates = [float(x.get("fundingRate", 0)) * 100 for x in funding_list]

    avg_funding = (
        round(sum(funding_rates) / len(funding_rates), 4) if funding_rates else 0
    )
    last_funding = funding_rates[-1] if funding_rates else 0
    max_funding = round(max(funding_rates), 4) if funding_rates else 0
    min_funding = round(min(funding_rates), 4) if funding_rates else 0
    positive_pct = (
        round(sum(1 for r in funding_rates if r > 0) / len(funding_rates) * 100)
        if funding_rates
        else 0
    )

    funding_bias = "NEUTRAL"
    if avg_funding > 0.03:
        funding_bias = "STRONGLY LONG BIASED — extreme crowding, high squeeze risk"
    elif avg_funding > 0.01:
        funding_bias = "LONGS PAYING — bullish skew, moderate crowded long risk"
    elif avg_funding < -0.03:
        funding_bias = (
            "STRONGLY SHORT BIASED — extreme negative funding, long squeeze risk"
        )
    elif avg_funding < -0.01:
        funding_bias = "SHORTS PAYING — bearish skew, potential long squeeze"

    funding_trend = "STABLE"
    if len(funding_rates) >= 6:
        recent_avg = sum(funding_rates[-3:]) / 3
        older_avg = sum(funding_rates[-6:-3]) / 3
        delta = recent_avg - older_avg
        if delta > 0.005:
            funding_trend = "ESCALATING (longs increasingly crowded)"
        elif delta < -0.005:
            funding_trend = "DECLINING (funding pressure easing)"

    # ════════════════════════════════════════════════
    #  LAYER 4 — SENTIMENT RATIOS (multi-period)
    # ════════════════════════════════════════════════
    ls_top_pos_now = latest_ratio(ctx.get("ls_top_position_15m", []))
    ls_top_acc_now = latest_ratio(ctx.get("ls_top_account_15m", []))
    ls_global_now = latest_ratio(ctx.get("ls_global_15m", []))
    taker_now = latest_ratio(ctx.get("taker_ratio_15m", []), key="buySellRatio")

    ls_top_pos_trend = ratio_trend(ctx.get("ls_top_position_1h", []))
    ls_top_acc_trend = ratio_trend(ctx.get("ls_top_account_1h", []))
    ls_global_trend = ratio_trend(ctx.get("ls_global_1h", []))
    taker_trend = ratio_trend(ctx.get("taker_ratio_1h", []), key="buySellRatio")

    sm_vs_retail = "ALIGNED"
    if ls_top_pos_now > 1.1 and ls_global_now < 0.9:
        sm_vs_retail = "SMART MONEY LONG / RETAIL SHORT — possible squeeze setup"
    elif ls_top_pos_now < 0.9 and ls_global_now > 1.1:
        sm_vs_retail = "SMART MONEY SHORT / RETAIL LONG — potential dump setup"
    elif ls_top_pos_now > 1.0 and ls_global_now > 1.0:
        sm_vs_retail = "BOTH LONG — crowded, watch for reversal"
    elif ls_top_pos_now < 1.0 and ls_global_now < 1.0:
        sm_vs_retail = "BOTH SHORT — crowded short, watch for squeeze"

    # ════════════════════════════════════════════════
    #  LAYER 5 — ORDER BOOK + RECENT TRADE FLOW
    # ════════════════════════════════════════════════
    ob = ctx.get("order_book", {})

    bid_depth_10 = sum(float(b[1]) for b in ob.get("bids", [])[:10])
    ask_depth_10 = sum(float(a[1]) for a in ob.get("asks", [])[:10])
    ob_ratio_10 = round(bid_depth_10 / ask_depth_10, 2) if ask_depth_10 else 0

    bid_depth_50 = sum(float(b[1]) for b in ob.get("bids", [])[:50])
    ask_depth_50 = sum(float(a[1]) for a in ob.get("asks", [])[:50])
    ob_ratio_50 = round(bid_depth_50 / ask_depth_50, 2) if ask_depth_50 else 0

    ob_bias = (
        "BID HEAVY (buy pressure)"
        if ob_ratio_10 > 1.2
        else "ASK HEAVY (sell pressure)"
        if ob_ratio_10 < 0.8
        else "BALANCED"
    )

    biggest_bid = max(
        ob.get("bids", [[0, 0]]), key=lambda x: float(x[1]), default=[0, 0]
    )
    biggest_ask = max(
        ob.get("asks", [[0, 0]]), key=lambda x: float(x[1]), default=[0, 0]
    )

    agg = ctx.get("agg_trades", [])
    buy_vol = sum(float(t.get("q", 0)) for t in agg if not t.get("m", True))
    sell_vol = sum(float(t.get("q", 0)) for t in agg if t.get("m", True))
    total_vol = buy_vol + sell_vol
    buy_pct = round((buy_vol / total_vol) * 100, 1) if total_vol else 50.0
    flow_bias = (
        "AGGRESSIVE BUYERS"
        if buy_pct > 55
        else "AGGRESSIVE SELLERS"
        if buy_pct < 45
        else "BALANCED FLOW"
    )

    # ════════════════════════════════════════════════
    #  LAYER 6 — MARK PRICE KLINES (premium analysis)
    # ════════════════════════════════════════════════
    mk = ctx.get("mark_klines_15m", [])
    mark_premium_avg = 0.0
    if mk and len(mk) >= 4:
        try:
            recent_mark_closes = [float(k[4]) for k in mk[-4:]]
            mark_premium_avg = (
                round(sum(recent_mark_closes) / len(recent_mark_closes) - index_px, 4)
                if index_px
                else 0.0
            )
        except Exception:
            pass

    # ════════════════════════════════════════════════
    #  BUILD OUTPUT STRING
    # ════════════════════════════════════════════════
    lines = [
        "=" * 65,
        "  FULL MARKET CONTEXT DASHBOARD  —  " + symbol,
        "=" * 65,
        "",
        "  ── [1] PRICE SNAPSHOT ──────────────────────────────────",
        f"  Futures Price  : {price_24h}  |  Spot: {spot_price}  |  Mark: {mark_px}",
        f"  Index Price    : {index_px}  |  Mark Premium: {mark_premium:+.4f}% vs index",
        f"  24h Change     : {'+' if change_24h_p >= 0 else ''}{change_24h_p}%  |  VWAP (24h): {weighted_avg}",
        f"  24h High/Low   : {high_24h} / {low_24h}  (range: {round(high_24h - low_24h, 2)})",
        f"  24h Volume     : {volume_24h:,.0f} contracts  |  Quote: ${quote_vol_24h:,.0f}",
        f"  24h Trade Count: {trade_count:,}",
        f"  Best Bid/Ask   : {best_bid} ({bid_qty}) / {best_ask} ({ask_qty})  (spread: {spread_pct}%)",
        f"  Next Funding   : {next_funding:+.4f}%  at {next_funding_time}",
        "",
        "  ── [2] OPEN INTEREST ───────────────────────────────────",
        f"  Current OI     : {oi_value:,.2f} contracts",
        f"  OI Change 4h   : {'+' if oi_chg_4h >= 0 else ''}{oi_chg_4h}%",
        f"  OI Change 24h  : {'+' if oi_chg_24h >= 0 else ''}{oi_chg_24h}%",
        f"  OI Change 7d   : {'+' if oi_chg_7d >= 0 else ''}{oi_chg_7d}%",
        f"  OI/Price Signal: {oi_price_divergence}",
        "",
        "  ── [3] FUNDING RATE (last 30 periods ~10 days) ─────────",
        f"  Average Rate   : {avg_funding:+.4f}%  →  {funding_bias}",
        f"  Last Rate      : {last_funding:+.4f}%  |  Trend: {funding_trend}",
        f"  Range (30p)    : {min_funding:+.4f}% to {max_funding:+.4f}%",
        f"  Positive %     : {positive_pct}% of last 30 periods were positive (longs paid)",
        "",
        "  ── [4] SENTIMENT RATIOS ────────────────────────────────",
        f"  Top Trader L/S (Position) : {ls_top_pos_now}  trend: {ls_top_pos_trend}",
        f"  Top Trader L/S (Account)  : {ls_top_acc_now}  trend: {ls_top_acc_trend}",
        f"  Global L/S (Retail)       : {ls_global_now}  trend: {ls_global_trend}",
        f"  Taker Buy/Sell Ratio (15m): {taker_now}  trend: {taker_trend}",
        f"  Smart Money vs Retail     : {sm_vs_retail}",
    ]

    ls_flip = ctx.get("ls_flip", {})
    if ls_flip.get("flip_detected"):
        lines += [
            f"  ⚠ L/S FLIP ALERT          : {ls_flip['direction']}",
            f"    {ls_flip['note']}",
        ]

    lines += [
        "",
        "  ── [5] ORDER BOOK & RECENT FLOW ────────────────────────",
        f"  OB Pressure (top 10)  : {ob_bias}  (ratio: {ob_ratio_10})",
        f"  OB Pressure (top 50)  : ratio {ob_ratio_50}  (wider market depth)",
        f"  Biggest Bid Wall      : {biggest_bid[0]} @ {float(biggest_bid[1]):,.2f} contracts",
        f"  Biggest Ask Wall      : {biggest_ask[0]} @ {float(biggest_ask[1]):,.2f} contracts",
        f"  Recent Trade Flow     : {flow_bias}  ({buy_pct}% buy-initiated of last 50 trades)",
        "",
        "  ── [6] MARK PRICE PREMIUM (last 4 × 15m) ──────────────",
        f"  Avg Mark Premium vs Index : {mark_premium_avg:+.4f}",
        f"  (positive = futures trading above spot index — bullish premium)",
        "",
        "  ── [7] FEAR & GREED INDEX (market sentiment) ───────────",
    ]

    if fear_greed:
        fg_history_str = "  ".join(
            f"{s}({l[:2]})" for s, l in fear_greed.get("history", [])
        )
        lines += [
            f"  Current Score  : {fear_greed['latest_score']} — {fear_greed['latest_label']}",
            f"  Previous Day   : {fear_greed['previous_score']}  |  10-Day Avg: {fear_greed['week_avg']}",
            f"  Trend (10d)    : {fear_greed['trend']}",
            f"  Implication    : {fear_greed['implication']}",
            f"  10-Day History : {fg_history_str}",
            f"  Scale          : 0-24=Extreme Fear  25-49=Fear  50-74=Greed  75-100=Extreme Greed",
        ]
    else:
        lines.append("  [Fear & Greed data unavailable]")

    lines += [
        "",
        "  ── [8] TECHNICAL INDICATORS BY TIMEFRAME ───────────────",
    ]

    for tf in TIMEFRAMES:
        ind = indicators_by_tf.get(tf, {})
        if not ind:
            continue
        lines += [
            f"  [{tf}]",
            f"    Price          : {ind['price']}",
            f"    EMA50          : {ind['ema_50']}  ({ind['ema50_dist']:+.2f}% from price)",
            f"    EMA200         : {ind['ema_200']}  ({ind['ema200_dist']:+.2f}% from price)",
            f"    EMA Cross      : {ind['ema_cross']}",
            f"    RSI (14)       : {ind['rsi_14']}  "
            f"({'OVERSOLD' if ind['rsi_14'] < 30 else 'OVERBOUGHT' if ind['rsi_14'] > 70 else 'NEUTRAL'})",
            f"    VWAP           : {ind['vwap']}",
            f"    ATR (14)       : {ind['atr_14']}  (volatility unit for stop sizing)",
            f"    Volume Ratio   : {ind['vol_ratio']}x avg  "
            f"({'HIGH' if ind['vol_ratio'] > 1.5 else 'LOW' if ind['vol_ratio'] < 0.6 else 'NORMAL'})",
        ]

    ind_15m = indicators_by_tf.get("15m", {})
    if ind_15m and ind_15m.get("atr_14") and ind_15m.get("price"):
        sizing = calculate_position_size(
            entry=ind_15m["price"],
            atr=ind_15m["atr_14"],
        )
        lines += [
            "",
            "  ── [9] SUGGESTED POSITION SIZING (1.5× ATR stop) ──────",
            f"  Account Size   : ${sizing['account_size']:,.0f}  |  Risk per trade: {sizing['risk_pct']}%  (${sizing['risk_amount_usd']})",
            f"  ATR (15m)      : {sizing['atr']}  →  Stop distance: {sizing['stop_distance']}",
            f"  Stop Price     : {sizing['stop_price_long']}  (long-side, adjust for short)",
            f"  Contracts      : {sizing['contracts']}  (notional: ${sizing['notional_usd']:,})",
            f"  Leverage needed: {sizing['leverage_needed']}×  (vs your account size)",
            f"  NOTE: Adjust account_size and risk_per_trade_pct in config.json to customise.",
        ]

    lines += [
        "",
        "=" * 65,
        "  END OF CONTEXT DASHBOARD",
        "=" * 65,
        "",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────
# CHART HELPERS
# ──────────────────────────────────────────────
def _save_fig(fig, filename: str) -> str:
    plt.savefig(filename, dpi=150, facecolor=C_CHART_BG, bbox_inches="tight")
    plt.close(fig)
    return filename


def _time_labels(sorted_data: list, n: int = 8) -> tuple[list, list]:
    timestamps = [
        datetime.utcfromtimestamp(c["open_time"] / 1000).strftime("%m-%d %H:%M")
        for c in sorted_data
    ]
    every = max(1, len(sorted_data) // n)
    positions = list(range(0, len(sorted_data), every))
    labels = [timestamps[i] for i in positions]
    return positions, labels


def _style_ax(ax, title: str, ylabel: str = "Price (USDT)"):
    ax.set_facecolor(C_CHART_BG)
    ax.set_title(title, color="white", fontsize=10, pad=6)
    ax.set_ylabel(ylabel, color="#888888", fontsize=8)
    ax.tick_params(colors="#888888", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#333344")


# ── CHART 1 — Multi-TF Candlestick ──────────────────────────────────────────
def plot_klines_image(klines_by_tf: dict, symbol: str, filename: str) -> str | None:
    """3-panel candlestick: 4h / 1h / 15m with EMA 50 + 200."""
    logger.info("Chart 1: multi-TF candlestick for %s", symbol)
    tf_order = ["4h", "1h", "15m"]
    height_ratios = [1, 1, 1.4]
    fig = plt.figure(figsize=(16, 10))
    fig.patch.set_facecolor(C_CHART_BG)
    gs = gridspec.GridSpec(3, 1, figure=fig, hspace=0.45, height_ratios=height_ratios)

    for idx, tf in enumerate(tf_order):
        data = klines_by_tf.get(tf, [])
        if not data:
            continue
        sd = sorted(data, key=lambda x: x["open_time"])
        opens = [c["open"] for c in sd]
        highs = [c["high"] for c in sd]
        lows = [c["low"] for c in sd]
        closes = [c["close"] for c in sd]
        ema50 = calculate_ema(closes, 50)
        ema200 = calculate_ema(closes, 200)
        tpos, tlabels = _time_labels(sd)

        ax = fig.add_subplot(gs[idx])
        _style_ax(ax, f"{symbol}  |  {tf}  ({len(sd)} candles)")
        for i in range(len(sd)):
            c = C_CHART_UP if closes[i] >= opens[i] else C_CHART_DOWN
            ax.plot([i, i], [lows[i], highs[i]], linewidth=0.6, color=c)
            ax.plot([i, i], [opens[i], closes[i]], linewidth=2.2, color=c)
        ax.plot(ema50, linewidth=1.5, linestyle="-", color=C_EMA1, label="EMA 50")
        ax.plot(ema200, linewidth=1.5, linestyle="--", color=C_EMA2, label="EMA 200")
        ax.set_xticks(tpos)
        ax.set_xticklabels(tlabels, color="#888888", fontsize=6, rotation=15)
        ax.legend(frameon=False, labelcolor="white", fontsize=9, loc="upper left")

    return _save_fig(fig, filename)


# ── CHART 2 — 15m Candlestick + RSI panel ───────────────────────────────────
def plot_rsi_chart(klines_by_tf: dict, symbol: str, filename: str) -> str | None:
    """15m price + RSI(14) sub-panel. Highlights divergence visually."""
    logger.info("Chart 2: 15m + RSI for %s", symbol)
    data = klines_by_tf.get("15m", [])
    if not data:
        return None
    sd = sorted(data, key=lambda x: x["open_time"])
    opens = [c["open"] for c in sd]
    highs = [c["high"] for c in sd]
    lows = [c["low"] for c in sd]
    closes = [c["close"] for c in sd]
    ema50 = calculate_ema(closes, 50)

    rsi_series = []
    for i in range(len(closes)):
        rsi_series.append(calculate_rsi(closes[: i + 1], 14))

    tpos, tlabels = _time_labels(sd)
    xs = list(range(len(sd)))

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 8), gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12}
    )
    fig.patch.set_facecolor(C_CHART_BG)

    _style_ax(ax1, f"{symbol}  |  15m  — Price + RSI(14)")
    for i in range(len(sd)):
        c = C_CHART_UP if closes[i] >= opens[i] else C_CHART_DOWN
        ax1.plot([i, i], [lows[i], highs[i]], linewidth=0.6, color=c)
        ax1.plot([i, i], [opens[i], closes[i]], linewidth=2.0, color=c)
    ax1.plot(ema50, linewidth=1.4, color=C_EMA1, label="EMA 50")
    ax1.set_xticks([])
    ax1.legend(frameon=False, labelcolor="white", fontsize=8)

    _style_ax(ax2, "", ylabel="RSI")
    ax2.plot(xs, rsi_series, linewidth=1.4, color="#00BFFF")
    ax2.axhline(70, color=C_CHART_DOWN, linewidth=0.8, linestyle="--", alpha=0.7)
    ax2.axhline(30, color=C_CHART_UP, linewidth=0.8, linestyle="--", alpha=0.7)
    ax2.axhline(50, color="#555566", linewidth=0.5, linestyle=":")
    ax2.fill_between(
        xs,
        rsi_series,
        70,
        where=[r > 70 for r in rsi_series],
        color=C_CHART_DOWN,
        alpha=0.15,
    )
    ax2.fill_between(
        xs,
        rsi_series,
        30,
        where=[r < 30 for r in rsi_series],
        color=C_CHART_UP,
        alpha=0.15,
    )
    ax2.set_ylim(0, 100)
    ax2.set_xticks(tpos)
    ax2.set_xticklabels(tlabels, color="#888888", fontsize=6, rotation=15)
    ax2.text(
        len(xs) - 1,
        rsi_series[-1] + 2,
        f"{rsi_series[-1]:.1f}",
        color="white",
        fontsize=8,
        ha="right",
    )

    return _save_fig(fig, filename)


# ── CHART 3 — OI + Price overlay (1h) ───────────────────────────────────────
def plot_oi_price_chart(
    klines_by_tf: dict, ctx: dict, symbol: str, filename: str
) -> str | None:
    """1h price vs OI history overlay. Divergence = key signal."""
    logger.info("Chart 3: OI + Price overlay for %s", symbol)
    oi_hist = ctx.get("oi_hist_1h", [])
    data_1h = klines_by_tf.get("1h", [])
    if not oi_hist or not data_1h:
        return None

    sd_1h = sorted(data_1h, key=lambda x: x["open_time"])
    closes = [c["close"] for c in sd_1h]
    oi_vals = [float(o.get("sumOpenInterest", 0)) for o in oi_hist]
    oi_ts = [int(o.get("timestamp", 0)) for o in oi_hist]

    price_ts = [c["open_time"] for c in sd_1h]
    aligned_oi = []
    for pt in price_ts:
        closest = (
            min(range(len(oi_ts)), key=lambda i: abs(oi_ts[i] - pt)) if oi_ts else 0
        )
        aligned_oi.append(oi_vals[closest] if oi_ts else 0)

    tpos, tlabels = _time_labels(sd_1h)
    xs = list(range(len(sd_1h)))

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 8), gridspec_kw={"height_ratios": [2, 1], "hspace": 0.12}
    )
    fig.patch.set_facecolor(C_CHART_BG)

    _style_ax(ax1, f"{symbol}  |  1h Price vs Open Interest")
    for i in range(len(sd_1h)):
        c = C_CHART_UP if closes[i] >= sd_1h[i]["open"] else C_CHART_DOWN
        ax1.plot([i, i], [sd_1h[i]["low"], sd_1h[i]["high"]], linewidth=0.6, color=c)
        ax1.plot([i, i], [sd_1h[i]["open"], closes[i]], linewidth=2.0, color=c)
    ax1.set_xticks([])

    _style_ax(ax2, "", ylabel="Open Interest")
    oi_colors = []
    for i in range(len(aligned_oi)):
        prev = aligned_oi[i - 1] if i > 0 else aligned_oi[i]
        oi_colors.append(C_CHART_UP if aligned_oi[i] >= prev else C_CHART_DOWN)
    ax2.bar(xs, aligned_oi, color=oi_colors, alpha=0.8, width=0.8)
    ax2.set_xticks(tpos)
    ax2.set_xticklabels(tlabels, color="#888888", fontsize=6, rotation=15)

    if len(closes) >= 4 and len(aligned_oi) >= 4:
        price_dir = "UP" if closes[-1] > closes[-4] else "DOWN"
        oi_dir = "UP" if aligned_oi[-1] > aligned_oi[-4] else "DOWN"
        if price_dir != oi_dir:
            label = "⚠ DIVERGENCE" if price_dir == "UP" else "⚠ BEARISH BUILDUP"
            ax1.text(
                0.99,
                0.95,
                label,
                transform=ax1.transAxes,
                color=C_CHART_DOWN,
                fontsize=10,
                ha="right",
                va="top",
                fontweight="bold",
            )

    return _save_fig(fig, filename)


# ── CHART 4 — Funding Rate history bar chart ────────────────────────────────
def plot_funding_chart(ctx: dict, symbol: str, filename: str) -> str | None:
    """Bar chart of last 30 funding rate periods, coloured green/red."""
    logger.info("Chart 4: Funding rate history for %s", symbol)
    funding = ctx.get("funding", [])
    if not funding:
        return None

    rates = [float(f.get("fundingRate", 0)) * 100 for f in funding]
    times = [
        datetime.utcfromtimestamp(int(f.get("fundingTime", 0)) / 1000).strftime(
            "%m-%d %H:%M"
        )
        for f in funding
    ]
    xs = list(range(len(rates)))
    bar_colors = [C_CHART_UP if r >= 0 else C_CHART_DOWN for r in rates]

    fig, ax = plt.subplots(figsize=(16, 5))
    fig.patch.set_facecolor(C_CHART_BG)
    _style_ax(
        ax,
        f"{symbol}  |  Funding Rate History (last {len(rates)} periods)",
        ylabel="Rate (%)",
    )

    ax.bar(xs, rates, color=bar_colors, alpha=0.85, width=0.7)
    ax.axhline(0, color="#555566", linewidth=0.8, linestyle="-")
    ax.axhline(
        0.01,
        color=C_CHART_DOWN,
        linewidth=0.6,
        linestyle="--",
        alpha=0.5,
        label="+0.01% (longs crowded)",
    )
    ax.axhline(
        -0.01,
        color=C_CHART_UP,
        linewidth=0.6,
        linestyle="--",
        alpha=0.5,
        label="-0.01% (shorts crowded)",
    )

    avg = sum(rates) / len(rates)
    ax.axhline(
        avg, color="#F0B90B", linewidth=1.0, linestyle=":", label=f"Avg: {avg:+.4f}%"
    )

    tick_every = max(1, len(xs) // 10)
    ax.set_xticks(xs[::tick_every])
    ax.set_xticklabels(times[::tick_every], color="#888888", fontsize=6, rotation=25)
    ax.legend(frameon=False, labelcolor="white", fontsize=8)
    ax.text(
        xs[-1],
        rates[-1] + (0.002 if rates[-1] >= 0 else -0.002),
        f"{rates[-1]:+.4f}%",
        color="white",
        fontsize=8,
        ha="center",
    )

    return _save_fig(fig, filename)


# ── CHART 5 — L/S Ratio comparison (top traders vs retail) ──────────────────
def plot_ls_ratio_chart(ctx: dict, symbol: str, filename: str) -> str | None:
    """Line chart: top trader position ratio vs global retail ratio (1h)."""
    logger.info("Chart 5: L/S ratio comparison for %s", symbol)
    top_pos = ctx.get("ls_top_position_1h", [])
    global_ = ctx.get("ls_global_1h", [])
    if not top_pos or not global_:
        return None

    top_vals = [float(x.get("longShortRatio", 1)) for x in top_pos]
    global_vals = [float(x.get("longShortRatio", 1)) for x in global_]
    times_top = [
        datetime.utcfromtimestamp(int(x.get("timestamp", 0)) / 1000).strftime(
            "%m-%d %H:%M"
        )
        for x in top_pos
    ]

    n = min(len(top_vals), len(global_vals))
    xs = list(range(n))
    top_vals = top_vals[-n:]
    global_vals = global_vals[-n:]
    times_top = times_top[-n:]

    fig, ax = plt.subplots(figsize=(16, 5))
    fig.patch.set_facecolor(C_CHART_BG)
    _style_ax(
        ax,
        f"{symbol}  |  Long/Short Ratio — Smart Money vs Retail (1h)",
        ylabel="L/S Ratio",
    )

    ax.plot(
        xs, top_vals, linewidth=1.8, color="#00BFFF", label="Top Traders (Position)"
    )
    ax.plot(xs, global_vals, linewidth=1.8, color=C_EMA1, label="Global Retail")
    ax.axhline(
        1.0, color="#555566", linewidth=0.8, linestyle="--", label="Neutral (1.0)"
    )

    for i in range(n):
        if top_vals[i] > 1.0 and global_vals[i] < 1.0:
            ax.axvspan(i - 0.5, i + 0.5, color=C_CHART_UP, alpha=0.08)
        elif top_vals[i] < 1.0 and global_vals[i] > 1.0:
            ax.axvspan(i - 0.5, i + 0.5, color=C_CHART_DOWN, alpha=0.08)

    tick_every = max(1, n // 8)
    ax.set_xticks(xs[::tick_every])
    ax.set_xticklabels(
        times_top[::tick_every], color="#888888", fontsize=6, rotation=15
    )
    ax.legend(frameon=False, labelcolor="white", fontsize=9)
    ax.text(
        n - 1,
        top_vals[-1],
        f" {top_vals[-1]:.2f}",
        color="#00BFFF",
        fontsize=8,
        va="center",
    )
    ax.text(
        n - 1,
        global_vals[-1],
        f" {global_vals[-1]:.2f}",
        color=C_EMA1,
        fontsize=8,
        va="center",
    )

    return _save_fig(fig, filename)


# ── CHART 6 — Order Book Depth chart ────────────────────────────────────────
def plot_orderbook_chart(ctx: dict, symbol: str, filename: str) -> str | None:
    """Stacked bid/ask depth — shows walls visually around current price."""
    logger.info("Chart 6: Order book depth for %s", symbol)
    ob = ctx.get("order_book", {})
    if not ob:
        return None

    bids = ob.get("bids", [])[:30]
    asks = ob.get("asks", [])[:30]
    if not bids or not asks:
        return None

    bid_prices = [float(b[0]) for b in reversed(bids)]
    bid_sizes = [float(b[1]) for b in reversed(bids)]
    ask_prices = [float(a[0]) for a in asks]
    ask_sizes = [float(a[1]) for a in asks]

    cum_bids = []
    running = 0
    for s in reversed(bid_sizes):
        running += s
        cum_bids.insert(0, running)

    cum_asks = []
    running = 0
    for s in ask_sizes:
        running += s
        cum_asks.append(running)

    mid_price = (bid_prices[-1] + ask_prices[0]) / 2

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6), gridspec_kw={"wspace": 0.05})
    fig.patch.set_facecolor(C_CHART_BG)
    fig.suptitle(
        f"{symbol}  |  Order Book Depth  (mid: {mid_price:,.2f})",
        color="white",
        fontsize=11,
    )

    _style_ax(ax1, "Raw Depth — Bids & Asks", ylabel="Size (contracts)")
    ax1.barh(
        bid_prices,
        bid_sizes,
        color=C_CHART_UP,
        alpha=0.7,
        height=bid_prices[1] - bid_prices[0] if len(bid_prices) > 1 else 1,
        label="Bids",
    )
    ax1.barh(
        ask_prices,
        ask_sizes,
        color=C_CHART_DOWN,
        alpha=0.7,
        height=ask_prices[1] - ask_prices[0] if len(ask_prices) > 1 else 1,
        label="Asks",
    )
    ax1.axhline(
        mid_price,
        color="#F0B90B",
        linewidth=1,
        linestyle="--",
        label=f"Mid {mid_price:,.2f}",
    )
    ax1.legend(frameon=False, labelcolor="white", fontsize=8)
    ax1.set_xlabel("Size", color="#888888", fontsize=8)

    _style_ax(ax2, "Cumulative Depth", ylabel="")
    ax2.fill_betweenx(bid_prices, cum_bids, color=C_CHART_UP, alpha=0.3)
    ax2.fill_betweenx(ask_prices, cum_asks, color=C_CHART_DOWN, alpha=0.3)
    ax2.plot(cum_bids, bid_prices, color=C_CHART_UP, linewidth=1.5)
    ax2.plot(cum_asks, ask_prices, color=C_CHART_DOWN, linewidth=1.5)
    ax2.axhline(mid_price, color="#F0B90B", linewidth=1, linestyle="--")
    ax2.set_xlabel("Cumulative Size", color="#888888", fontsize=8)
    ax2.tick_params(axis="y", labelleft=False)

    return _save_fig(fig, filename)


# ── CHART 7 — Volume Profile (15m) ──────────────────────────────────────────
def plot_volume_profile(klines_by_tf: dict, symbol: str, filename: str) -> str | None:
    """
    Horizontal volume profile for 15m candles.
    Shows which price levels attracted the most volume — these are real S/R.
    """
    logger.info("Chart 7: Volume profile for %s", symbol)
    data = klines_by_tf.get("15m", [])
    if not data:
        return None
    sd = sorted(data, key=lambda x: x["open_time"])

    all_prices = [c["close"] for c in sd]
    min_p, max_p = min(all_prices), max(all_prices)
    n_buckets = 50
    bucket_size = (max_p - min_p) / n_buckets
    if bucket_size == 0:
        return None

    buckets: dict[int, float] = {}
    for c in sd:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        bucket = int((typical - min_p) / bucket_size)
        bucket = min(bucket, n_buckets - 1)
        buckets[bucket] = buckets.get(bucket, 0) + c["volume"]

    bx = sorted(buckets.keys())
    bp = [min_p + (b + 0.5) * bucket_size for b in bx]
    bv = [buckets[b] for b in bx]

    max_vol = max(bv) if bv else 1
    poc_idx = bv.index(max_vol)
    poc_price = bp[poc_idx]
    bar_colors = [
        C_EMA1 if i == poc_idx else (C_CHART_UP if bv[i] > max_vol * 0.6 else "#3A5A6A")
        for i in range(len(bv))
    ]

    current_price = sd[-1]["close"]

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(16, 8), gridspec_kw={"width_ratios": [3, 1], "wspace": 0.04}
    )
    fig.patch.set_facecolor(C_CHART_BG)

    closes = [c["close"] for c in sd]
    _style_ax(ax1, f"{symbol}  |  15m  +  Volume Profile")
    for i in range(len(sd)):
        c = C_CHART_UP if closes[i] >= sd[i]["open"] else C_CHART_DOWN
        ax1.plot([i, i], [sd[i]["low"], sd[i]["high"]], linewidth=0.5, color=c)
        ax1.plot([i, i], [sd[i]["open"], closes[i]], linewidth=1.8, color=c)
    ax1.axhline(
        poc_price,
        color=C_EMA1,
        linewidth=1.0,
        linestyle="--",
        label=f"POC {poc_price:,.2f}",
    )
    ax1.axhline(current_price, color="white", linewidth=0.6, linestyle=":", alpha=0.5)
    tpos, tlabels = _time_labels(sd)
    ax1.set_xticks(tpos)
    ax1.set_xticklabels(tlabels, color="#888888", fontsize=6, rotation=15)
    ax1.legend(frameon=False, labelcolor="white", fontsize=8)
    ax1.set_ylim(min_p * 0.9995, max_p * 1.0005)

    _style_ax(ax2, "", ylabel="")
    ax2.barh(bp, bv, height=bucket_size * 0.85, color=bar_colors, alpha=0.85)
    ax2.axhline(poc_price, color=C_EMA1, linewidth=1.0, linestyle="--")
    ax2.axhline(current_price, color="white", linewidth=0.6, linestyle=":", alpha=0.5)
    ax2.set_xlabel("Volume", color="#888888", fontsize=7)
    ax2.tick_params(axis="y", labelleft=False)
    ax2.set_ylim(min_p * 0.9995, max_p * 1.0005)
    ax2.text(max_vol * 0.05, poc_price, "POC", color=C_EMA1, fontsize=7, va="bottom")

    return _save_fig(fig, filename)


def generate_all_charts(
    klines_by_tf: dict, ctx: dict, symbol: str, run_date: str
) -> dict[str, str]:
    """
    Generate all 7 chart images. Returns dict of {name: filepath}.
    Any chart that fails is skipped with a warning.
    """
    charts = {}
    jobs = [
        (
            "candles",
            plot_klines_image,
            (klines_by_tf, symbol, f"{symbol}_{run_date}_1_candles.png"),
        ),
        (
            "rsi",
            plot_rsi_chart,
            (klines_by_tf, symbol, f"{symbol}_{run_date}_2_rsi.png"),
        ),
        (
            "oi_price",
            plot_oi_price_chart,
            (klines_by_tf, ctx, symbol, f"{symbol}_{run_date}_3_oi_price.png"),
        ),
        (
            "funding",
            plot_funding_chart,
            (ctx, symbol, f"{symbol}_{run_date}_4_funding.png"),
        ),
        (
            "ls_ratio",
            plot_ls_ratio_chart,
            (ctx, symbol, f"{symbol}_{run_date}_5_ls_ratio.png"),
        ),
        (
            "orderbook",
            plot_orderbook_chart,
            (ctx, symbol, f"{symbol}_{run_date}_6_orderbook.png"),
        ),
        (
            "vol_profile",
            plot_volume_profile,
            (klines_by_tf, symbol, f"{symbol}_{run_date}_7_volprofile.png"),
        ),
    ]
    for name, fn, args in jobs:
        try:
            path = fn(*args)
            if path:
                charts[name] = path
                logger.info("Chart generated: %s → %s", name, path)
        except Exception as e:
            logger.warning("Chart '%s' failed: %s", name, e)
    return charts


# ──────────────────────────────────────────────
# RAW DATA PDF  (text/tables only — no charts)
# Charts are uploaded to Grok directly as images.
# ──────────────────────────────────────────────
def make_raw_pdf(
    klines_by_tf: dict,
    context_summary: str,
    filename: str,
    indicators_by_tf: dict = None,
):
    logger.info("Generating raw data PDF: %s", filename)

    columns = [
        "tf",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "num_trades",
    ]

    all_candles = []
    for tf in TIMEFRAMES:
        all_candles.extend(klines_by_tf.get(tf, []))
    all_candles.sort(key=lambda x: (x["tf"], x["open_time"]))

    table_data = [columns] + [
        [
            row["tf"],
            row["open_time"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["volume"],
            row.get("num_trades", ""),
        ]
        for row in all_candles
    ]

    mono = ParagraphStyle(
        "mono", fontName="Courier", fontSize=7, leading=10, spaceAfter=0
    )

    pdf = SimpleDocTemplate(
        filename,
        pagesize=A4,
        rightMargin=15,
        leftMargin=15,
        topMargin=15,
        bottomMargin=15,
    )

    elements = []

    # ── Context summary block ─────────────────────────
    elements.append(
        Paragraph(
            "MARKET CONTEXT DASHBOARD",
            ParagraphStyle(
                "DashTitle",
                fontName="Helvetica-Bold",
                fontSize=10,
                textColor=colors.HexColor("#0B1426"),
                spaceAfter=4,
                spaceBefore=4,
            ),
        )
    )
    for line in context_summary.splitlines():
        elements.append(Paragraph(line.replace(" ", "&nbsp;"), mono))
    elements.append(Spacer(1, 8))

    # ── Indicators summary table ──────────────────────
    if indicators_by_tf:
        elements.append(
            Paragraph(
                "CALCULATED INDICATORS SUMMARY",
                ParagraphStyle(
                    "IndTitle",
                    fontName="Helvetica-Bold",
                    fontSize=10,
                    textColor=colors.HexColor("#0B1426"),
                    spaceAfter=4,
                    spaceBefore=4,
                ),
            )
        )
        ind_headers = [
            "TF",
            "Price",
            "EMA50",
            "EMA200",
            "EMA Cross",
            "RSI(14)",
            "VWAP",
            "ATR(14)",
            "Vol Ratio",
        ]
        ind_rows = [ind_headers]
        for tf in TIMEFRAMES:
            ind = indicators_by_tf.get(tf, {})
            if ind:
                ind_rows.append([
                    tf,
                    str(ind["price"]),
                    f"{ind['ema_50']} ({ind['ema50_dist']:+.1f}%)",
                    f"{ind['ema_200']} ({ind['ema200_dist']:+.1f}%)",
                    ind["ema_cross"],
                    str(ind["rsi_14"]),
                    str(ind["vwap"]),
                    str(ind["atr_14"]),
                    f"{ind['vol_ratio']}x",
                ])
        ind_table = Table(ind_rows, repeatRows=1)
        ind_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1426")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F0F4FA")],
                ),
            ])
        )
        elements.append(ind_table)
        elements.append(Spacer(1, 8))

    # ── Raw kline data table ──────────────────────────
    elements.append(
        Paragraph(
            "RAW KLINE DATA",
            ParagraphStyle(
                "KlineTitle",
                fontName="Helvetica-Bold",
                fontSize=10,
                textColor=colors.HexColor("#0B1426"),
                spaceAfter=4,
                spaceBefore=4,
            ),
        )
    )
    kline_table = Table(table_data, repeatRows=1)
    kline_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
            (
                "ROWBACKGROUNDS",
                (0, 1),
                (-1, -1),
                [colors.white, colors.HexColor("#F7F7F7")],
            ),
        ])
    )
    elements.append(kline_table)

    pdf.build(elements)
    logger.info("Raw PDF created: %s", filename)
    return filename


# ──────────────────────────────────────────────
# ANALYSIS REPORT PDF  (the clean branded one)
# ──────────────────────────────────────────────
def make_analysis_report_pdf(
    symbol: str,
    analysis_text: str,
    chart_image_path: str,
    run_date: str,
    filename: str,
    indicators_by_tf: dict = None,
    charts: dict = None,
):
    import re
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader

    logger.info("Building analysis report PDF: %s", filename)

    PAGE_W, PAGE_H = A4[1], A4[0]  # landscape
    MARGIN = 20 * mm
    CONTENT_W = PAGE_W - 2 * MARGIN

    # ── Palette ──────────────────────────────────────
    C_NAVY = colors.HexColor("#0B1426")
    C_NAVY2 = colors.HexColor("#162040")
    C_GOLD = colors.HexColor("#C9A84C")
    C_GOLD_LIGHT = colors.HexColor("#EDD98A")
    C_GREEN = colors.HexColor("#1A6B3C")
    C_RED = colors.HexColor("#8B1A1A")
    C_AMBER = colors.HexColor("#7A5C10")
    C_BORDER = colors.HexColor("#D4C5A0")
    C_CARD_BG = colors.HexColor("#F7F5F0")
    C_TEXT = colors.HexColor("#1C1C2E")
    C_TEXT_MID = colors.HexColor("#3A3A5C")
    C_TEXT_LIGHT = colors.HexColor("#7A7A9A")
    C_WHITE = colors.white

    def md(t):
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)
        t = re.sub(r"`(.+?)`", r'<font name="Courier">\1</font>', t)
        t = re.sub(r"&(?!amp;|lt;|gt;|#)", "&amp;", t)
        return t

    def ps(
        name,
        size,
        color,
        bold=False,
        center=False,
        leading=None,
        indent=0,
        space_before=0,
        space_after=4,
    ):
        return ParagraphStyle(
            name,
            fontSize=size,
            textColor=color,
            fontName="Helvetica-Bold" if bold else "Helvetica",
            alignment=TA_CENTER if center else TA_LEFT,
            leading=leading or (size * 1.4),
            leftIndent=indent,
            spaceBefore=space_before,
            spaceAfter=space_after,
        )

    s_sec_label = ParagraphStyle(
        "SecLabel",
        fontSize=7,
        textColor=C_GOLD,
        fontName="Helvetica-Bold",
        spaceAfter=1,
    )
    s_sec_heading = ParagraphStyle(
        "SecHeading",
        fontSize=13,
        textColor=colors.HexColor("#4F81BD"),
        fontName="Helvetica-Bold",
        leading=16,
        spaceAfter=4,
    )
    s_body = ps("Body", 9.5, C_TEXT_MID, space_after=4)
    s_bullet = ps("Bul", 9.5, C_TEXT_MID, indent=10, space_after=3)
    s_subbullet = ps("SubBul", 9, C_TEXT_LIGHT, indent=22, space_after=2)
    s_disclaimer = ps("Disc", 7.5, C_TEXT_LIGHT, space_after=0, leading=11)

    def on_page(canv, doc):
        canv.saveState()
        w, h = PAGE_W, PAGE_H
        canv.setFillColor(colors.HexColor("#F0F4FA"))
        canv.rect(0, h - 32, w, 32, fill=1, stroke=0)
        canv.setFillColor(C_GOLD)
        canv.rect(0, h - 32, 4, 32, fill=1, stroke=0)
        try:
            canv.drawImage(
                ImageReader(logo_path),
                10,
                h - 28,
                width=70,
                height=22,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            canv.setFont("Helvetica-Bold", 10)
            canv.setFillColor(colors.HexColor("#4F81BD"))
            canv.drawString(14, h - 21, "Cryptic Technology")
        canv.setFont("Helvetica", 8)
        canv.setFillColor(colors.HexColor("#44546A"))
        canv.drawRightString(
            w - 14, h - 21, f"{symbol}   |   15m/1h/4h   |   {run_date}"
        )
        canv.setStrokeColor(C_GOLD)
        canv.setLineWidth(0.8)
        canv.line(0, h - 33, w, h - 33)
        canv.setStrokeColor(colors.HexColor("#C8D8E8"))
        canv.setLineWidth(0.4)
        canv.line(MARGIN, 26, w - MARGIN, 26)
        canv.setFont("Helvetica", 7)
        canv.setFillColor(colors.HexColor("#7A9AB8"))
        canv.drawString(
            MARGIN,
            14,
            "FOR INFORMATIONAL PURPOSES ONLY. NOT INVESTMENT ADVICE. "
            "PAST PERFORMANCE DOES NOT GUARANTEE FUTURE RESULTS.",
        )
        canv.drawRightString(w - MARGIN, 14, f"Page {doc.page}")
        canv.restoreState()

    # ════════════════════════════════════════════════
    #  PAGE 1 — Cover (low-level canvas)
    # ════════════════════════════════════════════════
    c = rl_canvas.Canvas(filename, pagesize=(PAGE_W, PAGE_H))

    def draw_header_bar(canv):
        w, h = PAGE_W, PAGE_H
        canv.setFillColor(C_NAVY)
        canv.rect(0, h - 32, w, 32, fill=1, stroke=0)
        canv.setFillColor(C_GOLD)
        canv.rect(0, h - 32, 4, 32, fill=1, stroke=0)
        canv.setFont("Helvetica-Bold", 10)
        canv.setFillColor(C_WHITE)
        canv.drawString(14, h - 21, "CryptoLens")
        canv.setFont("Helvetica", 10)
        canv.setFillColor(C_GOLD_LIGHT)
        canv.drawString(82, h - 21, "AI Research")
        canv.setFont("Helvetica", 8)
        canv.setFillColor(C_GOLD_LIGHT)
        canv.drawRightString(
            w - 14, h - 21, f"{symbol}   |   15m/1h/4h   |   {run_date}"
        )
        canv.setStrokeColor(C_GOLD)
        canv.setLineWidth(0.8)
        canv.line(0, h - 33, w, h - 33)
        canv.setStrokeColor(C_BORDER)
        canv.setLineWidth(0.4)
        canv.line(MARGIN, 26, w - MARGIN, 26)
        canv.setFont("Helvetica", 7)
        canv.setFillColor(C_TEXT_LIGHT)
        canv.drawString(
            MARGIN,
            14,
            "FOR INFORMATIONAL PURPOSES ONLY. NOT INVESTMENT ADVICE. "
            "PAST PERFORMANCE DOES NOT GUARANTEE FUTURE RESULTS.",
        )
        canv.drawRightString(w - MARGIN, 14, "Page 1")

    draw_header_bar(c)

    bg_path = "img/Doc_head_page.JPG"
    logo_path = "img/logo_2.png"

    try:
        c.drawImage(
            ImageReader(bg_path),
            0,
            0,
            width=PAGE_W,
            height=PAGE_H,
            preserveAspectRatio=False,
            mask="auto",
        )
    except Exception:
        c.setFillColor(colors.white)
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    draw_header_bar(c)

    LOGO_W, LOGO_H = 300, 72
    try:
        c.drawImage(
            ImageReader(logo_path),
            MARGIN,
            PAGE_H - 50 - LOGO_H - 30,
            width=LOGO_W,
            height=LOGO_H,
            preserveAspectRatio=True,
            mask="auto",
        )
    except Exception as e:
        logger.warning("Logo embed failed: %s", e)

    RULE_Y = PAGE_H - 33 - LOGO_H - 38
    TITLE_Y = RULE_Y - 95

    c.setFont("Helvetica-Bold", 48)
    c.setFillColor(colors.HexColor("#4F81BD"))
    c.drawString(MARGIN, TITLE_Y, symbol)

    c.setFont("Helvetica-Bold", 24)
    c.setFillColor(colors.HexColor("#44546A"))
    c.drawString(MARGIN, TITLE_Y - 36, "AI Market Analysis Report")

    c.setFont("Helvetica", 14)
    c.setFillColor(colors.HexColor("#44546A"))
    c.drawString(
        MARGIN, TITLE_Y - 60, f"Multi-Timeframe (15m / 1h / 4h)   |   {run_date}"
    )

    CONTACT_Y = 175
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(colors.HexColor("#4F81BD"))
    c.drawString(MARGIN, CONTACT_Y, "Cryptic Technology")
    c.setFont("Helvetica", 11)
    c.setFillColor(colors.HexColor("#44546A"))
    for i, line in enumerate([
        "Cape Town",
        "Protea Heights",
        "Brackenfell, 7560",
        "",
        "hrothmann704@gmail.com",
    ]):
        c.drawString(MARGIN, CONTACT_Y - 18 - (i * 16), line)

    META_HEIGHT = 46
    META_BOTTOM = 38
    META_TOP = META_BOTTOM + META_HEIGHT

    c.setFillColor(colors.HexColor("#F0F4FA"))
    c.rect(MARGIN, META_BOTTOM, CONTENT_W, META_HEIGHT, fill=1, stroke=0)
    c.setStrokeColor(C_GOLD)
    c.setLineWidth(1.5)
    c.line(MARGIN, META_TOP, MARGIN + CONTENT_W, META_TOP)
    c.setStrokeColor(colors.HexColor("#C8D8E8"))
    c.setLineWidth(0.4)
    c.line(MARGIN, META_BOTTOM, MARGIN + CONTENT_W, META_BOTTOM)

    generated_time = datetime.utcnow().strftime("%H:%M UTC")
    meta_items = [
        ("INSTRUMENT", symbol),
        ("TIMEFRAMES", "15m / 1h / 4h"),
        ("REPORT DATE", run_date),
        ("GENERATED", generated_time),
        ("SOURCE", "Binance Futures"),
        ("ANALYSIS BY", "Grok-4 AI"),
    ]
    col_w = CONTENT_W / len(meta_items)
    for i, (label, value) in enumerate(meta_items):
        x = MARGIN + i * col_w + 8
        if i > 0:
            c.setStrokeColor(colors.HexColor("#C8D8E8"))
            c.setLineWidth(0.3)
            c.line(
                MARGIN + i * col_w,
                META_BOTTOM + 6,
                MARGIN + i * col_w,
                META_TOP - 6,
            )
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(colors.HexColor("#7A9AB8"))
        c.drawString(x, META_BOTTOM + META_HEIGHT - 16, label)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(colors.HexColor("#44546A"))
        c.drawString(x, META_BOTTOM + META_HEIGHT - 30, value)

    c.showPage()

    # ════════════════════════════════════════════════
    #  PAGE 2+ — Platypus body
    # ════════════════════════════════════════════════

    def section_card(num, title, flowables):
        out = [Spacer(1, 6 * mm)]
        out.append(Paragraph(f"{num}  ——", s_sec_label))
        out.append(Paragraph(title, s_sec_heading))
        out.append(
            HRFlowable(
                width="100%", thickness=1.2, color=C_GOLD, spaceAfter=4, spaceBefore=0
            )
        )
        for fl in flowables:
            out.append(fl)
        out.append(Spacer(1, 2 * mm))
        return out

    def signal_badge(verdict, reason):
        upper = verdict.upper()
        if "—" in verdict:
            parts = verdict.split("—", 1)
            verdict_word = parts[0].strip()
            inline_reason = parts[1].strip()
        elif "-" in verdict and len(verdict) > 6:
            parts = verdict.split("-", 1)
            verdict_word = parts[0].strip()
            inline_reason = parts[1].strip()
        else:
            verdict_word = verdict.strip()
            inline_reason = ""
        full_reason = inline_reason or reason

        if "BUY" in upper:
            bg, label = C_GREEN, "BUY / LONG"
        elif "SELL" in upper:
            bg, label = C_RED, "SELL / SHORT"
        else:
            bg, label = C_AMBER, "WAIT / HOLD"

        s_lbl = ParagraphStyle(
            "SL",
            fontSize=22,
            textColor=C_WHITE,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
            leading=26,
            spaceAfter=4,
        )
        s_rsn = ParagraphStyle(
            "SR",
            fontSize=9.5,
            textColor=C_WHITE,
            fontName="Helvetica",
            alignment=TA_CENTER,
            leading=14,
            spaceAfter=0,
        )
        badge = Table(
            [
                [Paragraph(label, s_lbl)],
                [Paragraph(md(full_reason) if full_reason else "", s_rsn)],
            ],
            colWidths=[CONTENT_W],
        )
        badge.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 16),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
                ("LEFTPADDING", (0, 0), (-1, -1), 20),
                ("RIGHTPADDING", (0, 0), (-1, -1), 20),
                ("BOX", (0, 0), (-1, -1), 2, bg),
                ("LINEABOVE", (0, 0), (-1, 0), 4, C_GOLD),
                ("LINEBELOW", (0, -1), (-1, -1), 4, C_GOLD),
            ])
        )
        return [Spacer(1, 4 * mm), badge]

    SECTION_MAP = {
        "📊": ("01", "Market Overview"),
        "🕯": ("02", "Candle Patterns"),
        "📐": ("03", "Key Price Levels"),
        "📈": ("04", "EMA Momentum"),
        "⚡": ("05", "Trade Setup"),
        "🚦": ("06", "Signal"),
        "⚠": ("07", "Risk Factors"),
    }

    def parse_sections(text):
        sections, cur_key, cur_lines = [], None, []
        for raw in text.splitlines():
            hit = next((e for e in SECTION_MAP if raw.strip().startswith(e)), None)
            if hit:
                if cur_key:
                    sections.append((cur_key, cur_lines))
                cur_key, cur_lines = hit, []
            elif cur_key:
                cur_lines.append(raw)
        if cur_key:
            sections.append((cur_key, cur_lines))
        return sections

    def lines_to_flowables(lines):
        out = []
        for raw in lines:
            line = raw.strip()
            if not line:
                out.append(Spacer(1, 2))
                continue
            if re.match(r"^\s{2,}-\s", raw):
                content = re.sub(r"^\s+-\s*", "", raw).strip()
                out.append(Paragraph(f"  -  {md(content)}", s_subbullet))
            elif re.match(r"^-\s", line):
                content = line[2:].strip()
                lm = re.match(r"^\*\*(.+?)\*\*[:\s]+(.*)", content)
                if lm:
                    out.append(
                        Paragraph(f"<b>{lm.group(1)}:</b>  {md(lm.group(2))}", s_bullet)
                    )
                else:
                    out.append(Paragraph(f"•  {md(content)}", s_bullet))
            else:
                out.append(Paragraph(md(line), s_body))
        return out

    elements = []
    elements.append(Spacer(1, 8 * mm))

    # ── Charts for report (RSI + Volume Profile) ──────
    chart_meta_report = [
        (
            "rsi",
            "Chart 2 — Price + RSI(14)",
            "15m candles with RSI sub-panel — divergence signals",
        ),
        (
            "vol_profile",
            "Chart 7 — Volume Profile",
            "15m volume by price level — POC = highest-volume S/R",
        ),
    ]

    chart_heading_style = ParagraphStyle(
        "ChartH",
        fontName="Helvetica-Bold",
        fontSize=10,
        textColor=colors.HexColor("#0B1426"),
        spaceBefore=6,
        spaceAfter=1,
    )
    chart_sub_style = ParagraphStyle(
        "ChartSub",
        fontName="Helvetica",
        fontSize=8,
        textColor=colors.HexColor("#7A9AB8"),
        spaceAfter=4,
    )

    all_charts = charts or {}
    if "candles" not in all_charts and chart_image_path:
        all_charts = {"candles": chart_image_path, **all_charts}

    for key, heading, subtext in chart_meta_report:
        path = all_charts.get(key)
        if not path:
            continue
        try:
            elements.append(Paragraph(heading, chart_heading_style))
            elements.append(Paragraph(subtext, chart_sub_style))
            elements.append(RLImage(path, width=CONTENT_W, height=240))
            elements.append(Spacer(1, 5 * mm))
        except Exception as e:
            logger.warning("Report chart embed failed (%s): %s", key, e)

    elements.append(PageBreak())

    # ── Indicator snapshot table ──────────────────────
    if indicators_by_tf:
        ind_title_style = ParagraphStyle(
            "IndTitle2",
            fontSize=9,
            textColor=colors.HexColor("#7A9AB8"),
            fontName="Helvetica-Bold",
            spaceAfter=3,
        )
        elements.append(Paragraph("INDICATOR SNAPSHOT", ind_title_style))
        ind_headers = [
            "TF",
            "Price",
            "EMA50 dist",
            "EMA200 dist",
            "Cross",
            "RSI(14)",
            "ATR(14)",
            "Vol Ratio",
        ]
        ind_rows = [ind_headers]
        for tf in TIMEFRAMES:
            ind = indicators_by_tf.get(tf, {})
            if ind:
                ind_rows.append([
                    tf,
                    str(ind["price"]),
                    f"{ind['ema50_dist']:+.2f}%",
                    f"{ind['ema200_dist']:+.2f}%",
                    ind["ema_cross"],
                    str(ind["rsi_14"]),
                    str(ind["atr_14"]),
                    f"{ind['vol_ratio']}x",
                ])
        ind_tbl = Table(
            ind_rows, repeatRows=1, colWidths=[30, 70, 65, 65, 65, 50, 55, 55]
        )
        ind_tbl.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1426")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C8D8E8")),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F0F4FA")],
                ),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ])
        )
        elements.append(ind_tbl)
        elements.append(Spacer(1, 4 * mm))

    elements.append(
        Paragraph(
            f"Technical Analysis   |   {symbol}   |   {run_date}",
            ParagraphStyle(
                "Intro",
                fontSize=9,
                textColor=colors.HexColor("#7A9AB8"),
                fontName="Helvetica",
                spaceAfter=2,
            ),
        )
    )
    elements.append(
        HRFlowable(
            width="100%",
            thickness=1.5,
            color=colors.HexColor("#4F81BD"),
            spaceAfter=2,
            spaceBefore=0,
        )
    )

    for emoji_key, lines in parse_sections(analysis_text):
        if emoji_key not in SECTION_MAP:
            continue
        num, title = SECTION_MAP[emoji_key]

        if emoji_key == "🚦":
            all_lines = [l.strip() for l in lines if l.strip()]
            verdict, reason = "WAIT", ""
            for ln in all_lines:
                if "BUY" in ln.upper() or "SELL" in ln.upper() or "WAIT" in ln.upper():
                    verdict = ln
                    idx = all_lines.index(ln)
                    reason = " ".join(all_lines[idx + 1 :])
                    break
            elements += signal_badge(verdict, reason)
        else:
            body = lines_to_flowables(lines)
            if body:
                elements += section_card(num, title, body)

    # Disclaimer
    elements.append(Spacer(1, 8 * mm))
    disc_t = Table(
        [
            [
                Paragraph(
                    "<b>IMPORTANT DISCLAIMER</b>  —  This report is generated by CryptoLens AI "
                    "using automated technical analysis. It is provided for informational purposes "
                    "only and does not constitute investment advice, a solicitation, or a "
                    "recommendation to buy or sell any financial instrument. Cryptocurrency markets "
                    "are highly volatile. Past performance is not indicative of future results. "
                    "Always conduct your own research and consult a qualified financial adviser "
                    "before making investment decisions. Never invest more than you can afford to lose.",
                    s_disclaimer,
                )
            ]
        ],
        colWidths=[CONTENT_W],
    )
    disc_t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_CARD_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("LINEABOVE", (0, 0), (-1, 0), 2, C_NAVY),
        ])
    )
    elements.append(disc_t)

    # ── Merge cover page (canvas) + body (Platypus) ──
    import tempfile, shutil
    from pypdf import PdfWriter, PdfReader

    tmp_body = tempfile.mktemp(suffix=".pdf")
    tmp_cover = tempfile.mktemp(suffix=".pdf")

    try:
        doc_body = SimpleDocTemplate(
            tmp_body,
            pagesize=(PAGE_W, PAGE_H),
            rightMargin=MARGIN,
            leftMargin=MARGIN,
            topMargin=48,
            bottomMargin=50,
        )
        doc_body.build(elements, onFirstPage=on_page, onLaterPages=on_page)

        c.save()
        shutil.move(filename, tmp_cover)

        writer = PdfWriter()
        for path in [tmp_cover, tmp_body]:
            reader = PdfReader(path)
            for page in reader.pages:
                writer.add_page(page)
        with open(filename, "wb") as fout:
            writer.write(fout)
    finally:
        for p in [tmp_cover, tmp_body]:
            try:
                os.remove(p)
            except Exception:
                pass

    logger.info("Analysis report PDF created: %s", filename)
    return filename


# ──────────────────────────────────────────────
# xAI GROK ANALYSIS
# ──────────────────────────────────────────────
def analyze_with_grok(pdf_path: str, symbol: str, charts: dict = None) -> str:
    logger.info("Uploading raw PDF to xAI (symbol=%s)", symbol)
    uploaded_pdf = client_xai.files.upload(pdf_path)
    logger.info("PDF uploaded. File ID: %s", uploaded_pdf.id)

    # Upload each chart image separately so Grok can visually inspect each one
    uploaded_charts: list[tuple[str, object]] = []
    chart_labels = {
        "candles": "Chart 1 — Multi-TF Candlestick",
        "rsi": "Chart 2 — 15m + RSI Panel",
        "oi_price": "Chart 3 — OI vs Price (1h)",
        "funding": "Chart 4 — Funding Rate History",
        "ls_ratio": "Chart 5 — L/S Ratio: Smart Money vs Retail",
        "orderbook": "Chart 6 — Order Book Depth",
        "vol_profile": "Chart 7 — Volume Profile (15m)",
    }
    if charts:
        for key, path in charts.items():
            try:
                uf = client_xai.files.upload(path)
                uploaded_charts.append((chart_labels.get(key, key), uf))
                logger.info("Chart uploaded: %s → %s", key, uf.id)
            except Exception as e:
                logger.warning("Could not upload chart '%s': %s", key, e)

    chat = client_xai.chat.create(model="grok-4")
    chat.append(
        system(
            "You are a battle-tested crypto perps trader. Be brutally honest. No filler.\n\n"
            "You will receive:\n"
            "  1. A PDF with a full MARKET CONTEXT DASHBOARD (9 sections of derivatives data) "
            "followed by raw candle tables.\n"
            "  2. Up to 7 chart images — read each one carefully:\n"
            "     • Chart 1 — Multi-TF candlestick (4h/1h/15m) with EMA 50/200\n"
            "     • Chart 2 — 15m price + RSI(14) sub-panel (look for divergence)\n"
            "     • Chart 3 — 1h Open Interest vs Price overlay (divergence = key signal)\n"
            "     • Chart 4 — Funding rate bar chart (green=longs paying, red=shorts paying)\n"
            "     • Chart 5 — Long/Short ratio: top traders vs retail (shaded divergence zones)\n"
            "     • Chart 6 — Order book depth (left=raw walls, right=cumulative curve)\n"
            "     • Chart 7 — Volume profile (horizontal bars show real S/R by volume)\n\n"
            "Read the dashboard FIRST. Then study each chart visually. "
            "Synthesise everything — if dashboard and charts conflict, say so and explain which you trust more. "
            "The volume profile POC (point of control) is the most important S/R level — always reference it. "
            "RSI divergence on Chart 2 overrides a simple candle signal. "
            "OI/price divergence on Chart 3 is a leading indicator — weight it heavily. "
            "If it is a coin toss → say WAIT."
        )
    )

    # Build user message content — PDF first, then all chart images
    content_parts = [
        f"Analyse {symbol}. Use EXACTLY these headers, nothing more:\n\n"
        "📊 MARKET MOOD\n"
        "1–3 sentences. Bullish / bearish / choppy? Who's getting trapped right now?\n\n"
        "🕯️ CANDLE PATTERNS\n"
        "Only clear setups. State TF + pattern + what it means. "
        "If nothing clean → say 'nothing particularly clean'.\n\n"
        "📐 KEY LEVELS\n"
        "3–5 most important S/R. Lead with the Volume Profile POC. "
        "Use VWAP + recent swings + ATR multiples. One line per level: price — why it matters.\n\n"
        "📈 EMA PICTURE\n"
        "Aligned or conflicting across 4h/1h/15m? Stretched or mean-reverting? Two sentences max.\n\n"
        "⚡ TRADE IDEA  (or write 'NO CLEAR SETUP' if genuinely unclear)\n"
        "Direction | Entry trigger | Stop (1.5× ATR) | Target | R:R | Conviction: HIGH / MEDIUM / LOW\n\n"
        "🚦 SIGNAL\n"
        "BUY / SELL / WAIT — one line, one reason, strongest evidence only.\n\n"
        "⚠️ BIGGEST RISK\n"
        "1–2 sentences. What kills this trade fastest?\n\n"
        "Hard limit: keep total response under 500 words."
    ]

    chat.append(
        user(
            *content_parts,
            xai_file(uploaded_pdf.id),
            *[xai_file(uf.id) for _, uf in uploaded_charts],
        )
    )

    logger.info(
        "Requesting Grok analysis for %s (%s charts attached)",
        symbol,
        len(uploaded_charts),
    )
    response = chat.sample()
    logger.info("Grok analysis received for %s", symbol)
    return response.content


# ──────────────────────────────────────────────
# MAIN EXECUTION
# ──────────────────────────────────────────────
logger.info("Script execution started")
run_date = datetime.utcnow().strftime("%Y-%m-%d")

# Fear & Greed is market-wide (not per symbol) — fetch once before the loop
logger.info("Fetching Fear & Greed Index")
fear_greed_data = fetch_fear_greed(limit=10)

for symbol in trading_pairs:
    logger.info("Processing: %s", symbol)

    # 1. Fetch multi-timeframe klines
    klines_by_tf = get_all_klines(symbol)

    # 2. Fetch all futures context (OI, funding, L/S ratios, order book, etc.)
    futures_ctx = fetch_futures_context(symbol)

    # 3. Calculate indicators for each timeframe
    indicators_by_tf = {}
    for tf in TIMEFRAMES:
        candles = klines_by_tf.get(tf, [])
        if candles:
            indicators_by_tf[tf] = get_indicators(candles)
            logger.info(
                "Indicators calculated for %s %s: %s", symbol, tf, indicators_by_tf[tf]
            )

    # 4. Build context summary text block (includes Fear & Greed)
    context_summary = build_context_summary(
        symbol, futures_ctx, indicators_by_tf, fear_greed=fear_greed_data
    )

    # 5. Filenames
    raw_pdf_name = f"{symbol}_{run_date}_raw_data.pdf"
    report_pdf_name = f"{symbol}_{run_date}_analysis_report.pdf"

    # 6. Generate all 7 chart images
    charts = generate_all_charts(klines_by_tf, futures_ctx, symbol, run_date)
    logger.info("Generated %s charts for %s", len(charts), symbol)

    # 7. Raw data PDF — context dashboard + indicator table + kline data (no charts)
    make_raw_pdf(
        klines_by_tf=klines_by_tf,
        context_summary=context_summary,
        filename=raw_pdf_name,
        indicators_by_tf=indicators_by_tf,
    )

    # 8. Grok analysis — lean PDF + all 7 chart images uploaded as separate files
    analysis = analyze_with_grok(raw_pdf_name, symbol, charts=charts)
    # analysis = ""

    # 9. Branded analysis report PDF
    make_analysis_report_pdf(
        symbol=symbol,
        analysis_text=analysis,
        chart_image_path=charts.get("candles"),
        run_date=run_date,
        filename=report_pdf_name,
        indicators_by_tf=indicators_by_tf,
        charts=charts,
    )

    # 10. Cleanup — delete raw PDF and all chart PNGs
    for temp_file in [raw_pdf_name, *charts.values()]:
        try:
            #os.remove(temp_file)
            logger.info("Temp file deleted: %s", temp_file)
        except OSError as e:
            logger.warning("Could not delete temp file %s: %s", temp_file, e)

    logger.info("Report saved: %s", report_pdf_name)

    # Save API cache to disk for debugging / replay
    save_cache_to_disk(symbol, run_date)

logger.info("All done.")
