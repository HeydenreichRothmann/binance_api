# 📈 Market Data Collector – Binance Futures (15m)

👋  This repository exists for one simple reason:

**to collect clean, honest market data in a way that you can trust.**

If you’re here, you’re probably not looking for hype, signals, or “secret strategies.”
You’re looking for something much more valuable: **clarity** ✨

This script is designed to quietly and reliably pull **raw 15-minute candlestick data** from Binance USD-M Futures, organize it, and present it in a way that makes sense both to machines *and* humans.

No guessing.
No magic.
No hidden behavior.

Just data you can stand behind 🤍


## 🧭 The Big Picture

When you run this script, it gently walks through a sequence:

1. It checks that everything is configured correctly ✅
2. It verifies that your Binance credentials actually work 🔐
3. It pulls recent market data for the symbols you care about 📊
4. It formats that data so it’s consistent and usable 🧹
5. It generates clear outputs you can inspect and keep 🖨️
6. It stores everything neatly so nothing gets lost 📁

At every step, it tells you what it’s doing — and if something isn’t right, it stops and explains why.

The goal is that **nothing ever happens silently**.


## 🧠 The Philosophy Behind It

Before getting into how things work, it’s important to understand *why* they work this way.

This project follows a few guiding ideas:

* **15-minute candles are the foundation** 🧱
  Granular enough to capture structure, stable enough to avoid noise overload.

* **Raw data comes first** 🔍
  Indicators and strategies can be added later. Corrupted data cannot be fixed later.

* **Validation beats assumptions** 🛑
  API keys are tested. Inputs are checked. Failures are explicit.

* **Humans should be able to look at the data** 👀
  That’s why PDFs and charts are generated — not just machine-only outputs.

Everything in this script exists to support those ideas.


## ⚙️ Configuration: Where You Start

All configuration lives in a single file: `config.json`.

```json
{
  "api_key": "YOUR_BINANCE_API_KEY",
  "api_secret": "YOUR_BINANCE_API_SECRET",
  "trading_pair": ["BTCUSDT", "ETHUSDT"]
}
```

### What each part means

* **🔑 api_key / api_secret**
  Your Binance USD-M Futures API credentials.
  Read-only access is enough — no trading permissions required.

* **📌 trading_pair**
  A list of symbols you want data for.
  Each symbol is processed calmly, one at a time.

If this file is missing or malformed, the script will stop immediately and explain what’s wrong. It won’t guess.

## 🔐 Making Sure Binance Is Reachable

Before any data is collected, the script performs a small but important check:

It makes an authenticated request to Binance to confirm that:

* the API key exists
* the secret matches
* permissions are correct
* IP restrictions (if any) allow access

If this check fails, **nothing else runs** ❌

This protects you from confusing situations where:

* files are half-generated
* charts are empty
* errors appear much later

If something’s wrong, you’ll know right away.

---

## 📊 What Data Is Collected

The script collects **15-minute OHLCV candlestick data** from Binance USD-M Futures.

Each candle includes:

* open time
* open price
* high price
* low price
* close price
* volume
* exchange metadata

All numeric values are normalized into floats for consistency.

Nothing is resampled. Nothing is smoothed.
What Binance returns is what you get.

## 🧾 What Gets Generated

For each trading pair, the script creates **two outputs**, stored inside the `data/` folder.

### 📄 1. Raw Candle Data (PDF)

A clean, readable table containing:

* one row per candle
* exact values from the exchange
* no derived calculations

This is useful for:

* audits
* spot checks
* sharing raw data with humans

---

### 🖼️ 2. Candlestick Chart (PNG)

Each chart shows:

* true candlesticks (wicks + bodies)
* a clean white background
* visible axes for scale
* **EMA 25 and EMA 50 overlays**

These EMA lines exist **only for visual context** 🧭
They help your eye understand structure and momentum — nothing more.


## 📁 Where Everything Goes

When the script runs, it creates a folder called `data/` (if it doesn’t already exist).

Example:

```
data/
├── BTCUSDT_2025-01-01_raw_candle_results.pdf
├── BTCUSDT_2025-01-01_15m_candles.png
├── ETHUSDT_2025-01-01_raw_candle_results.pdf
└── ETHUSDT_2025-01-01_15m_candles.png
```

Everything is named clearly so you always know:

* what it is
* when it was generated
* which symbol it belongs to

## 📝 Logging & Transparency

As the script runs, it logs what it’s doing in plain language:

* configuration loading
* credential validation
* candle counts
* file creation

Nothing is hidden. Nothing is silent.

## 🚫 What This Script Is *Not*

This script is **not**:

* a trading bot
* a strategy
* a signal generator
* a profit promise

It’s a foundation — and foundations are meant to be boring, solid, and dependable 🧱


## 💬 Final Words

This project was written with care so that:

* users don’t feel lost
* mistakes are caught early
* data stays honest
* results are inspectable

If reading this made you feel calm instead of confused, then it’s doing exactly what it was meant to do 🤍

Build whatever you want on top of it —
just keep the clarity that makes it trustworthy.

