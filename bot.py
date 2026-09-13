import os
import time
import json
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# SLK BIAS BUILDER v10
# Daily line-chart structure -> approved key-level rejection
# -> H4 line-chart sweep/reclaim -> H4 breakout confirmation
# -> Telegram alert
# Approved key levels ONLY: Resistance, Support, RBS, SBR, OCL, QMR.
# Engulfing OB / strong OB / order-block detectors are intentionally excluded.
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
REQUEST_MIN_INTERVAL = float(os.getenv("REQUEST_MIN_INTERVAL", "8.5"))
RATE_LIMIT_SLEEP = int(os.getenv("RATE_LIMIT_SLEEP", "65"))
PORT = int(os.getenv("PORT", "10000"))
DAILY_CLOCK_SYMBOL = os.getenv("DAILY_CLOCK_SYMBOL", "EUR/USD")
H4_CLOCK_SYMBOL = os.getenv("H4_CLOCK_SYMBOL", "EUR/USD")
MAX_SETUP_AGE_DAYS = int(os.getenv("MAX_SETUP_AGE_DAYS", "10"))
SWING_STRENGTH = int(os.getenv("SWING_STRENGTH", "2"))
TWELVE_DATA_URL = "https://api.twelvedata.com"
DAILY_CACHE_TTL = int(os.getenv("DAILY_CACHE_TTL", "21600"))
H4_CACHE_TTL = int(os.getenv("H4_CACHE_TTL", "900"))

INSTRUMENTS_CONFIG = [
    {"name": "EURUSD", "symbols": ["EUR/USD", "EURUSD"]},
    {"name": "GBPUSD", "symbols": ["GBP/USD", "GBPUSD"]},
    {"name": "USDJPY", "symbols": ["USD/JPY", "USDJPY"]},
    {"name": "USDCHF", "symbols": ["USD/CHF", "USDCHF"]},
    {"name": "AUDUSD", "symbols": ["AUD/USD", "AUDUSD"]},
    {"name": "USDCAD", "symbols": ["USD/CAD", "USDCAD"]},
    {"name": "EURGBP", "symbols": ["EUR/GBP", "EURGBP"]},
    {"name": "EURJPY", "symbols": ["EUR/JPY", "EURJPY"]},
    {"name": "GBPJPY", "symbols": ["GBP/JPY", "GBPJPY"]},
    {"name": "AUDJPY", "symbols": ["AUD/JPY", "AUDJPY"]},
    {"name": "JP225", "symbols": ["JP225", "NIKKEI", "NI225"]},
    {"name": "UK100", "symbols": ["UK100", "FTSE"]},
    {"name": "NAS100", "symbols": ["NAS100", "NDX"]},
    {"name": "XAUUSD", "symbols": ["XAU/USD", "XAUUSD"]},
]

ACTIVE_SETUPS = {}
ALERTED_SETUPS = set()
RESOLVED_SYMBOLS = {}
RESOLUTION_TIMES = {}
DATA_CACHE = {}
INSTRUMENTS = []
LAST_DAILY_CLOCK = None
LAST_H4_CLOCK = None
API_LOCK = threading.Lock()
LAST_API_REQUEST = 0.0


