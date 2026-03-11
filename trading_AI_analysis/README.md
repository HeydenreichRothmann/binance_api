
# Cryptic Trading Journal

This project is a cryptocurrency trading journal built using Python. It connects to the Binance Futures API to track your trades, process data, and generate insightful reports that help you analyze your trading performance. The script automatically fetches your trading data, calculates key metrics like profit/loss (PNL) and return on investment (ROI), and exports these results as CSV and PDF reports.

## Features

- **Binance Futures API Integration**: Automatically pulls and logs trade data for specified cryptocurrency symbols (e.g., BTCUSDT).
- **Data Processing**: Calculates trade metrics such as entry price, PNL, ROI, trade direction, and more.
- **CSV Data Export**: Exports processed trade data to CSV for easy analysis.
- **Graphical Analysis**: Generates visual reports using `matplotlib` to help you better understand your trading performance.
- **Currency Conversion**: Uses `forex_python` to handle conversions between different currencies.
- **PDF Reporting**: Automatically creates and merges PDF reports summarizing your trading performance.
- **Image Handling**: Uses `PIL` to embed images or charts in the reports.

## Requirements

Before running the script, ensure you have the following dependencies installed:

```bash
pip install binance-connector-python forex-python fpdf pypdf matplotlib pillow pandas
```

## Getting Started

1. **Clone the Repository**: Download or clone this repository to your local machine.

```bash
git clone <repository-url>
```

2. **Set Up Your Binance API Keys**: Obtain your API key and secret from your Binance account. You can set these keys within the script:

```python
cm_futures_client = UMFutures(key='YOUR_API_KEY', secret='YOUR_API_SECRET')
```

3. **Run the Script**: Execute the script from your terminal or IDE:

```bash
python trading_journal_v2.py
```

## Configuration

- **Coin Symbol**: The script tracks trades for a specified cryptocurrency symbol. By default, it’s set to `BTCUSDT`. You can change this in the script:

```python
coin_symbol = 'BTCUSDT'
```

- **File Paths**: Ensure that the paths for storing images, CSV, and PDF files are correct. Update the `path` variable accordingly:

```python
path = "/your/directory/path/"
```

## How It Works

1. The script fetches trade data from Binance using the Futures API.
2. It processes each trade, extracting key details such as entry price, realized PNL, trade side, and investment amount.
3. The data is written to a CSV file for further analysis.
4. A graphical analysis of your trading performance is generated using `matplotlib`.
5. A PDF report is created summarizing the trading data and visual insights.

## Example Output

The script outputs two main files:
- **CSV Report**: Contains detailed trade data (date, entry, status, PNL, ROI, etc.).
- **PDF Report**: Includes a summary of your trades with visual charts.

## Customization

- You can customize the trade metrics that are calculated or change how the PDF report is structured.
- Modify the graphical analysis section to generate additional charts or insights that align with your trading goals.

## Troubleshooting

- Ensure that your API keys are correct and have sufficient permissions.
- If you encounter rate limits, consider adding delays between API calls.
- For issues with PDF generation, ensure all required libraries are installed and properly configured.

## Future Improvements

- Add support for multiple coin symbols.
- Implement real-time trade tracking.
- Improve the report generation by adding more detailed performance metrics.

## License

This project is licensed under the MIT License.















# Trading Checklist and Journal Logging Guide

This guide is designed to be followed exactly as outlined. Each step in the checklist must be completed to ensure disciplined, rule-based trading and thorough documentation in the trading journal.

---

## **Initiating Trade**

**Follow each step carefully and in order.** This checklist is essential for assessing market conditions, confirming trade signals, and managing risk effectively. Skipping steps may result in missed insights or mismanaged trades. Use the checkboxes to mark each step as completed.

| Tick | Task                                              | Comment                                             |
|------|---------------------------------------------------|-----------------------------------------------------|
| [ ]  | **Determine Market Trend and Direction**          | Confirm primary trend direction using higher timeframes (daily, weekly). |
| [ ]  | **Evaluate Potential for Buy or Sell Position**   | Decide if conditions favor a buy or sell. Avoid counter-trend trades unless a clear reversal signal is present. |
| [ ]  | **Use Fibonacci Retracement Tool**                | Place the Fibonacci tool from point A to B and mark the 38.2%, 50%, and 61.8% levels. |
| [ ]  | **Identify Support and Resistance Levels**        | Zoom out to locate previous support and resistance levels that may reinforce the trade setup. |
| [ ]  | **Consider Support and Resistance at 61.8%**      | Pay attention to any critical levels around the 61.8% retracement for added confirmation. |
| [ ]  | **Draw Key Trading Zones and Trendlines**         | Draw zones and trendlines around these levels to reinforce the trade direction and potential entry points. |
| [ ]  | **Wait for Market to Reach Your Zone**            | Allow the price to approach your identified zone. Confirm with stochastic or other momentum indicators if applicable. |
| [ ]  | **Seek Multiple Trade Signals**                   | Look for two or more confirming signals, such as candlestick patterns or volume confirmation, to increase trade validity. |
| [ ]  | **Define Risk Management Parameters**             | Set stop-loss based on recent highs/lows or a fixed risk percentage. Determine profit targets before entering the trade. |
| [ ]  | **Monitor News and High-Impact Events**           | Review the economic calendar to identify any events that may affect the trade’s outcome. |
| [ ]  | **Place the Trade and Observe**                   | Only place the trade once all criteria are met. Monitor behavior carefully once the trade is active. |
| [ ]  | **Draw in a Channel Line**                        | Use a channel line to track deviations from the intended trade direction over time. |

---

## **Trade Logging**

**Each step in this logging process is mandatory** to ensure that all trades are documented comprehensively in your automated journal. Accurate logging helps review and analyze past trades, which is crucial for improving future performance. No steps should be skipped.

| Step | Task                                                 | Description                                                              |
|------|------------------------------------------------------|--------------------------------------------------------------------------|
| [ ]  | **Open the Terminal**                                | Begin by opening the terminal to start the journal logging process.      |
| [ ]  | **Activate Virtual Environment**                     | Run `source Documents/Side_hussle/Journal/side_hussle/bin/activate` to activate the virtual environment. |
| [ ]  | **Navigate to Journal Directory**                    | Use `cd Documents/Side_hussle/Journal/mic/` to access the directory where the journal script is located. |
| [ ]  | **Run Journal Script**                               | Run `python Journal.py` to log trade details accurately in the automated journal. |
| [ ]  | **Check PDF Output**                                 | Verify that the PDF journal is saved at `Documents/Side_hussle/Journal/mic/Journal.py`. Ensure all details are captured correctly. |

---

This guide provides a structured approach to trading and logging. Following these rules strictly is crucial for consistency, disciplined trading, and maintaining a reliable record of your trades.