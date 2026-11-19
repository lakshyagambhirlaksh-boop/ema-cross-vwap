# Delta EMA / VWAP Strategy

A local dashboard and Python trading engine for Delta Exchange India futures. The dashboard lists the ten configured perpetuals and any active dated futures returned by Delta India. The engine validates each contract against Delta's product metadata. The dashboard also shows connection status, open positions, and recent order activity. The repository includes a separate TradingView Pine strategy.

## Run locally

Use Python 3.10 or later. The dashboard uses the Python standard library.

1. Copy `.env.example` to `.env`. In PowerShell, run `Copy-Item .env.example .env`.
2. Set `DELTA_API_KEY` and `DELTA_API_SECRET` in your local `.env`. Allowlist the server's public IPv4 address in Delta and enable the key's trading permission for live execution.
3. Run `python dashboard.py` from the project folder.
4. Open [the dashboard](http://127.0.0.1:8768/) in a browser or in the VS Code Integrated Browser.

The engine starts with new entries disabled. Select contracts, submit a timeframe, and set lots per contract before using **Start strategy**. Confirming Start enables real order submissions when a setup qualifies. **Stop strategy** pauses new entries while the running process continues managing its tracked positions. Keep the Python process running for software-managed exits. Position tracking is stored locally in `.delta-positions.json`.

## Trading rules

- An 8 EMA cross above or below session VWAP (OHLC4) creates a setup when the traded-price candle closes on the same side of VWAP. The next candle must close beyond the signal candle's high for a long or low for a short.
- If the first confirmation closes inside the signal candle's body, the setup gets one additional candle to confirm against the original high or low.
- The stop is the low of the candle before the signal for a long, or its high for a short. The target is four times the distance from the actual entry fill to that stop.
- Running-candle RSI exits at 74 or higher for longs and 20 or lower for shorts. The EMA 5 exit occurs at candle close when both the traded-price and mark-price candles miss their respective EMA 5.

The Python engine uses Delta candle data. It builds 10-minute candles from consecutive 5-minute candles and uses UTC day boundaries for session VWAP. Exact indicator parity with Delta's chart and the live order lifecycle have yet to be verified. Strategy entry and exit reason labels are available for orders sent by versions that include reason tags.

## Deploy the Streamlit dashboard

In Streamlit Community Cloud, select this GitHub repository, branch `main`, and main file path `streamlit_app.py`. Existing deployments using `dashboard.py` also open the Streamlit dashboard. For a local preview, run `python -m pip install -r requirements.txt` and `python -m streamlit run streamlit_app.py`.

The Streamlit app opens without credentials. You can connect with a read-only key to see positions and recent orders, or enable live controls by adding these values in Streamlit Cloud's app Secrets:

```toml
DELTA_API_KEY = "your_delta_india_trade_enabled_key"
DELTA_API_SECRET = "your_delta_api_secret"
DASHBOARD_ACCESS_CODE = "a_long_unique_random_access_code"
```

Delta must allowlist the cloud server's outbound IPv4 for the live key. Unlock the dashboard, select contracts and lots, submit your timeframe, and confirm **Start strategy** to enable real orders. **Stop strategy** pauses entries and continues managing positions opened by that engine. Run only one strategy engine for the same account and contracts. If you change the credentials for the same API key, reboot the Streamlit app to refresh the cached trading connection.

Streamlit Community Cloud can sleep or restart. While its process is down, the software RSI and EMA 5 exits cannot run. Delta's accepted stop-loss and target orders remain on the exchange. Cloud storage also does not guarantee that the position journal survives a redeploy. Use the local engine or an always-on host with persistent storage when continuous management is required.

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
