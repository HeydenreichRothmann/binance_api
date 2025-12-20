from __future__ import annotations

import os
import time
import sys
import json
import logging
from pathlib import Path
from typing import Any, Iterable
from datetime import datetime, timezone

import matplotlib.pyplot as plt
from binance.um_futures import UMFutures
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle


# =============================================================================
# LOGGING SETUP
# =============================================================================

LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger: logging.Logger = logging.getLogger(__name__)

# =============================================================================
# PATH SETUP
# =============================================================================


def get_runtime_dir() -> Path:
    """
    Directory where the executable lives.
    - Source run: folder of this file
    - PyInstaller onefile: folder of the .exe
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


RUNTIME_DIR = get_runtime_dir()
DATA_DIRECTORY = RUNTIME_DIR / "data"

DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)

logger.info("Data directory ready: %s", DATA_DIRECTORY)


# =============================================================================
# CONFIGURATION LOADING
# =============================================================================

def get_runtime_dir() -> Path:
    """
    Returns the directory the program is running from.
    - When frozen (PyInstaller): directory of the executable
    - When running from source: directory of this file
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


RUNTIME_DIR = get_runtime_dir()
CONFIG_PATH = RUNTIME_DIR / "config.json"

logger.info("Loading configuration from %s", CONFIG_PATH)


# ---- Load config file ----
if not CONFIG_PATH.exists():
    logger.critical("Config file not found: %s", CONFIG_PATH)
    logger.critical("Copy config_template.json and rename it to config.json")
    sys.exit(1)

try:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config: dict = json.load(f)
except json.JSONDecodeError as exc:
    logger.critical("Invalid JSON in config.json: %s", exc)
    sys.exit(1)


# ---- Validate required keys ----
REQUIRED_KEYS = ["api_key", "api_secret", "trading_pair"]

missing_keys = [k for k in REQUIRED_KEYS if k not in config]
if missing_keys:
    logger.critical("Missing required config keys: %s", ", ".join(missing_keys))
    sys.exit(1)


# ---- Validate trading pairs ----
trading_pairs = config.get("trading_pair", [])

if not isinstance(trading_pairs, list) or not trading_pairs:
    logger.critical("'trading_pair' must be a non-empty list in config.json")
    sys.exit(1)


# ---- Final confirmation ----
logger.info(
    "Configuration loaded (api_key_present=%s, api_secret_present=%s)",
    bool(config.get("api_key")),
    bool(config.get("api_secret")),
)
logger.info("Trading pairs configured: %s", ", ".join(trading_pairs))



# =============================================================================
# BINANCE CLIENT INITIALIZATION & VALIDATION
# =============================================================================

logger.info("Initializing Binance UM Futures client")

binance_client: UMFutures = UMFutures(
    key=config.get("api_key"),
    secret=config.get("api_secret"),
)

logger.info("Validating Binance API credentials")

try:
    account_information: dict[str, Any] = binance_client.account()
    logger.info(
        "Binance credentials validated (can_trade=%s, can_withdraw=%s)",
        account_information.get("canTrade"),
        account_information.get("canWithdraw"),
    )
except Exception as exc:
    logger.critical(
        "Binance API credentials validation failed: %s",
        exc,
        exc_info=True,
    )
    raise RuntimeError("Invalid Binance API credentials") from exc













# =============================================================================
# KLINE CONSTANTS
# =============================================================================

TIMEFRAMES: list[str] = ["15m"]

TIMEFRAME_TO_MINUTES: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}

KLINE_FIELD_NAMES: list[str] = [
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

NUMERIC_KLINE_FIELDS: set[str] = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_base_volume",
    "taker_quote_volume",
}


# =============================================================================
# DATA PROCESSING FUNCTIONS
# =============================================================================

def calculate_candle_limits(
    base_timeframe: str,
    base_candle_count: int,
    requested_timeframes: Iterable[str],
) -> dict[str, int]:
    """
    Calculate how many candles to request per timeframe so all
    cover the same historical window.
    """
    if base_timeframe not in TIMEFRAME_TO_MINUTES:
        raise ValueError(f"Unknown base timeframe: {base_timeframe}")

    total_window_minutes: int = (
        TIMEFRAME_TO_MINUTES[base_timeframe] * base_candle_count
    )

    candle_limits: dict[str, int] = {}

    for timeframe in requested_timeframes:
        if timeframe not in TIMEFRAME_TO_MINUTES:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        candle_limits[timeframe] = (
            total_window_minutes // TIMEFRAME_TO_MINUTES[timeframe]
        )

    return candle_limits


def normalize_klines(
    raw_klines: list[list[Any]],
) -> list[dict[str, Any]]:
    """
    Convert raw Binance kline arrays into dictionaries with
    normalized numeric types.
    """
    normalized_klines: list[dict[str, Any]] = []

    for raw_kline in raw_klines:
        kline_record: dict[str, Any] = dict(
            zip(KLINE_FIELD_NAMES, raw_kline)
        )

        for field_name in NUMERIC_KLINE_FIELDS:
            kline_record[field_name] = float(kline_record[field_name])

        normalized_klines.append(kline_record)

    return normalized_klines


