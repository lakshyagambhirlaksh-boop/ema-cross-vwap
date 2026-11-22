"""Serve the local Delta India dashboard."""

import ipaddress
import json
import os
import runpy
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError
from uuid import uuid4

from delta_strategy import RESOLUTIONS, candle_history_start, ema, entry_setup, fetch_candles, session_vwap
from delta_client import (
    CONTRACTS, DELTA_OPENER, dated_futures, delta_request,
    public_delta, strategy_activity, tradeable_product, valid_contract_symbol,
)
from live_strategy import LiveStrategy


DASHBOARD = Path(__file__).with_suffix(".html")
POSITIONS_PATH = "/api/positions"
ACTIVITY_PATH = "/api/activity"
POSITIONS_PORT = int(os.environ.get("POSITIONS_PORT", "8770"))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8768"))


STATE_DIR = Path(os.environ.get("DELTA_STATE_DIR", str(DASHBOARD.parent)))
STRATEGY = LiveStrategy(delta_request, public_delta, STATE_DIR / ".delta-positions.json")
INDICATOR_CACHE = {}
INDICATOR_HISTORY = {}
INDICATOR_LOCK = Lock()
INDICATOR_PENDING = set()


def indicator_snapshot(timeframe):
    with INDICATOR_LOCK:
        cached = INDICATOR_CACHE.get(timeframe)
        if timeframe not in INDICATOR_PENDING and (
                not cached or time.monotonic() - cached[0] >= 1):
            INDICATOR_PENDING.add(timeframe)
            Thread(target=sample_indicators, args=(timeframe,), daemon=True).start()
        return cached[1] if cached else {"timeframe": timeframe, "rows": [], "loading": True}


def sample_indicators(timeframe):
    seconds, _ = RESOLUTIONS[timeframe]
    try:
        now = int(time.time())

        def read_contract(symbol):
            try:
                history_key = (timeframe, symbol)
                history = INDICATOR_HISTORY.get(history_key, [])
                start = history[-1].time - seconds if history else None
                cutoff = candle_history_start(timeframe, now)
                if start is not None and start < cutoff:
                    history, start = [], None
                updates = fetch_candles(public_delta, symbol, timeframe, now, start=start)
                if updates:
                    merged = {candle.time: candle for candle in history if candle.time >= cutoff}
                    merged.update((candle.time, candle) for candle in updates if candle.time <= now)
                    candles = [merged[key] for key in sorted(merged)]
                    if candles:
                        INDICATOR_HISTORY[history_key] = candles
                        return {"contract": symbol, "candle_time": candles[-1].time,
                                "ema8": ema(candles, 8)[-1], "vwap": session_vwap(candles)[-1]}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return {"contract": symbol, "candle_time": None, "ema8": None, "vwap": None}

        sampled_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(CONTRACTS)) as pool:
            rows = list(pool.map(read_contract, sorted(CONTRACTS)))
        snapshot = {"timeframe": timeframe, "rows": rows}
        with INDICATOR_LOCK:
            INDICATOR_CACHE[timeframe] = (sampled_at, snapshot)
    finally:
        with INDICATOR_LOCK:
            INDICATOR_PENDING.discard(timeframe)


