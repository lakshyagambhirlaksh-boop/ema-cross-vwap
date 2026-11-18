"""Serve the local Delta India dashboard."""

import hashlib
import hmac
import ipaddress
import json
import socket
import sys
import time
from http.client import HTTPSConnection
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from delta_strategy import RESOLUTIONS, ema, entry_setup, fetch_candles, session_vwap
from live_strategy import ORDER_REASONS, LiveStrategy


DASHBOARD = Path(__file__).with_suffix(".html")
CONTRACTS = frozenset("BTCUSD ETHUSD SOLUSD XRPUSD XAUTUSD DOGEUSD BNBUSD LTCUSD SLVONUSD OILUSD".split())
DELTA_URL = "https://api.india.delta.exchange"
POSITIONS_PATH = "/api/positions"
ACTIVITY_PATH = "/api/activity"
POSITIONS_PORT = 8770
DASHBOARD_PORT = 8768


class IPv4Connection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self.connect_ipv4

    @staticmethod
    def connect_ipv4(address, timeout, source_address):
        host, port = address
        failure = OSError("Delta has no reachable IPv4 address.")
        for _, _, _, _, endpoint in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            try:
                return socket.create_connection(endpoint, timeout, source_address)
            except OSError as error:
                failure = error
        raise failure


class IPv4HTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(IPv4Connection, request)


DELTA_OPENER = build_opener(ProxyHandler({}), IPv4HTTPSHandler())


def delta_request(method, path, order=None):
    values = {}
    for line in DASHBOARD.with_name(".env").read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.partition("=")
        if name.strip() in {"DELTA_API_KEY", "DELTA_API_SECRET"}:
            values[name.strip()] = value.strip().strip("'\"")
    key = values.get("DELTA_API_KEY")
    secret = values.get("DELTA_API_SECRET")
    if not key or not secret:
        raise ValueError("Delta API credentials are missing.")

    payload = json.dumps(order, separators=(",", ":")) if order is not None else ""
    timestamp = str(int(time.time()))
    message = (method + timestamp + path + payload).encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    request = Request(DELTA_URL + path, payload.encode() if order is not None else None,
                      method=method, headers={
                          "api-key": key, "timestamp": timestamp, "signature": signature,
                          "Content-Type": "application/json", "User-Agent": "DeltaDashboard/1.0",
                      })
    try:
        with DELTA_OPENER.open(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            return json.load(error)
        except ValueError:
            return {"success": False, "error": {"code": f"HTTP {error.code}"}}


def public_delta(path):
    with DELTA_OPENER.open(DELTA_URL + path, timeout=5) as response:
        data = json.load(response)
    if data.get("success") is not True:
        raise ValueError("Delta market data request failed.")
    return data["result"]


STRATEGY = LiveStrategy(delta_request, public_delta, DASHBOARD.with_name(".delta-positions.json"))


def order_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid order.")
    contract, side, size = (data.get(field) for field in ("contract", "side", "size"))
    if not isinstance(contract, str) or contract not in CONTRACTS or side not in ("buy", "sell"):
        raise ValueError("Select a supported contract and side.")
    if type(size) is not int or not 1 <= size <= 2_147_483_647:
        raise ValueError("Enter a positive whole number of lots.")
    return {"product_symbol": contract, "side": side, "size": size,
            "order_type": "market_order", "client_order_id": uuid4().hex}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/api/orders", "/api/strategy/start", "/api/strategy/stop"):
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
            STRATEGY.stop()
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
        try:
            order = order_payload(data)
        except ValueError as error:
            self.send_json(400, {"message": str(error)})
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
        if (not isinstance(contracts, list) or not 1 <= len(contracts) <= len(CONTRACTS)
                or not all(isinstance(symbol, str) and symbol in CONTRACTS for symbol in contracts)
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
            if symbol not in CONTRACTS or timeframe not in RESOLUTIONS:
                self.send_json(400, {"message": "Invalid contract or timeframe."})
                return
            now = int(time.time())
            seconds, _ = RESOLUTIONS[timeframe]
            try:
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
                data = public_delta("/v2/tickers?contract_types=perpetual_futures")
                if not isinstance(data, list):
                    raise ValueError("Invalid ticker response")
                tickers = {
                    ticker["symbol"]: {"last": ticker.get("close"), "mark": ticker.get("mark_price")}
                    for ticker in data if ticker.get("symbol") in CONTRACTS
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
        self.end_headers()
        self.wfile.write(page)

    def send_positions(self, origin=None):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            positions = STRATEGY.call("GET", "/v2/positions/margined")
            rows = []
            for position in positions:
                size = int(position["size"])
                if size:
                    rows.append({
                        "contract": position.get("product_symbol") or str(position["product_id"]),
                        "side": "Long" if size > 0 else "Short",
                        "lots": abs(size),
                        "entry": position.get("entry_price"),
                        "mark": position.get("mark_price"),
                        "pnl": position.get("unrealized_pnl"),
                    })
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(502, {"message": "Delta positions unavailable."}, origin)
            return
        self.send_json(200, {"positions": rows}, origin)

    def send_activity(self, origin=None):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            orders = STRATEGY.call("GET", "/v2/orders/history?page_size=50")
            rows = [activity_row(order) for order in orders]
        except (OSError, ValueError, KeyError, TypeError):
            self.send_json(502, {"message": "Delta order history unavailable."}, origin)
            return
        self.send_json(200, {"activities": rows}, origin)


def activity_row(order):
    client_id = order.get("client_order_id") or ""
    reason = next((label for prefix, label in ORDER_REASONS.items() if client_id.startswith(prefix)), None)
    stop_type = order.get("stop_order_type")
    if not reason:
        reason = {
            "stop_loss_order": "Stop-loss",
            "take_profit_order": "Take-profit",
            "liquidation_order": "Liquidation",
        }.get(stop_type, "Exit reason unavailable" if order.get("reduce_only") else "Order reason unavailable")
    cancellation = order.get("cancellation_reason")
    if order.get("state") == "cancelled" and cancellation:
        reason += " · Cancelled: " + cancellation.replace("_", " ")
    return {
        "time": order.get("updated_at") or order.get("created_at"),
        "contract": order.get("product_symbol") or order.get("product_id"),
        "side": order.get("side"), "state": order.get("state"),
        "filled_lots": int(order["size"]) - int(order["unfilled_size"]),
        "price": order.get("average_fill_price"), "reason": reason,
    }


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
    main()
