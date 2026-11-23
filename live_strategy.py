"""Run the Delta strategy and reconcile its positions with the exchange."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from threading import Event, Lock, Thread
from urllib.parse import urlencode
from uuid import uuid4

from delta_client import tradeable_product
from delta_strategy import RESOLUTIONS, entry_setup, fetch_candles, rsi_exit


logger = logging.getLogger(__name__)
MAKER_INTERVAL = 2
MAKER_MAX_EDITS = 9
MAKER_DEADLINE = 19


class DeltaOrderError(ValueError):
    def __init__(self, method, path, code):
        super().__init__(f"Delta rejected {method} {path.split('?')[0]} ({code}).")
        self.code = str(code)


def write_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def strategy_order_id(prefix):
    return prefix + uuid4().hex[:32 - len(prefix)]


class LiveStrategy:
    def __init__(self, private, public, journal):
        self.private = private
        self.public = public
        self.journal = journal
        self.run_config = journal.with_suffix(".run.json")
        self.lock = Lock()
        self.wake = Event()
        self.settings = json.loads(self.run_config.read_text()) if self.run_config.exists() else {}
        self.enabled = bool(self.settings)
        self.last_candle = {}
        self.errors = {}
        self.trades = json.loads(journal.read_text()) if journal.exists() else {}
        self.started_at = time.time() if self.enabled else 0

    def launch(self):
        Thread(target=self.run, daemon=True, name="delta-strategy").start()

    def start(self, contracts, timeframe, lots):
        symbols = tuple(contracts)
        if not symbols:
            raise ValueError("Select at least one contract.")
        with self.lock:
            if self.enabled:
                raise ValueError("Stop new entries before changing strategy settings.")

        def check_product(symbol):
            product = tradeable_product(self.public, symbol)
            tick = Decimal(str(product["tick_size"]))
            if not tick.is_finite() or tick <= 0:
                raise ValueError("Invalid Delta tick size for " + symbol)
            return symbol, {"id": int(product["id"]), "tick": str(tick),
                            "timeframe": timeframe, "lots": lots}

        with ThreadPoolExecutor(max_workers=min(5, len(symbols))) as pool:
            products = dict(pool.map(check_product, symbols))
        with self.lock:
            if self.enabled:
                raise ValueError("Stop new entries before changing strategy settings.")
            write_state(self.run_config, products)
            self.settings = products
            self.started_at = time.time()
            self.enabled = True
        self.wake.set()

    def stop(self):
        with self.lock:
            self.enabled = False
            write_state(self.run_config, {})
        self.wake.set()

    def update_lots(self, lots):
        with self.lock:
            if not self.enabled:
                return False
            settings = {symbol: {**product, "lots": lots}
                        for symbol, product in self.settings.items()}
            write_state(self.run_config, settings)
            self.settings = settings
        self.wake.set()
        return True

    def status(self):
        with self.lock:
            settings = next(iter(self.settings.values()), {})
            return {"running": self.enabled, "positions": len(self.trades),
                    "errors": dict(self.errors),
                    "timeframe": settings.get("timeframe") if self.enabled else None}

    def save_trade(self, symbol, trade):
        # Called with the lock held. Persist before sending an entry.
        updated = dict(self.trades)
        if trade is None:
            updated.pop(symbol, None)
        else:
            updated[symbol] = trade
        write_state(self.journal, updated)
        self.trades = updated

    def call(self, method, path, body=None):
        response = self.private(method, path, body)
        if response.get("success") is not True:
            code = (response.get("error") or {}).get("code", "unknown_error")
            raise DeltaOrderError(method, path, code)
        return response.get("result")

    def position(self, product_id):
        return self.call("GET", "/v2/positions?" + urlencode({"product_id": product_id}))

    def market_entry(self, symbol, side, lots, *, client_id):
        return self.call("POST", "/v2/orders", {
            "product_symbol": symbol, "side": side, "size": lots, "order_type": "market_order",
            "time_in_force": "ioc", "reduce_only": False, "client_order_id": client_id,
        })

    def maker_quote(self, symbol, side):
        book = self.public("/v2/l2orderbook/" + symbol + "?depth=1")
        if not book.get("buy") or not book.get("sell"):
            raise ValueError("Delta order book has no bid or ask.")
        bid, ask = (Decimal(str(book[key][0]["price"])) for key in ("buy", "sell"))
        if not all(price.is_finite() for price in (bid, ask)) or not 0 < bid < ask:
            raise ValueError("Delta did not return a usable bid and ask.")
        return bid if side == "buy" else ask

    def save_maker(self, symbol, trade, **changes):
        updated = {**trade, "maker": {**trade["maker"], **changes}}
        with self.lock:
            self.save_trade(symbol, updated)
        return updated

    def submit_maker(self, symbol, trade, price):
        try:
            self.call("POST", "/v2/orders", {
                "product_symbol": symbol, "side": trade["side"], "size": trade["lots"],
                "order_type": "limit_order", "limit_price": format(price, "f"),
                "time_in_force": "gtc", "post_only": True, "client_order_id": trade["client_id"],
            })
        except DeltaOrderError as error:
            if "post_only" not in error.code:
                if error.code.startswith("HTTP ") or error.code in (
                    "unknown_error", "internal_server_error", "internal_error", "request_timeout"
                ):
                    raise
                with self.lock:
                    self.save_trade(symbol, None)
                raise
            self.save_maker(symbol, trade, rejected=True)

    def maker_running(self, trade):
        return self.enabled and self.started_at == trade["maker"]["run_started"]

    def maker_price_is_safe(self, trade, price):
        stop = Decimal(trade["stop"])
        return price > stop if trade["side"] == "buy" else price < stop

    def finish_maker(self, symbol, trade, filled, fallback):
        final = {key: value for key, value in trade.items() if key != "maker"}
        if filled:
            # Cancel the remaining maker size before protecting a partial fill.
            with self.lock:
                self.save_trade(symbol, final)
            self.manage(symbol, final, int(time.time()))
            return
        if fallback:
            price = self.maker_quote(symbol, trade["side"])
            if self.maker_price_is_safe(trade, price) and not int(self.position(trade["id"])["size"]):
                final["client_id"] = strategy_order_id("ema-entry-")
                with self.lock:
                    if self.maker_running(trade):
                        self.save_trade(symbol, final)
                    else:
                        self.save_trade(symbol, None)
                        return
                self.market_entry(symbol, final["side"], final["lots"], client_id=final["client_id"])
                self.manage(symbol, final, int(time.time()))
                return
        with self.lock:
            self.save_trade(symbol, None)

    def cancel_maker(self, symbol, trade, order, fallback):
        trade = self.save_maker(symbol, trade, cancel_requested=True)
        cancelled = self.call("DELETE", "/v2/orders", {"id": order["id"], "product_id": trade["id"]})
        if cancelled and cancelled.get("state") in ("closed", "cancelled"):
            filled = int(cancelled["size"]) - int(cancelled["unfilled_size"])
            self.finish_maker(symbol, trade, filled, fallback and not filled)

    def manage_maker(self, symbol, trade):
        maker = trade["maker"]
        now = time.time()
        with self.lock:
            allowed = self.maker_running(trade)
        expired = now >= maker["deadline"]
        if allowed and not expired and now < maker["next_check"]:
            return

        rejected = maker.get("rejected", False)
        order = None if rejected else self.call("GET", "/v2/orders/client_order_id/" + trade["client_id"])
        now = time.time()
        expired = now >= maker["deadline"]
        with self.lock:
            allowed = self.maker_running(trade)
        if order:
            state = order["state"]
            filled = int(order["size"]) - int(order["unfilled_size"])
            if state not in ("closed", "cancelled"):
                if not allowed or expired or filled:
                    self.cancel_maker(symbol, trade, order, allowed and expired and not maker.get("no_fallback"))
                    return
            else:
                rejected = "post_only" in (order.get("cancellation_reason") or "")
                if filled or not allowed or (expired and maker.get("cancel_requested")) or not rejected:
                    fallback = (allowed and expired and not filled and not maker.get("no_fallback")
                                and (rejected or maker.get("cancel_requested")))
                    self.finish_maker(symbol, trade, filled, fallback)
                    return
        if not allowed or expired:
            self.finish_maker(symbol, trade, 0, allowed and expired and rejected)
            return
        if maker["checks"] >= MAKER_MAX_EDITS:
            return

        price = self.maker_quote(symbol, trade["side"])
        now = time.time()
        with self.lock:
            allowed = self.maker_running(trade)
        if not allowed or now >= maker["deadline"]:
            return
        if not self.maker_price_is_safe(trade, price):
            if rejected:
                self.finish_maker(symbol, trade, 0, False)
            else:
                trade = self.save_maker(symbol, trade, deadline=now, no_fallback=True)
                self.cancel_maker(symbol, trade, order, False)
            return
        trade = self.save_maker(symbol, trade, checks=maker["checks"] + 1,
                                next_check=now + MAKER_INTERVAL)
        if rejected:
            trade = {**trade, "client_id": strategy_order_id("ema-entry-")}
            trade = self.save_maker(symbol, trade, rejected=False)
            self.submit_maker(symbol, trade, price)
        elif price != Decimal(str(order["limit_price"])):
            self.call("PUT", "/v2/orders", {"id": order["id"], "product_id": trade["id"],
                                           "limit_price": format(price, "f"), "post_only": True})

    def protect(self, symbol, trade, size, position):
        entry = Decimal(str(position["entry_price"]))
        stop = Decimal(trade["stop"])
        tick = Decimal(trade["tick"])
        direction = 1 if size > 0 else -1
        risk = direction * (entry - stop)
        target = ((entry + direction * Decimal("1.5") * risk) / tick).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP) * tick
        if risk <= 0 or target <= 0:
            raise ValueError("Fill was outside the valid stop range.")
        with self.lock:
            self.save_trade(symbol, {**trade, "protected": True, "software_target": format(target, "f")})

    def save_limit_exit(self, symbol, trade, **changes):
        updated = {**trade, "limit_exit": {**trade["limit_exit"], **changes}}
        with self.lock:
            self.save_trade(symbol, updated)
        return updated

    def start_limit_exit(self, symbol, trade, size, reason):
        started = time.time()
        trade = {**trade, "limit_exit": {
            "reason": reason, "deadline": started + 20, "next_check": started,
        }}
        with self.lock:
            self.save_trade(symbol, trade)
        self.manage_limit_exit(symbol, trade, size)

    def manage_limit_exit(self, symbol, trade, size):
        state = trade["limit_exit"]
        now = time.time()
        if now < state["next_check"]:
            return
        side = "sell" if size > 0 else "buy"
        client_id = state.get("client_id")
        order = None
        if client_id:
            try:
                order = self.call("GET", "/v2/orders/client_order_id/" + client_id)
            except DeltaOrderError as error:
                if error.code != "order_not_found":
                    raise
                query = urlencode({"product_ids": trade["id"], "states": "open,pending"})
                active = self.call("GET", "/v2/orders?" + query)
                order = next((item for item in active if item.get("client_order_id") == client_id), None)
        if client_id and (not order or order["state"] in ("closed", "cancelled", "rejected")):
            remaining = int(self.position(trade["id"])["size"])
            if not remaining or (remaining > 0) != (size > 0) or abs(remaining) > trade["lots"]:
                return
            size = remaining
            trade = self.save_limit_exit(symbol, trade, client_id=None, deadline=time.time() + 20)
            order = None
        if order and time.time() >= state["deadline"]:
            self.save_limit_exit(symbol, trade, next_check=time.time() + MAKER_INTERVAL)
            return
        price = self.maker_quote(symbol, side)
        trade = self.save_limit_exit(symbol, trade, next_check=time.time() + MAKER_INTERVAL)
        if order:
            if time.time() < state["deadline"] and price != Decimal(str(order["limit_price"])):
                self.call("PUT", "/v2/orders", {
                    "id": order["id"], "product_id": trade["id"],
                    "limit_price": format(price, "f"), "post_only": True,
                })
        else:
            client_id = strategy_order_id(state["reason"])
            trade = self.save_limit_exit(symbol, trade, client_id=client_id)
            try:
                self.call("POST", "/v2/orders", {
                    "product_symbol": symbol, "side": side, "size": abs(size),
                    "order_type": "limit_order", "limit_price": format(price, "f"),
                    "time_in_force": "gtc", "post_only": True, "reduce_only": True,
                    "client_order_id": client_id,
                })
            except DeltaOrderError as error:
                if not error.code.startswith("HTTP ") and error.code not in (
                    "unknown_error", "internal_server_error", "internal_error", "request_timeout"
                ):
                    self.save_limit_exit(symbol, trade, client_id=None)
                raise

    def manage(self, symbol, trade, now):
        if "maker" in trade:
            self.manage_maker(symbol, trade)
            return
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
        if trade.get("limit_exit"):
            self.manage_limit_exit(symbol, trade, size)
            return
        if trade.get("closing"):
            self.start_limit_exit(symbol, trade, size, trade.get("close_reason", "ema-exit-"))
            return
        if not trade.get("protected"):
            trade = {**trade, "filled": True}
            with self.lock:
                self.save_trade(symbol, trade)
            try:
                self.protect(symbol, trade, size, position)
            except (OSError, ValueError, KeyError, ArithmeticError):
                self.start_limit_exit(symbol, trade, size, "ema-save-")
                raise ValueError("Stop and target could not be saved. A reduce-only limit close was requested.")
            return
        if trade.get("software_target"):
            last = Decimal(str(self.public("/v2/tickers/" + symbol)["close"]))
            stop, target = Decimal(trade["stop"]), Decimal(trade["software_target"])
            stop_hit = last <= stop if size > 0 else last >= stop
            target_hit = last >= target if size > 0 else last <= target
            if stop_hit or target_hit:
                reason = "ema-sl-" if stop_hit else "ema-tp-"
                self.start_limit_exit(symbol, trade, size, reason)
                return
        seconds, _ = RESOLUTIONS[trade["timeframe"]]
        traded = fetch_candles(self.public, symbol, trade["timeframe"], now)
        if rsi_exit(traded, seconds, now, size):
            reason = "ema-rsi-"
            with self.lock:
                self.last_candle[symbol] = (now // seconds - 1) * seconds
            self.start_limit_exit(symbol, trade, size, reason)
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
                self.start_limit_exit(symbol, trade, size, "ema-reverse-")

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
        price = self.maker_quote(symbol, side)
        maker_start = time.time()
        trade = {**settings, "side": side, "stop": str(stop), "client_id": strategy_order_id("ema-entry-"),
                 "opened_at": now, "maker": {"deadline": maker_start + MAKER_DEADLINE,
                    "next_check": maker_start + MAKER_INTERVAL, "checks": 0, "run_started": started_at}}
        if not self.maker_price_is_safe(trade, price):
            return
        with self.lock:
            if not self.enabled or self.started_at != started_at:
                return
            trade["lots"] = self.settings[symbol]["lots"]
            self.save_trade(symbol, trade)
        self.submit_maker(symbol, trade, price)

    def process(self, symbol, settings, trade, now):
        try:
            if trade:
                self.manage(symbol, trade, now)
            elif settings:
                self.scan(symbol, settings, now)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            with self.lock:
                message = str(error)
                if self.errors.get(symbol) != message:
                    logger.warning("Delta strategy %s: %s", symbol, message)
                self.errors[symbol] = message
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
