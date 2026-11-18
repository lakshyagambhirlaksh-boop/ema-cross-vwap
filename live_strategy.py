"""Run the Delta strategy and reconcile its positions with the exchange."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from threading import Event, Lock, Thread
from urllib.parse import urlencode
from uuid import uuid4

from delta_strategy import RESOLUTIONS, entry_setup, exit_reason, fetch_candles


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


def strategy_order_id(prefix):
    return prefix + uuid4().hex[:32 - len(prefix)]


class LiveStrategy:
    def __init__(self, private, public, journal):
        self.private = private
        self.public = public
        self.journal = journal
        self.lock = Lock()
        self.wake = Event()
        self.enabled = False
        self.settings = {}
        self.last_candle = {}
        self.errors = {}
        self.trades = json.loads(journal.read_text()) if journal.exists() else {}
        self.started_at = 0

    def launch(self):
        Thread(target=self.run, daemon=True, name="delta-strategy").start()

    def start(self, contracts, timeframe, lots):
        products = {}
        for symbol in contracts:
            product = tradeable_product(self.public, symbol)
            tick = Decimal(str(product["tick_size"]))
            if not tick.is_finite() or tick <= 0:
                raise ValueError("Invalid Delta tick size for " + symbol)
            products[symbol] = {"id": int(product["id"]), "tick": str(tick),
                                "timeframe": timeframe, "lots": lots}
        with self.lock:
            if self.enabled:
                raise ValueError("Stop new entries before changing strategy settings.")
            self.settings = products
            self.started_at = time.time()
            self.enabled = True
        self.wake.set()

    def stop(self):
        with self.lock:
            self.enabled = False
        self.wake.set()

    def status(self):
        with self.lock:
            return {"running": self.enabled, "positions": len(self.trades),
                    "errors": dict(self.errors)}

    def save_trade(self, symbol, trade):
        # Called with the lock held. Persist before sending an entry.
        updated = dict(self.trades)
        if trade is None:
            updated.pop(symbol, None)
        else:
            updated[symbol] = trade
        temporary = self.journal.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated), encoding="utf-8")
        temporary.replace(self.journal)
        self.trades = updated

    def call(self, method, path, body=None):
        response = self.private(method, path, body)
        if response.get("success") is not True:
            code = (response.get("error") or {}).get("code", "unknown_error")
            raise ValueError(f"Delta rejected {method} {path.split('?')[0]} ({code}).")
        return response.get("result")

    def position(self, product_id):
        return self.call("GET", "/v2/positions?" + urlencode({"product_id": product_id}))

    def order(self, symbol, side, lots, *, client_id=None, reduce_only=False):
        return self.call("POST", "/v2/orders", {
            "product_symbol": symbol, "side": side, "size": lots, "order_type": "market_order",
            "time_in_force": "ioc", "reduce_only": reduce_only,
            "client_order_id": client_id or uuid4().hex,
        })

    def exit(self, symbol, size, reason):
        self.order(symbol, "sell" if size > 0 else "buy", abs(size),
                   client_id=strategy_order_id(reason), reduce_only=True)

    def protect(self, symbol, trade, size, position):
        entry = Decimal(str(position["entry_price"]))
        stop = Decimal(trade["stop"])
        tick = Decimal(trade["tick"])
        direction = 1 if size > 0 else -1
        risk = direction * (entry - stop)
        target = ((entry + direction * 4 * risk) / tick).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP) * tick
        if risk <= 0 or target <= 0:
            raise ValueError("Fill was outside the valid stop range.")
        self.call("POST", "/v2/orders/bracket", {
            "product_id": trade["id"], "bracket_stop_trigger_method": "last_traded_price",
            "stop_loss_order": {"order_type": "market_order", "stop_price": format(stop, "f")},
            "take_profit_order": {"order_type": "limit_order", "stop_price": format(target, "f"),
                                  "limit_price": format(target, "f")},
        })
        with self.lock:
            self.save_trade(symbol, {**trade, "protected": True})

    def manage(self, symbol, trade, now):
        position = self.position(trade["id"])
        size = int(position["size"])
        if not size:
            if not trade.get("filled"):
                # An ambiguous entry is resolved by its exchange client ID before clearing it.
                order = self.call("GET", "/v2/orders/client_order_id/" + trade["client_id"])
                if order["state"] not in ("closed", "cancelled"):
                    return
                filled = int(order["size"]) - int(order["unfilled_size"])
                if filled:
                    raise ValueError("Entry filled but position has not been confirmed. Check Delta.")
            with self.lock:
                self.save_trade(symbol, None)
            return
        if (size > 0) != (trade["side"] == "buy") or abs(size) > trade["lots"]:
            raise ValueError("Position differs from the strategy entry. Check Delta.")
        if trade.get("closing"):
            self.exit(symbol, size, trade.get("close_reason", "ema-exit-"))
            return
        if not trade.get("protected"):
            trade = {**trade, "filled": True}
            with self.lock:
                self.save_trade(symbol, trade)
            try:
                self.protect(symbol, trade, size, position)
            except (OSError, ValueError, KeyError, ArithmeticError):
                # A failed or uncertain bracket must not leave an unprotected position open.
                # The close is reduce-only so an existing bracket fill cannot reverse the trade.
                with self.lock:
                    self.save_trade(symbol, {**trade, "closing": True, "close_reason": "ema-protect-"})
                self.exit(symbol, size, "ema-protect-")
                raise ValueError("Bracket could not be confirmed. A reduce-only close was requested.")
            return
        seconds, _ = RESOLUTIONS[trade["timeframe"]]
        traded = fetch_candles(self.public, symbol, trade["timeframe"], now)
        mark = fetch_candles(self.public, "MARK:" + symbol, trade["timeframe"], now)
        why = exit_reason(traded, mark, seconds, now, size, trade["opened_at"])
        if why:
            reason = "ema-rsi-" if why == "RSI" else "ema-five-"
            with self.lock:
                self.last_candle[symbol] = (now // seconds - 1) * seconds
                self.save_trade(symbol, {**trade, "closing": True, "close_reason": reason})
            self.exit(symbol, size, reason)
            return
        with self.lock:
            settings = self.settings.get(symbol) if self.enabled else None
            started = self.started_at
        if settings and settings["timeframe"] == trade["timeframe"]:
            closed = [candle for candle in traded if candle.time + seconds <= now]
            if not closed or closed[-1].time != (now // seconds - 1) * seconds:
                return
            if closed[-1].time + seconds <= max(started, trade["opened_at"]):
                return
            signal = entry_setup(closed, seconds)
            if signal and signal[0] != trade["side"]:
                with self.lock:
                    self.save_trade(symbol, {**trade, "closing": True, "close_reason": "ema-reverse-"})
                self.exit(symbol, size, "ema-reverse-")

    def scan(self, symbol, settings, now):
        seconds, _ = RESOLUTIONS[settings["timeframe"]]
        expected = (now // seconds - 1) * seconds
        with self.lock:
            started_at = self.started_at
            if self.last_candle.get(symbol) == expected or expected + seconds <= started_at:
                return
        traded = fetch_candles(self.public, symbol, settings["timeframe"], now)
        closed = [candle for candle in traded if candle.time + seconds <= now]
        if not closed or closed[-1].time != expected:
            return
        signal = entry_setup(closed, seconds)
        with self.lock:
            self.last_candle[symbol] = expected
        if not signal or int(self.position(settings["id"])["size"]):
            return
        side, stop = signal
        last = traded[-1].close
        if (last <= stop if side == "buy" else last >= stop):
            return
        trade = {**settings, "side": side, "stop": str(stop), "client_id": strategy_order_id("ema-entry-"),
                 "opened_at": now}
        with self.lock:
            if not self.enabled or self.started_at != started_at:
                return
            self.save_trade(symbol, trade)
        self.order(symbol, side, settings["lots"], client_id=trade["client_id"])
        self.manage(symbol, trade, int(time.time()))

    def process(self, symbol, settings, trade, now):
        try:
            if trade:
                self.manage(symbol, trade, now)
            elif settings:
                self.scan(symbol, settings, now)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            with self.lock:
                self.errors[symbol] = str(error)
                if symbol in self.trades:
                    self.enabled = False
        else:
            with self.lock:
                self.errors.pop(symbol, None)

    def run(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            while True:
                started = time.monotonic()
                now = int(time.time())
                with self.lock:
                    settings = dict(self.settings) if self.enabled else {}
                    trades = dict(self.trades)
                jobs = [pool.submit(self.process, symbol, settings.get(symbol), trades.get(symbol), now)
                        for symbol in settings.keys() | trades.keys()]
                for job in jobs:
                    job.result()
                self.wake.wait(max(0, 1 - (time.monotonic() - started)))
                self.wake.clear()