def fetch_symbol_klines(symbol: str) -> list[dict[str, Any]]:
    """
    Fetch and normalize all required klines for a single trading pair.
    """
    logger.info("Collecting klines for symbol=%s", symbol)

    all_candles: list[dict[str, Any]] = []

    candle_limits: dict[str, int] = calculate_candle_limits(
        base_timeframe="2h",
        base_candle_count=100,
        requested_timeframes=TIMEFRAMES,
    )

    for timeframe, candle_limit in candle_limits.items():
        logger.info(
            "Fetching klines (symbol=%s, timeframe=%s, limit=%s)",
            symbol,
            timeframe,
            candle_limit,
        )

        raw_klines: list[list[Any]] = binance_client.klines(
            symbol=symbol,
            interval=timeframe,
            limit=candle_limit,
        )

        normalized_klines: list[dict[str, Any]] = normalize_klines(raw_klines)

        for candle in normalized_klines:
            candle["tf"] = timeframe
            all_candles.append(candle)

    logger.info(
        "Collected %s total candles for %s",
        len(all_candles),
        symbol,
    )

    return all_candles


# =============================================================================
# OUTPUT GENERATION
# =============================================================================

def generate_pdf_table(
    candle_data: list[dict[str, Any]],
    output_path: str,
) -> None:
    """
    Generate a PDF table containing raw candle data.
    """
    logger.info("Generating PDF: %s", output_path)

    column_headers: list[str] = [
        "tf",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    table_rows: list[list[Any]] = [column_headers]

    for candle in candle_data:
        table_rows.append([
            candle["tf"],
            candle["open_time"],
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle["volume"],
        ])

    document: SimpleDocTemplate = SimpleDocTemplate(
        output_path,
        pagesize=A4,
    )

    table: Table = Table(table_rows, repeatRows=1)

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
        ])
    )

    document.build([table])

    logger.info("PDF written successfully: %s", output_path)


def calculate_exponential_moving_average(
    values: list[float],
    period: int,
) -> list[float]:
    """
    Calculate EMA values without lookahead.
    """
    ema_values: list[float] = []
    smoothing_factor: float = 2 / (period + 1)

    for index, price in enumerate(values):
        if index == 0:
            ema_values.append(price)
        else:
            ema_values.append(
                price * smoothing_factor
                + ema_values[index - 1] * (1 - smoothing_factor)
            )

    return ema_values


def generate_candlestick_chart(
    candle_data: list[dict[str, Any]],
    symbol: str,
    output_path: str,
    timeframe: str = "15m",
) -> None:
    """
    Generate a candlestick chart with EMA overlays.
    """
    filtered_candles: list[dict[str, Any]] = [
        candle for candle in candle_data
        if candle.get("tf") == timeframe
    ]

    if not filtered_candles:
        logger.warning(
            "No candle data available for symbol=%s timeframe=%s",
            symbol,
            timeframe,
        )
        return

    filtered_candles.sort(key=lambda c: c["open_time"])

    opens: list[float] = [c["open"] for c in filtered_candles]
    highs: list[float] = [c["high"] for c in filtered_candles]
    lows: list[float] = [c["low"] for c in filtered_candles]
    closes: list[float] = [c["close"] for c in filtered_candles]

    ema_25: list[float] = calculate_exponential_moving_average(closes, 25)
    ema_50: list[float] = calculate_exponential_moving_average(closes, 50)

    figure, axis = plt.subplots(figsize=(14, 6))
    figure.patch.set_facecolor("white")
    axis.set_facecolor("white")

    for index in range(len(filtered_candles)):
        candle_color: str = "green" if closes[index] >= opens[index] else "red"

        axis.plot([index, index], [lows[index], highs[index]], linewidth=1, color=candle_color)
        axis.plot([index, index], [opens[index], closes[index]], linewidth=3, color=candle_color)

    axis.plot(ema_25, linewidth=1.5, label="EMA 25")
    axis.plot(ema_50, linewidth=1.5, linestyle="--", label="EMA 50")

    axis.set_title(f"{symbol} 15m Candles")
    axis.set_xlabel("Candle Index")
    axis.set_ylabel("Price")
    axis.legend(frameon=False)
    axis.grid(False)

    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()

    logger.info("Chart image written: %s", output_path)


# =============================================================================
# SCRIPT ENTRYPOINT
# =============================================================================

logger.info("Script execution started")

run_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

for trading_symbol in trading_pairs:
    logger.info("Processing trading pair: %s", trading_symbol)

    candle_dataset: list[dict[str, Any]] = fetch_symbol_klines(trading_symbol)

    pdf_output_path: str = os.path.join(
        DATA_DIRECTORY,
        f"{trading_symbol}_{run_date}_raw_candle_results.pdf",
    )

    image_output_path: str = os.path.join(
        DATA_DIRECTORY,
        f"{trading_symbol}_{run_date}_15m_candles.png",
    )

    generate_pdf_table(candle_dataset, pdf_output_path)
    generate_candlestick_chart(candle_dataset, trading_symbol, image_output_path)

logger.info("Script execution completed successfully")
