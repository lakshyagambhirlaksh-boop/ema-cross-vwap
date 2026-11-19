"""Store VWAP values exported from the Delta chart, indexed by candle open time."""

import json
import math
from threading import Lock

from delta_strategy import RESOLUTIONS


class ChartVwap:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def ingest(self, symbol, timeframe, rows, now):
        seconds, _ = RESOLUTIONS[timeframe]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
            raise ValueError("Supply 1 to 2000 closed candle readings.")
        received = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Each reading must be an object.")
            stamp, value = row.get("candle_time"), row.get("vwap")
            if type(stamp) is not int or stamp <= 0 or stamp % seconds or stamp + seconds > now:
                raise ValueError("Use the Delta candle's closed, aligned open timestamp in Unix seconds.")
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Each reading needs a positive Delta chart VWAP.")
            received[str(stamp)] = value
        key = f"{symbol}:{timeframe}"
        with self.lock:
            merged = {**self.values.get(key, {}), **received}
            latest = dict(sorted(merged.items(), key=lambda pair: int(pair[0]))[-2000:])
            state = {**self.values, key: latest}
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(self.path)
            self.values = state
        return len(received)

    def series(self, symbol, timeframe, candles):
        with self.lock:
            values = self.values.get(f"{symbol}:{timeframe}", {})
            return [values.get(str(candle.time)) for candle in candles]
