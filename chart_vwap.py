"""Store VWAP values exported from the Delta chart, indexed by candle open time."""

import csv
import io
import json
import math
from datetime import datetime
from threading import Lock

from delta_strategy import RESOLUTIONS


def parse_chart_export(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Paste Delta chart data with Time and VWAP columns.")
    text = text.lstrip("\ufeff").strip()
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = {name.strip().lower(): name for name in reader.fieldnames or []}
    timestamp_column = headers.get("time") or headers.get("candle_time")
    value_column = headers.get("vwap")
    if not timestamp_column or not value_column:
        raise ValueError("The header must contain Time (or candle_time) and VWAP.")
    readings = []
    for row in reader:
        try:
            stamp = row[timestamp_column].strip()
            if stamp.isdecimal():
                seconds = int(stamp)
            else:
                date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if date.utcoffset() is None or date.microsecond:
                    raise ValueError
                seconds = int(date.timestamp())
            price = float(row[value_column].strip())
        except (AttributeError, OverflowError, TypeError, ValueError):
            raise ValueError(
                f"Line {reader.line_num} needs Unix seconds or a date with timezone, and a numeric VWAP."
            ) from None
        readings.append({"candle_time": seconds, "vwap": price})
        if len(readings) > 2000:
            raise ValueError("Import at most 2000 closed candles at a time.")
    return readings


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