def parse_dt(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def http_get(url, params=None):
    global LAST_API_REQUEST
    if params:
        url += "?" + urlencode(params)
    with API_LOCK:
        wait = REQUEST_MIN_INTERVAL - (time.monotonic() - LAST_API_REQUEST)
        if wait > 0:
            time.sleep(wait)
        LAST_API_REQUEST = time.monotonic()
        try:
            req = Request(url, headers={"User-Agent": "SLK-Bias-Builder/9.0"})
            with urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            print(f"HTTP error {exc.code}")
            if exc.code == 429:
                time.sleep(RATE_LIMIT_SLEEP)
            return None
        except (URLError, Exception) as exc:
            print(f"HTTP error: {exc}")
            return None


def cache_ttl(interval):
    return DAILY_CACHE_TTL if interval == "1day" else H4_CACHE_TTL


def get_time_series(symbol, interval, outputsize=200, force=False):
    if not symbol or not TWELVE_DATA_API_KEY:
        return []
    key = (symbol, interval, int(outputsize))
    now = time.time()
    cached = DATA_CACHE.get(key)
    if cached and not force and now - cached[0] < cache_ttl(interval):
        return cached[1]
    data = http_get(f"{TWELVE_DATA_URL}/time_series", {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    })
    if not data or "values" not in data:
        print(f"No data for {symbol} {interval}: {data.get('message') if data else 'request failed'}")
        return []
    candles = []
    for item in data["values"]:
        try:
            candles.append({
                "datetime": item["datetime"],
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: parse_dt(c["datetime"]) or datetime.min.replace(tzinfo=timezone.utc))
    DATA_CACHE[key] = (now, candles)
    return candles


def completed(candles):
    return candles[:-1] if len(candles) >= 2 else []


def body_direction(c):
    if c["close"] > c["open"]:
        return "BUY"
    if c["close"] < c["open"]:
        return "SELL"
    return None


# A swing is returned only when its right-side confirmation candles
# already exist before the event being evaluated.
def confirmed_swings_before(candles, event_index, kind, strength=SWING_STRENGTH):
    values = [c["close"] for c in candles]
    result = []
    last_candidate = event_index - strength - 1
    for i in range(strength, max(strength, last_candidate + 1)):
        if i + strength >= event_index:
            break
        current = values[i]
        left = values[i-strength:i]
        right = values[i+1:i+strength+1]
        if kind == "high" and all(current > x for x in left + right):
            result.append((i, current))
        if kind == "low" and all(current < x for x in left + right):
            result.append((i, current))
    return result


def latest_prior_swing(candles, event_index, kind):
    swings = confirmed_swings_before(candles, event_index, kind)
    return swings[-1] if swings else None


def detect_bos_before(candles, end_index):
    candidates = []
    for i in range(1, end_index):
        high = latest_prior_swing(candles, i, "high")
        low = latest_prior_swing(candles, i, "low")
        if high and candles[i]["close"] > high[1]:
            candidates.append({"index": i, "datetime": candles[i]["datetime"], "level": high[1], "type": "Bullish BOS"})
        if low and candles[i]["close"] < low[1]:
            candidates.append({"index": i, "datetime": candles[i]["datetime"], "level": low[1], "type": "Bearish BOS"})
    return max(candidates, key=lambda x: x["index"]) if candidates else None


def detect_resistance(candles):
    """Resistance is the bearish A-shape turning level.

    A-shape is not exposed as a separate key-level type; it is labelled
    simply as Resistance. The level is based on a confirmed line-chart
    swing high.
    """
    out = []
    for event in range(len(candles)):
        for index, level in confirmed_swings_before(candles, event, "high"):
            out.append({
                "pattern": "Resistance",
                "level": level,
                "index": index,
                "direction": "SELL",
            })
    return out


def detect_support(candles):
    """Support is the bullish V-shape turning level.

    V-shape is not exposed as a separate key-level type; it is labelled
    simply as Support. The level is based on a confirmed line-chart
    swing low.
    """
    out = []
    for event in range(len(candles)):
        for index, level in confirmed_swings_before(candles, event, "low"):
            out.append({
                "pattern": "Support",
                "level": level,
                "index": index,
                "direction": "BUY",
            })
    return out


def turning_points_for_rbs_sbr(candles):
    """Internal turning points for RBS/SBR.

    These are deliberately internal and are not displayed as A-shape or
    V-shape key levels. RBS is bullish and SBR is bearish.
    """
    return detect_resistance(candles) + detect_support(candles)


def detect_ocl(candles):
    out = []
    for i in range(1, len(candles)):
        d1, d2 = body_direction(candles[i-1]), body_direction(candles[i])
        if d1 and d1 == d2:
            out.append({"pattern": "OCL", "level": candles[i-1]["open"], "index": i, "direction": d1})
    return out


def detect_rbs_sbr(candles):
    """RBS = bullish key level; SBR = bearish key level."""
    out = []
    bases = turning_points_for_rbs_sbr(candles)
    for base in bases:
        level, start = base["level"], base["index"]
        for b in range(start + 1, len(candles)):
            if candles[b]["close"] > level:
                for r in range(b + 1, len(candles)):
                    c = candles[r]
                    if c["low"] <= level <= c["high"] and c["close"] > level:
                        out.append({"pattern": "RBS", "level": level, "index": r, "direction": "BUY"})
                        break
                    if c["close"] < level:
                        break
            if candles[b]["close"] < level:
                for r in range(b + 1, len(candles)):
                    c = candles[r]
                    if c["low"] <= level <= c["high"] and c["close"] < level:
                        out.append({"pattern": "SBR", "level": level, "index": r, "direction": "SELL"})
                        break
                    if c["close"] > level:
                        break
    return out


def detect_qmr(candles):
    """Detect simple confirmed Quasimodo reference levels using the line chart.

    Bearish QM: left-shoulder high -> higher head -> lower right shoulder.
    The reference level is the left-shoulder high.
    Bullish QM: left-shoulder low -> lower head -> higher right shoulder.
    The reference level is the left-shoulder low.
    """
    out = []
    highs = []
    lows = []
    for event in range(len(candles)):
        highs = confirmed_swings_before(candles, event, "high")
        lows = confirmed_swings_before(candles, event, "low")
        if len(highs) >= 2:
            ls_i, ls = highs[-2]
            head_i, head = highs[-1]
            if head > ls:
                later_highs = [x for x in confirmed_swings_before(candles, len(candles), "high") if x[0] > head_i and x[1] < head]
                if later_highs:
                    rs_i, rs = later_highs[0]
                    out.append({"pattern": "QMR", "level": ls, "index": rs_i, "direction": "SELL"})
        if len(lows) >= 2:
            ls_i, ls = lows[-2]
            head_i, head = lows[-1]
            if head < ls:
                later_lows = [x for x in confirmed_swings_before(candles, len(candles), "low") if x[0] > head_i and x[1] > head]
                if later_lows:
                    rs_i, rs = later_lows[0]
                    out.append({"pattern": "QMR", "level": ls, "index": rs_i, "direction": "BUY"})
    unique = {}
    for x in out:
        unique[(x["pattern"], round(x["level"], 8), x["index"], x["direction"])] = x
    return list(unique.values())


def find_key_levels(candles):
    items = (
        detect_resistance(candles)
        + detect_support(candles)
        + detect_ocl(candles)
        + detect_qmr(candles)
        + detect_rbs_sbr(candles)
    )
    unique = {}
    for x in items:
        unique[(x["pattern"], round(x["level"], 8), x["index"], x["direction"])] = x
    return list(unique.values())


def rejection(candle, level, direction):
    if direction == "SELL":
        # Price trades above resistance and closes below it.
        if candle["high"] >= level and candle["close"] < level and candle["close"] < candle["open"]:
            return {"type": "Bearish rejection", "close": candle["close"]}
    else:
        if candle["low"] <= level and candle["close"] > level and candle["close"] > candle["open"]:
            return {"type": "Bullish rejection", "close": candle["close"]}
    return None


def find_daily_setup(candles):
    d = completed(candles)
    if len(d) < 30:
        return None
    for ri in range(len(d)-1, 5, -1):
        history = d[:ri]
        candidates = find_key_levels(history)
        valid = []
        for key in candidates:
            if key["index"] >= ri:
                continue
            rej = rejection(d[ri], key["level"], key["direction"])
            if rej:
                valid.append((key, rej))
        if not valid:
            continue
        key, rej = max(valid, key=lambda pair: pair[0]["index"])
        bos = detect_bos_before(d, key["index"])
        if not bos:
            continue
        if key["direction"] == "SELL" and bos["type"] != "Bearish BOS":
            continue
        if key["direction"] == "BUY" and bos["type"] != "Bullish BOS":
            continue
        target = None
        if key["direction"] == "SELL":
            lows = confirmed_swings_before(d, ri, "low")
            prior = [x for x in lows if x[0] < ri]
            target = prior[-1][1] if prior else None
        else:
            highs = confirmed_swings_before(d, ri, "high")
            prior = [x for x in highs if x[0] < ri]
            target = prior[-1][1] if prior else None
        return {
            "rejection_index": ri,
            "rejection_datetime": d[ri]["datetime"],
            "key_level": key["level"],
            "pattern": key["pattern"],
            "daily_bias": key["direction"],
            "rejection": rej["type"],
            "close": rej["close"],
            "prior_bos": bos,
            "target_level": target,
            "created_at": time.time(),
        }
    return None


def setup_key(s):
    return f"{s['rejection_datetime']}|{s['pattern']}|{s['key_level']}|{s['daily_bias']}"


def daily_invalidated(candles, setup):
    d = completed(candles)
    for c in d[setup["rejection_index"] + 1:]:
        if setup["daily_bias"] == "SELL" and c["close"] > setup["key_level"]:
            return True
        if setup["daily_bias"] == "BUY" and c["close"] < setup["key_level"]:
            return True
    return False


def find_h4_sweep(candles, bias, after_datetime):
    h = completed(candles)
    after = parse_dt(after_datetime)
    if len(h) < 10 or not after:
        return None
    for i in range(len(h)):
        dt = parse_dt(h[i]["datetime"])
        if not dt or dt <= after:
            continue
        kind = "low" if bias == "BUY" else "high"
        swing = latest_prior_swing(h, i, kind)
        if not swing:
            continue
        _, level = swing
        c = h[i]
        if bias == "BUY" and c["low"] < level and c["close"] > level:
            return {"index": i, "datetime": c["datetime"], "level": level}
        if bias == "SELL" and c["high"] > level and c["close"] < level:
            return {"index": i, "datetime": c["datetime"], "level": level}
    return None


def find_h4_breakout_after_sweep(candles, bias, sweep_index):
    """After the liquidity sweep, wait for a line-chart structure break.

    BUY: close above the latest confirmed H4 swing high formed after the sweep.
    SELL: close below the latest confirmed H4 swing low formed after the sweep.
    """
    h = completed(candles)
    if sweep_index is None or sweep_index >= len(h) - 1:
        return None

    for i in range(sweep_index + 1, len(h)):
        kind = "high" if bias == "BUY" else "low"
        swings = [x for x in confirmed_swings_before(h, i, kind) if x[0] > sweep_index]
        if not swings:
            continue
        level = swings[-1][1]
        if bias == "BUY" and h[i]["close"] > level:
            return {"index": i, "datetime": h[i]["datetime"], "level": level, "type": "Bullish H4 CHoCH/BOS"}
        if bias == "SELL" and h[i]["close"] < level:
            return {"index": i, "datetime": h[i]["datetime"], "level": level, "type": "Bearish H4 CHoCH/BOS"}
    return None


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing")
        return False
    data = urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    try:
        req = Request(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, method="POST")
        with urlopen(req, timeout=30) as response:
            return bool(json.loads(response.read().decode()).get("ok"))
    except Exception as exc:
        print(f"Telegram error: {exc}")
        return False


def make_alert(name, setup, sweep, breakout):
    direction = setup["daily_bias"]
    return (
        f"📊 SLK BIAS BUILDER ALERT — {direction}\n\n"
        f"Symbol: {name}\n"
        f"Daily pattern: {setup['pattern']}\n"
        f"Daily BOS: {setup['prior_bos']['type']}\n"
        f"Key level: {setup['key_level']}\n"
        f"Daily rejection: {setup['rejection_datetime']}\n"
        f"H4 sweep/reclaim: {sweep['datetime']}\n"
        f"H4 structure break entry: {breakout['datetime']}\n"
        f"Target level: {setup.get('target_level') or 'Not detected'}\n\n"
        "Sequence: Daily line-chart CHoCH/BOS → key-level rejection → H4 sweep/reclaim → H4 line-chart CHoCH/BOS."
    )


def resolve_symbol(item):
    now = time.time()
    if item["name"] in RESOLVED_SYMBOLS and now - RESOLUTION_TIMES.get(item["name"], 0) < 86400:
        return RESOLVED_SYMBOLS[item["name"]]
    for symbol in item["symbols"]:
        if get_time_series(symbol, "1day", 3):
            RESOLVED_SYMBOLS[item["name"]] = symbol
            RESOLUTION_TIMES[item["name"]] = now
            print(f"Resolved {item['name']} -> {symbol}")
            return symbol
    RESOLVED_SYMBOLS[item["name"]] = None
    RESOLUTION_TIMES[item["name"]] = now
    return None


def scan_daily_setups():
    global LAST_DAILY_CLOCK
    clock = completed(get_time_series(DAILY_CLOCK_SYMBOL, "1day", 3))
    if not clock:
        return False
    latest = clock[-1]["datetime"]
    if latest == LAST_DAILY_CLOCK:
        return False
    LAST_DAILY_CLOCK = latest
    print(f"New completed Daily candle: {latest}")
    for item in INSTRUMENTS:
        symbol = resolve_symbol(item)
        if not symbol:
            continue
        candles = get_time_series(symbol, "1day", 200)
        if not candles:
            continue
        old = ACTIVE_SETUPS.get(item["name"])
        if old and daily_invalidated(candles, old):
            print(f"Invalidated setup: {item['name']}")
            ACTIVE_SETUPS.pop(item["name"], None)
        setup = find_daily_setup(candles)
        if not setup or time.time() - setup["created_at"] > MAX_SETUP_AGE_DAYS * 86400:
            continue
        key = setup_key(setup)
        if key in ALERTED_SETUPS:
            continue
        setup.update({"symbol": symbol, "name": item["name"], "h4_sweep": None})
        ACTIVE_SETUPS[item["name"]] = setup
        print(f"ACTIVE SETUP: {item['name']} | {setup['daily_bias']} | {setup['pattern']}")
    return True


def latest_h4_clock():
    h = completed(get_time_series(H4_CLOCK_SYMBOL, "4h", 3))
    return h[-1]["datetime"] if h else None


def monitor_active_setups():
    global LAST_H4_CLOCK
    if not ACTIVE_SETUPS:
        return
    clock = latest_h4_clock()
    if not clock or clock == LAST_H4_CLOCK:
        return
    LAST_H4_CLOCK = clock
    print(f"New completed H4 candle: {clock}")
    for name in list(ACTIVE_SETUPS):
        setup = ACTIVE_SETUPS.get(name)
        if not setup:
            continue
        h4 = get_time_series(setup["symbol"], "4h", 150)
        if not h4:
            continue
        if setup.get("h4_sweep") is None:
            setup["h4_sweep"] = find_h4_sweep(h4, setup["daily_bias"], setup["rejection_datetime"])
        if not setup.get("h4_sweep"):
            continue
        breakout = find_h4_breakout_after_sweep(h4, setup["daily_bias"], setup["h4_sweep"]["index"])
        if not breakout:
            continue
        key = setup_key(setup)
        if key in ALERTED_SETUPS:
            continue
        if send_telegram(make_alert(name, setup, setup["h4_sweep"], breakout)):
            ALERTED_SETUPS.add(key)
            ACTIVE_SETUPS.pop(name, None)
            print(f"ALERT SENT: {name}")


def scanner_loop():
    global INSTRUMENTS
    if not TWELVE_DATA_API_KEY:
        print("TWELVE_DATA_API_KEY is missing")
        return
    INSTRUMENTS = [dict(x) for x in INSTRUMENTS_CONFIG]
    print(f"Loaded {len(INSTRUMENTS)} instruments")
    while True:
        try:
            scan_daily_setups()
            monitor_active_setups()
        except Exception as exc:
            print(f"Scanner error: {exc}")
        time.sleep(CHECK_INTERVAL)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "running", "service": "SLK Bias Builder v7", "active_setups": len(ACTIVE_SETUPS)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        return


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    print("SLK Bias Builder v7 is running")
    while True:
        time.sleep(60)
