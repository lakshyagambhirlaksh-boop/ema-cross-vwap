"""Shared Delta India API access for the local server and Streamlit."""

import hashlib
import hmac
import json
import os
import re
import socket
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


DELTA_URL = "https://api.india.delta.exchange"


CONTRACTS = frozenset(
    "BTCUSD ETHUSD SOLUSD XRPUSD XAUTUSD DOGEUSD SNDKBUSD LTCUSD SLVONUSD OILUSD "
    "EVAAUSD PIEVERSEUSD BLESSUSD AKEUSD SKYAIUSD VVVUSD".split()
)


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
    "ema-sl-": "Stop-loss",
    "ema-tp-": "Take-profit",
    "ema-save-": "Stop and target could not be saved",
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


def delta_request(method, path, order=None, *, credentials=None, timeout=10):
    if credentials is None:
        names = ("DELTA_API_KEY", "DELTA_API_SECRET")
        credentials = tuple(os.environ.get(name) for name in names)
        if credentials == (None, None):
            values = {}
            for line in Path(__file__).with_name(".env").read_text(encoding="utf-8-sig").splitlines():
                name, _, value = line.partition("=")
                if name.strip() in names:
                    values[name.strip()] = value.strip().strip("'\"")
            credentials = tuple(values.get(name) for name in names)
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
        with DELTA_OPENER.open(request, timeout=timeout) as response:
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


def strategy_activity(orders):
    """Return fills attributable to tagged entries or their Delta brackets."""
    prefixes = tuple(ORDER_REASONS)
    entries = [
        order for order in orders
        if (order.get("client_order_id") or "").startswith("ema-entry-") and filled_lots(order)
    ]
    brackets = [
        order for order in orders
        if order.get("bracket_order") is True and order.get("reduce_only") is True
        and order.get("stop_order_type") in ("stop_loss_order", "take_profit_order")
    ]
    verified_brackets = set()
    for entry in entries:
        candidates = [order for order in brackets if bracket_matches_entry(order, entry)]
        stops = [order for order in candidates if order["stop_order_type"] == "stop_loss_order"]
        targets = [order for order in candidates if order["stop_order_type"] == "take_profit_order"]
        pairs = [(stop, target) for stop in stops for target in targets
                 if bracket_matches_target(stop, target, entry)]
        if len(pairs) == 1:
            verified_brackets.update(order["id"] for order in pairs[0])

    rows = []
    for order in orders:
        tagged = (order.get("client_order_id") or "").startswith(prefixes)
        if filled_lots(order) and (tagged or order.get("id") in verified_brackets):
            rows.append({**activity_row(order), "strategy_verified": True})
    return rows


def filled_lots(order):
    return int(order["size"]) - int(order["unfilled_size"]) > 0


def order_time(order, field):
    return datetime.fromisoformat(order[field].replace("Z", "+00:00"))


def api_origin(order):
    meta = order.get("meta_data") or {}
    return meta.get("ip") if meta.get("source") == "api" else None


def bracket_matches_entry(order, entry):
    origin = api_origin(entry)
    if (not origin or origin != api_origin(order) or order.get("product_id") != entry.get("product_id")
            or order.get("side") == entry.get("side")
            or int(order["size"]) != int(entry["size"]) - int(entry["unfilled_size"])):
        return False
    try:
        delay = (order_time(order, "created_at") - order_time(entry, "updated_at")).total_seconds()
        match_price = (order.get("meta_data") or {}).get("entry_price")
        return 0 <= delay <= 30 and (
            match_price is None or Decimal(match_price) == Decimal(entry["average_fill_price"])
        )
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False


def bracket_matches_target(stop, target, entry):
    try:
        creation_gap = abs((order_time(stop, "created_at") - order_time(target, "created_at")).total_seconds())
        price = Decimal(entry["average_fill_price"])
        stop_price = Decimal(stop["stop_price"])
        target_price = Decimal(target["stop_price"])
        risk = price - stop_price
        tolerance = Decimal(1).scaleb(target_price.as_tuple().exponent) / 2
        return (creation_gap <= 1 and (risk > 0 if entry["side"] == "buy" else risk < 0)
                and any(abs(target_price - (price + multiple * risk)) <= tolerance
                        for multiple in (Decimal("1.5"), 3, 4)))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False