def order_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid order.")
    contract, side, size = (data.get(field) for field in ("contract", "side", "size"))
    if not valid_contract_symbol(contract) or side not in ("buy", "sell"):
        raise ValueError("Select a supported contract and side.")
    if type(size) is not int or not 1 <= size <= 2_147_483_647:
        raise ValueError("Enter a positive whole number of lots.")
    return {"product_symbol": contract, "side": side, "size": size,
            "order_type": "market_order", "client_order_id": uuid4().hex}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/api/orders", "/api/strategy/start", "/api/strategy/stop",
                             "/api/strategy/lots"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != origin or self.headers.get("Host") != urlsplit(origin).netloc:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if self.path == "/api/strategy/stop":
            try:
                STRATEGY.stop()
            except OSError:
                self.send_json(503, {"message": "New entries stopped, but Stop could not be saved for a restart."})
                return
            self.send_json(200, STRATEGY.status())
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self.send_json(400, {"message": "Invalid order. Check the contract, side, and lots."})
            return
        if self.path == "/api/strategy/start":
            self.start_strategy(data)
            return
        if self.path == "/api/strategy/lots":
            lots = data.get("lots") if isinstance(data, dict) else None
            if type(lots) is not int or not 1 <= lots <= 2_147_483_647:
                self.send_json(400, {"message": "Enter a valid whole number of lots."})
                return
            try:
                applied = STRATEGY.update_lots(lots)
            except OSError:
                self.send_json(503, {"message": "Could not save the live lot size."})
            else:
                self.send_json(200, {"applied": applied})
            return
        try:
            order = order_payload(data)
            tradeable_product(public_delta, order["product_symbol"])
        except ValueError as error:
            self.send_json(400, {"message": str(error)})
            return
        except OSError:
            self.send_json(502, {"message": "Could not verify the Delta contract."})
            return
        try:
            result = delta_request("POST", "/v2/orders", order)
        except (URLError, OSError, ValueError):
            self.send_json(502, {"message": "No usable response from Delta. Check your Delta order history before retrying.",
                                 "reference": order["client_order_id"]})
            return
        if result.get("success") is not True:
            code = result.get("error", {}).get("code", "unknown_error")
            self.send_json(502, {"message": f"Delta reported an order error ({code}). Check Delta before retrying.",
                                 "reference": order["client_order_id"]})
            return
        accepted = result.get("result") or {}
        self.send_json(200, {"id": accepted.get("id"), "state": accepted.get("state")})

    def start_strategy(self, data):
        if not isinstance(data, dict):
            self.send_json(400, {"message": "Invalid strategy settings."})
            return
        contracts, timeframe, lots = (data.get(field) for field in ("contracts", "timeframe", "lots"))
        if (not isinstance(contracts, list) or not contracts
                or not all(valid_contract_symbol(symbol) for symbol in contracts)
                or not isinstance(timeframe, str) or timeframe not in RESOLUTIONS
                or type(lots) is not int or not 1 <= lots <= 2_147_483_647):
            self.send_json(400, {"message": "Check the selected contracts, timeframe, and lots."})
            return
        try:
            STRATEGY.call("GET", "/v2/positions/margined")
            STRATEGY.start(dict.fromkeys(contracts), timeframe, lots)
        except (OSError, ValueError, KeyError, ArithmeticError) as error:
            self.send_json(409, {"message": str(error)})
            return
        self.send_json(200, STRATEGY.status())

    def send_json(self, status, data, origin=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/account-balance":
            try:
                wallet = delta_request("GET", "/v2/wallet/balances", timeout=5)
                if wallet.get("success") is not True:
                    raise ValueError("Delta wallet request failed")
                amount = next(row["balance_inr"] for row in wallet["result"]
                              if row.get("asset_symbol") == "USD")
                balance = Decimal(str(amount))
                if not balance.is_finite():
                    raise ValueError("Invalid INR balance")
            except (OSError, ValueError, InvalidOperation, KeyError, StopIteration, TypeError):
                self.send_json(502, {"message": "Account balance unavailable."})
            else:
                self.send_json(200, {"balance_inr": str(balance)})
            return
        if path == "/api/indicators":
            requested = parse_qs(parsed.query).get("timeframe", ["3 min"])[0]
            timeframe = STRATEGY.status()["timeframe"] or requested
            if timeframe not in RESOLUTIONS:
                self.send_json(400, {"message": "Invalid timeframe."})
                return
            self.send_json(200, indicator_snapshot(timeframe))
            return
        if path == "/api/contracts":
            try:
                futures = dated_futures()
            except (OSError, ValueError, TypeError):
                self.send_json(502, {"message": "Delta futures list unavailable."})
            else:
                self.send_json(200, {"dated_futures": futures})
            return
        if path == POSITIONS_PATH:
            self.send_positions()
            return
        if path == ACTIVITY_PATH:
            self.send_activity()
            return
        if path == "/api/strategy":
            self.send_json(200, STRATEGY.status())
            return
        if path == "/api/strategy-check":
            query = parse_qs(parsed.query)
            symbol = query.get("contract", ["XAUTUSD"])[0]
            timeframe = query.get("timeframe", ["3 min"])[0]
            if not valid_contract_symbol(symbol) or timeframe not in RESOLUTIONS:
                self.send_json(400, {"message": "Invalid contract or timeframe."})
                return
            now = int(time.time())
            seconds, _ = RESOLUTIONS[timeframe]
            try:
                tradeable_product(public_delta, symbol)
                traded = fetch_candles(public_delta, symbol, timeframe, now)
                mark = fetch_candles(public_delta, "MARK:" + symbol, timeframe, now)
                closed = [candle for candle in traded if candle.time + seconds <= now]
                if not closed:
                    raise ValueError("No closed candles.")
                candle = closed[-1]
                positions = delta_request("GET", "/v2/positions/margined")
            except (OSError, ValueError, KeyError):
                self.send_json(502, {"message": "Unable to read Delta strategy data."})
                return
            self.send_json(200, {
                "contract": symbol, "timeframe": timeframe, "candle_time": candle.time,
                "ema8": ema(closed, 8)[-1], "vwap": session_vwap(closed)[-1],
                "mark_candle_available": any(item.time == candle.time for item in mark),
                "entry_setup": entry_setup(closed, seconds),
                "positions_readable": positions.get("success") is True,
            })
            return
        if path == "/api/market-data":
            try:
                data = public_delta("/v2/tickers?contract_types=perpetual_futures,futures")
                if not isinstance(data, list):
                    raise ValueError("Invalid ticker response")
                tickers = {
                    ticker["symbol"]: {"last": ticker.get("close"), "mark": ticker.get("mark_price")}
                    for ticker in data if ticker.get("symbol") in CONTRACTS or ticker.get("contract_type") == "futures"
                }
                if not tickers:
                    raise ValueError("No supported tickers")
            except (OSError, ValueError):
                self.send_json(502, {"message": "Delta market data unavailable."})
            else:
                self.send_json(200, {"tickers": tickers})
            return
        if path == "/api/delta-status":
            try:
                result = delta_request("GET", "/v2/orders")
                connected = result.get("success") is True
                reason = None if connected else (result.get("error") or {}).get("code", "API check failed")
            except (OSError, ValueError):
                connected, reason = False, "Could not complete the API check"
            self.send_json(200, {"connected": connected, "reason": reason})
            return
        if path == "/api/public-ip":
            try:
                with DELTA_OPENER.open("https://api.ipify.org", timeout=5) as response:
                    address = str(ipaddress.IPv4Address(response.read(128).decode().strip()))
            except (OSError, ValueError):
                self.send_json(502, {"message": "Public IP unavailable."})
            else:
                self.send_json(200, {"ip": address})
            return
        if path not in ("/", "/dashboard.html"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        page = DASHBOARD.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def send_positions(self, origin=None):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            positions = STRATEGY.call("GET", "/v2/positions/margined")
            open_positions = [position for position in positions if int(position["size"])]
            protective_prices = None
            if open_positions:
                try:
                    orders = STRATEGY.call("GET", "/v2/orders?states=open,pending")
                    protective_prices = {}
                    for order in orders:
                        order_type = order.get("stop_order_type")
                        if (order_type in ("stop_loss_order", "take_profit_order") and order.get("reduce_only")
                                and order.get("state") in ("open", "pending") and order.get("stop_price")):
                            key = (order["product_id"], order["side"], order_type)
                            protective_prices.setdefault(key, []).append(str(order["stop_price"]))
                except (OSError, ValueError, KeyError, TypeError):
                    protective_prices = None
            rows = []
            with STRATEGY.lock:
                tracked = dict(STRATEGY.trades)
            for position in open_positions:
                size = int(position["size"])
                symbol = position.get("product_symbol") or str(position["product_id"])
                record = tracked.get(symbol, {})
                bot_target = record.get("software_target")
                key = (position["product_id"], "sell" if size > 0 else "buy")
                stops = protective_prices.get((*key, "stop_loss_order"), []) if protective_prices is not None else []
                targets = protective_prices.get((*key, "take_profit_order"), []) if protective_prices is not None else []
                rows.append({
                    "contract": symbol,
                    "side": "Long" if size > 0 else "Short",
                    "lots": abs(size),
                    "entry": position.get("entry_price"),
                    "mark": position.get("mark_price"),
                    "pnl": position.get("unrealized_pnl"),
                    "stop_loss": (record["stop"] + " (bot)" if bot_target else None) or ", ".join(dict.fromkeys(stops)) or
                    ("No active stop" if protective_prices is not None else "Unavailable"),
                    "target": (bot_target + " (bot)" if bot_target else None) or ", ".join(dict.fromkeys(targets)) or
                    ("No active target" if protective_prices is not None else "Unavailable"),
                })
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(502, {"message": "Delta positions unavailable."}, origin)
            return
        self.send_json(200, {"positions": rows, "includes_targets": True, "includes_stop_loss": True}, origin)

    def send_activity(self, origin=None):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            orders = STRATEGY.call("GET", "/v2/orders/history?page_size=50")
            rows = strategy_activity(orders)
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(502, {"message": "Delta order history unavailable."}, origin)
            return
        self.send_json(200, {"activities": rows, "includes_bracket_fills": True}, origin)


class PositionsHandler(DashboardHandler):
    """Serve the new read-only view while the existing engine stays active."""

    def do_GET(self):
        if self.path not in (POSITIONS_PATH, ACTIVITY_PATH):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        origin = f"http://127.0.0.1:{DASHBOARD_PORT}"
        if self.headers.get("Origin") != origin:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.path == POSITIONS_PATH:
            self.send_positions(origin)
        else:
            self.send_activity(origin)

    def do_POST(self):
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)


class DashboardServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.allow_reuse_port = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    positions_only = sys.argv[1:] == ["--positions-only"]
    port, handler = (POSITIONS_PORT, PositionsHandler) if positions_only else (DASHBOARD_PORT, DashboardHandler)
    with DashboardServer(("127.0.0.1", port), handler) as server:
        if not positions_only:
            STRATEGY.launch()
        address = f"http://127.0.0.1:{server.server_port}/"
        print(f"{'Read-only positions feed' if positions_only else 'Dashboard'}: {address}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    # Existing Cloud deployments may still use this file as their entry point.
    streamlit_runner = sys.modules.get("streamlit.runtime.scriptrunner")
    if streamlit_runner is not None and streamlit_runner.get_script_run_ctx(suppress_warning=True):
        runpy.run_path(str(Path(__file__).with_name("streamlit_app.py")), run_name="__main__")
    else:
        main()
