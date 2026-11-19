"""Evaluate the EMA/VWAP rules on Delta India candle data."""

from dataclasses import dataclass
from urllib.parse import urlencode


RESOLUTIONS = {
    "1 min": (60, "1m"), "3 min": (180, "3m"), "5 min": (300, "5m"),
    "10 min": (600, "5m"), "15 min": (900, "15m"), "30 min": (1800, "30m"),
    "1 hr": (3600, "1h"),
}


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def fetch_candles(fetch, symbol, timeframe, now):
    seconds, resolution = RESOLUTIONS[timeframe]
    native_seconds = 300 if timeframe == "10 min" else seconds
    history = min(1999 * native_seconds, max(2 * 86400, 300 * seconds))
    query = urlencode({
        "symbol": symbol, "resolution": resolution,
        "start": now - history, "end": now,
    })
    candles = sorted(
        (Candle(int(row["time"]), *(float(row[key]) for key in ("open", "high", "low", "close")),
                float(row.get("volume") or 0))
         for row in fetch("/v2/history/candles?" + query)),
        key=lambda candle: candle.time,
    )
    if timeframe != "10 min":
        return candles
    groups = {}
    for candle in candles:
        start = candle.time // seconds * seconds
        groups.setdefault(start, []).append(candle)
    return [
        Candle(start, group[0].open, max(c.high for c in group),
               min(c.low for c in group), group[-1].close, sum(c.volume for c in group))
        for start, group in groups.items()
        if [c.time for c in group] == [start, start + 300]
        or (group[0].time == start and now < start + 300)
    ]


def ema(candles, length):
    values = []
    weight = 2 / (length + 1)
    for candle in candles:
        values.append(candle.close if not values else
                      weight * candle.close + (1 - weight) * values[-1])
    return values


def rsi(candles, length=14):
    changes = [current.close - previous.close for previous, current in zip(candles, candles[1:])]
    if len(changes) < length:
        return None
    gains = sum(max(change, 0) for change in changes[:length]) / length
    losses = sum(max(-change, 0) for change in changes[:length]) / length
    for change in changes[length:]:
        gains = (gains * (length - 1) + max(change, 0)) / length
        losses = (losses * (length - 1) + max(-change, 0)) / length
    return 100 - 100 / (1 + gains / losses) if losses else 100


def session_vwap(candles):
    values = []
    session = None
    for candle in candles:
        day = candle.time // 86400
        if day != session:
            session, value, volume = day, 0.0, 0.0
        value += (candle.open + candle.high + candle.low + candle.close) / 4 * candle.volume
        volume += candle.volume
        values.append(value / volume if volume else None)
    return values


def entry_setup(candles, seconds):
    if len(candles) < 4:
        return None
    average, vwap = ema(candles, 8), session_vwap(candles)
    final = len(candles) - 1
    matches = {}
    for index in (final - 1, final - 2):
        if index < 1 or vwap[index] is None or vwap[index - 1] is None:
            continue
        if any(candles[i].time - candles[i - 1].time != seconds for i in range(index, final + 1)):
            continue
        if candles[index - 1].time // 86400 != candles[final].time // 86400:
            continue
        signal, confirmation = candles[index], candles[final]
        if index == final - 2:
            first_close = candles[index + 1].close
            if not min(signal.open, signal.close) <= first_close <= max(signal.open, signal.close):
                continue
        if (average[index - 1] <= vwap[index - 1] and average[index] > vwap[index]
                and signal.close > vwap[index] and confirmation.close > signal.high):
            if "buy" not in matches:
                matches["buy"] = stop_before_signal(candles, vwap, index, "buy")
        if (average[index - 1] >= vwap[index - 1] and average[index] < vwap[index]
                and signal.close < vwap[index] and confirmation.close < signal.low):
            if "sell" not in matches:
                matches["sell"] = stop_before_signal(candles, vwap, index, "sell")
    if len(matches) == 1:
        side, stop = next(iter(matches.items()))
        if stop is not None:
            return side, stop
    return None


def stop_before_signal(candles, vwap, signal_index, side):
    for index in range(signal_index - 1, -1, -1):
        candle, level = candles[index], vwap[index]
        if level is None:
            continue
        if side == "buy" and candle.close < level:
            return candle.low
        if side == "sell" and candle.close > level:
            return candle.high
    return None


def exit_reason(traded, mark, seconds, now, position_size, opened_at=0):
    live = traded and traded[-1].time <= now < traded[-1].time + seconds
    current_rsi = rsi(traded) if live else None
    if current_rsi is not None and (
        (position_size > 0 and current_rsi >= 74) or (position_size < 0 and current_rsi <= 20)
    ):
        return "RSI"
    traded_closed = [candle for candle in traded if candle.time + seconds <= now]
    mark_closed = [candle for candle in mark if candle.time + seconds <= now]
    if not traded_closed or not mark_closed:
        return None
    trade, marked = traded_closed[-1], mark_closed[-1]
    if trade.time != marked.time or now >= trade.time + 2 * seconds or trade.time + seconds <= opened_at:
        return None
    traded_ema, mark_ema = ema(traded_closed, 5)[-1], ema(mark_closed, 5)[-1]
    if not (trade.low <= traded_ema <= trade.high) and not (marked.low <= mark_ema <= marked.high):
        return "EMA 5"
    return None
