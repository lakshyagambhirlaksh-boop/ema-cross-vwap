"""Shared Delta India API access for the local server and Streamlit."""

import hashlib
import hmac
import json
import re
import socket
import time
from http.client import HTTPSConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


DELTA_URL = "https://api.india.delta.exchange"


CONTRACTS = frozenset("BTCUSD ETHUSD SOLUSD XRPUSD XAUTUSD DOGEUSD BNBUSD LTCUSD SLVONUSD OILUSD".split())


def valid_contract_symbol(symbol):
    return isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,79}", symbol) is not None


def tradeable_product(public, symbol):
    if not valid_contract_symbol(symbol):
        raise ValueError("Invalid Delta contract symbol.")
    product = public("/v2/products/" + symbol)
    kind = product.get("contract_type")
    allowed = kind == "futures" or (kind == "perpetual_futures" and symbol in CONTRACTS)
    if not allowed or product.get("symbol") != symbol or product.get("state") != "live":
        raise ValueError(f"{symbol} is outside the supported live Delta futures markets.")
    return product


ORDER_REASONS = {
    "ema-entry-": "Confirmed 8 EMA / VWAP entry",
    "ema-rsi-": "RSI exit",
    "ema-five-": "EMA 5 missed on traded and mark candles",
    "ema-reverse-": "Confirmed opposite setup",
    "ema-protect-": "Protective bracket could not be confirmed",
}


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


def delta_request(method, path, order=None, *, credentials=None):
    if credentials is None:
        values = {}
        for line in Path(__file__).with_name(".env").read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() in {"DELTA_API_KEY", "DELTA_API_SECRET"}:
                values[name.strip()] = value.strip().strip("'\"")
        credentials = (values.get("DELTA_API_KEY"), values.get("DELTA_API_SECRET"))
    key, secret = credentials
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


def dated_futures():
    products = public_delta("/v2/products?states=live&contract_types=futures&page_size=100")
    return sorted({
        product["symbol"] for product in products
        if product.get("contract_type") == "futures" and product.get("state") == "live"
        and valid_contract_symbol(product.get("symbol"))
    })


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
