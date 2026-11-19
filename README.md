# Delta EMA / VWAP Strategy

A local dashboard and Python trading engine for Delta Exchange India futures. The dashboard lists the ten configured perpetuals and any active dated futures returned by Delta India. The engine validates each contract against Delta's product metadata. The dashboard also shows connection status, open positions, and recent order activity. The repository includes a separate TradingView Pine strategy.

## Run locally

Use Python 3.10 or later. The dashboard uses the Python standard library.

1. Copy `.env.example` to `.env`. In PowerShell, run `Copy-Item .env.example .env`.
2. Set `DELTA_API_KEY` and `DELTA_API_SECRET` in your local `.env`. Allowlist the server's public IPv4 address in Delta and enable the key's trading permission for live execution.
3. Run `python dashboard.py` from the project folder.
4. Open [the dashboard](http://127.0.0.1:8768/) in a browser or in the VS Code Integrated Browser.

The engine starts with new entries disabled on a fresh install. Select contracts, submit a timeframe, and set lots per contract before confirming **Start strategy**. The engine saves that choice and resumes scanning new candles after a service restart. **Stop strategy** saves the stopped state and pauses new entries while the engine continues managing tracked positions. Keep the Python process running for software-managed exits.

## Trading rules

- An 8 EMA cross above or below session VWAP (OHLC4) creates a setup when the traded-price candle closes on the same side of VWAP. The next candle must close beyond the signal candle's high for a long or low for a short.
- If the first confirmation closes inside the signal candle's body, the setup gets one additional candle to confirm against the original high or low.
- A long stop uses the low of the most recent candle before the signal that closed below its own VWAP. A short stop uses the high of the most recent candle before the signal that closed above its own VWAP. If no prior candle qualifies, the trade is skipped. The target is four times the distance from the actual entry fill to that stop.
- Running-candle RSI exits at 74 or higher for longs and 20 or lower for shorts.

The Python engine uses Delta candle data and builds 10-minute candles from consecutive 5-minute candles. It uses supplied Delta chart VWAP readings for entries and reversals. Strategy entry and exit reason labels are available for orders sent by versions that include reason tags.

## Supply Delta chart VWAP readings

Delta's public API supplies candles and volume. It has no documented feed for the plotted VWAP. A trusted chart sender must submit closed-candle readings to `POST /api/chart-vwap` through the local dashboard or VPS SSH tunnel. The request takes JSON with `contract`, `timeframe`, and `readings`, an array of objects with `candle_time` (the candle's open time in Unix seconds) and `vwap` (the chart value). The request must use the dashboard URL as its `Origin`. Supply the latest and preceding closed readings so the strategy can confirm the cross and find its stop candle.

No automatic chart-value sender is included. Without current readings, new entries and reversals wait. Exchange stop-loss and target orders remain in effect, and the bot continues checking the running-candle RSI exit.

For a manual import, open **Import Delta chart VWAP** under the indicator table, choose the contract, and paste CSV or tab-separated data with `Time` and `VWAP` headers. Times must identify closed candles by their open timestamp, in Unix seconds or ISO 8601 with an explicit timezone. The dashboard imports rows using the timeframe shown on its indicator table.

## Deploy the Streamlit dashboard

In Streamlit Community Cloud, select this GitHub repository, branch `main`, and main file path `streamlit_app.py`. Existing deployments using `dashboard.py` also open the Streamlit dashboard. For a local preview, run `python -m pip install -r requirements.txt` and `python -m streamlit run streamlit_app.py`.

The Streamlit app opens without credentials. You can connect with a read-only key to see positions and recent orders, or enable live controls by adding these values in Streamlit Cloud's app Secrets:

```toml
DELTA_API_KEY = "your_delta_india_trade_enabled_key"
DELTA_API_SECRET = "your_delta_api_secret"
DASHBOARD_ACCESS_CODE = "a_long_unique_random_access_code"
```

Delta must allowlist the cloud server's outbound IPv4 for the live key. Unlock the dashboard, select contracts and lots, submit your timeframe, and confirm **Start strategy** to enable real orders. **Stop strategy** pauses entries and continues managing positions opened by that engine. Run only one strategy engine for the same account and contracts. If you change the credentials for the same API key, reboot the Streamlit app to refresh the cached trading connection.

Streamlit Community Cloud can sleep or restart. While its process is down, software-managed exits cannot run. Delta's accepted stop-loss and target orders remain on the exchange. Cloud storage also does not guarantee that the position journal survives a redeploy. Use the local engine or an always-on host with persistent storage when continuous management is required.

## Deploy on a VPS

See [VPS deployment](VPS.md) for the Docker Compose setup, private dashboard access over SSH, and persistent position tracking.

## Source files

- `dashboard.py` serves the local dashboard.
- `delta_client.py` provides shared Delta API access and signs requests.
- `dashboard.html` contains the dashboard.
- `delta_strategy.py` evaluates candles and trade rules.
- `live_strategy.py` places orders and tracks strategy positions.
- `ema-cross-vwap.pine` is the TradingView strategy for chart testing.

Keep `.env`, API credentials, and the local position journal outside version control.
