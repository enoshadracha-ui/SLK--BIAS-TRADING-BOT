import os
import time
import json
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock
from datetime import datetime, timezone


# ============================================================
# SLK BIAS BOT
#
# W1 -> D1 EXTERNAL BO
# D1 -> H4 EXTERNAL BO
#
# BIAS ONLY
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

TWELVE_DATA_API_KEY = os.getenv(
    "TWELVE_DATA_API_KEY", ""
).strip()

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
).strip()

# 4 hours
SCAN_INTERVAL = int(
    os.getenv("SCAN_INTERVAL", "14400")
)

PORT = int(
    os.getenv("PORT", "10000")
)


# ============================================================
# FIXED 14 INSTRUMENTS
# ============================================================

INSTRUMENTS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "USDCHF": "USD/CHF",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",

    "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
    "AUDJPY": "AUD/JPY",

    "JP225": "JP225",
    "UK100": "UK100",
    "NAS100": "NAS100",
    "XAUUSD": "XAU/USD",
}


# ============================================================
# SETTINGS
# ============================================================

PIVOT_STRENGTH = 2
MIN_BARS = 40
KEY_LOOKBACK = 100
LEVEL_TOLERANCE = 0.0025


# ============================================================
# STATE
# ============================================================

cache = {}
sent_signals = {}

state_lock = Lock()

last_scan_time = None

# ------------------------------------------------------------
# API RATE LIMIT
#
# Free plan = 8 credits/minute.
# We deliberately stay below that.
# ------------------------------------------------------------

api_lock = Lock()

last_api_call = 0

API_DELAY = 9.0


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        body = json.dumps({
            "status": "ok",
            "bot": "SLK Bias Trading Bot",
            "instruments": len(INSTRUMENTS),
            "last_scan": last_scan_time
        }).encode("utf-8")

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"Health server running on port {PORT}"
    )

    server.serve_forever()


# ============================================================
# HELPERS
# ============================================================

def safe_float(value):

    try:
        return float(value)

    except Exception:
        return None


def format_price(value):

    if value is None:
        return "N/A"

    if abs(value) >= 1000:
        return f"{value:.2f}"

    if abs(value) >= 100:
        return f"{value:.2f}"

    if abs(value) >= 10:
        return f"{value:.3f}"

    return f"{value:.5f}"


def sort_candles(candles):

    return sorted(
        candles,
        key=lambda x: x["datetime"]
    )


def remove_current_candle(candles):

    if len(candles) <= 2:
        return candles

    return candles[:-1]


# ============================================================
# TWELVE DATA API
# ============================================================

def api_get(endpoint, params):

    global last_api_call

    # --------------------------------------------------------
    # RATE LIMITER
    # Maximum approximately 6-7 requests/minute.
    # --------------------------------------------------------

    with api_lock:

        now = time.time()

        wait_time = (
            API_DELAY
            - (now - last_api_call)
        )

        if wait_time > 0:

            print(
                f"API limiter: waiting "
                f"{wait_time:.1f}s"
            )

            time.sleep(
                wait_time
            )

        last_api_call = time.time()

    params = dict(params)

    params["apikey"] = (
        TWELVE_DATA_API_KEY
    )

    # Explicit UTC timestamps.
    params["timezone"] = "UTC"

    url = (
        "https://api.twelvedata.com"
        + endpoint
        + "?"
        + urllib.parse.urlencode(params)
    )

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                "SLK-Bias-Trading-Bot/1.0"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            return json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except Exception as error:

        print(
            f"API error: {error}"
        )

        return None


# ============================================================
# CANDLE DATA
# ============================================================

def get_candles(
    symbol,
    interval,
    outputsize=180
):

    key = (
        symbol,
        interval
    )

    cached = cache.get(key)

    if cached:

        cached_time, candles = cached

        # Do not repeatedly request unchanged data.
        if (
            time.time()
            - cached_time
            < 300
        ):

            return candles

    result = api_get(
        "/time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "order": "asc"
        }
    )

    if not result:
        return []

    if result.get("status") != "ok":

        print(
            f"{symbol} {interval}: "
            f"{result.get('message', 'API error')}"
        )

        return []

    values = result.get(
        "values",
        []
    )

    candles = []

    for item in values:

        candle = {
            "datetime":
                item.get("datetime"),

            "open":
                safe_float(
                    item.get("open")
                ),

            "high":
                safe_float(
                    item.get("high")
                ),

            "low":
                safe_float(
                    item.get("low")
                ),

            "close":
                safe_float(
                    item.get("close")
                )
        }

        if (
            candle["datetime"]
            and candle["close"] is not None
        ):

            candles.append(candle)

    candles = sort_candles(
        candles
    )

    cache[key] = (
        time.time(),
        candles
    )

    return candles


# ============================================================
# LINE-CHART STRUCTURE
# ============================================================

def is_pivot_high(
    candles,
    index
):

    s = PIVOT_STRENGTH

    if (
        index < s
        or index + s >= len(candles)
    ):
        return False

    price = candles[index]["close"]

    for i in range(
        index - s,
        index + s + 1
    ):

        if i == index:
            continue

        if candles[i]["close"] >= price:
            return False

    return True


def is_pivot_low(
    candles,
    index
):

    s = PIVOT_STRENGTH

    if (
        index < s
        or index + s >= len(candles)
    ):
        return False

    price = candles[index]["close"]

    for i in range(
        index - s,
        index + s + 1
    ):

        if i == index:
            continue

        if candles[i]["close"] <= price:
            return False

    return True


def get_pivots(candles):

    highs = []
    lows = []

    for i in range(
        PIVOT_STRENGTH,
        len(candles)
        - PIVOT_STRENGTH
    ):

        if is_pivot_high(
            candles,
            i
        ):

            highs.append({
                "index": i,
                "price":
                    candles[i]["close"],
                "time":
                    candles[i]["datetime"]
            })

        if is_pivot_low(
            candles,
            i
        ):

            lows.append({
                "index": i,
                "price":
                    candles[i]["close"],
                "time":
                    candles[i]["datetime"]
            })

    return highs, lows


# ============================================================
# BRS
# ============================================================

def find_latest_brs(candles):

    highs, lows = get_pivots(
        candles
    )

    events = []

    for pivot in highs:

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                > pivot["price"]
            ):

                events.append({
                    "direction": "BUY",
                    "index": i,
                    "time":
                        candles[i]["datetime"],
                    "level":
                        pivot["price"],
                    "pivot_time":
                        pivot["time"],
                    "type":
                        "Bullish BRS"
                })

                break

    for pivot in lows:

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                < pivot["price"]
            ):

                events.append({
                    "direction": "SELL",
                    "index": i,
                    "time":
                        candles[i]["datetime"],
                    "level":
                        pivot["price"],
                    "pivot_time":
                        pivot["time"],
                    "type":
                        "Bearish BRS"
                })

                break

    if not events:
        return None

    events.sort(
        key=lambda x: x["index"]
    )

    return events[-1]


# ============================================================
# KEY LEVELS
# ============================================================

def near_level(
    price,
    level
):

    if not price:
        return False

    return (
        abs(price - level)
        / abs(price)
        <= LEVEL_TOLERANCE
    )


def find_support_resistance(
    candles
):

    highs, lows = get_pivots(
        candles
    )

    levels = []

    for pivot in highs[-15:]:

        levels.append({
            "type": "Resistance",
            "level":
                pivot["price"],
            "time":
                pivot["time"]
        })

    for pivot in lows[-15:]:

        levels.append({
            "type": "Support",
            "level":
                pivot["price"],
            "time":
                pivot["time"]
        })

    return levels


def find_ocl(candles):

    levels = []

    start = max(
        1,
        len(candles)
        - KEY_LOOKBACK
    )

    for i in range(
        start,
        len(candles)
    ):

        a = candles[i - 1]
        b = candles[i]

        bullish = (
            a["close"] > a["open"]
            and b["close"] > b["open"]
        )

        bearish = (
            a["close"] < a["open"]
            and b["close"] < b["open"]
        )

        if bullish or bearish:

            level = (
                a["open"]
                + a["close"]
            ) / 2

            levels.append({
                "type": "OCL",
                "level": level,
                "time":
                    a["datetime"]
            })

    return levels


def find_rbs_sbr(candles):

    highs, lows = get_pivots(
        candles
    )

    levels = []

    # RBS
    for pivot in highs[-12:]:

        level = pivot["price"]

        broken_at = None

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                > level
            ):

                broken_at = i
                break

        if broken_at is None:
            continue

        for i in range(
            broken_at + 1,
            len(candles)
        ):

            if (
                candles[i]["low"] <= level
                and candles[i]["close"] > level
            ):

                levels.append({
                    "type": "RBS",
                    "level": level,
                    "time":
                        candles[i]["datetime"]
                })

                break

    # SBR
    for pivot in lows[-12:]:

        level = pivot["price"]

        broken_at = None

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                < level
            ):

                broken_at = i
                break

        if broken_at is None:
            continue

        for i in range(
            broken_at + 1,
            len(candles)
        ):

            if (
                candles[i]["high"] >= level
                and candles[i]["close"] < level
            ):

                levels.append({
                    "type": "SBR",
                    "level": level,
                    "time":
                        candles[i]["datetime"]
                })

                break

    return levels


# ============================================================
# QMR
# ============================================================

def find_qmr(candles):

    highs, lows = get_pivots(
        candles
    )

    levels = []

    # Bearish QMR
    for i in range(
        len(highs) - 2
    ):

        left = highs[i]
        head = highs[i + 1]
        right = highs[i + 2]

        if not (
            left["index"]
            < head["index"]
            < right["index"]
        ):
            continue

        if (
            head["price"]
            <= left["price"]
        ):
            continue

        between = [
            x for x in lows
            if (
                left["index"]
                < x["index"]
                < head["index"]
            )
        ]

        if not between:
            continue

        neckline = between[-1]

        broken = False

        for j in range(
            head["index"] + 1,
            right["index"] + 1
        ):

            if (
                candles[j]["close"]
                < neckline["price"]
            ):

                broken = True
                break

        if not broken:
            continue

        if (
            right["price"]
            >= head["price"]
        ):
            continue

        levels.append({
            "type": "QMR",
            "level":
                left["price"],
            "time":
                left["time"],
            "direction": "SELL"
        })

    # Bullish QMR
    for i in range(
        len(lows) - 2
    ):

        left = lows[i]
        head = lows[i + 1]
        right = lows[i + 2]

        if not (
            left["index"]
            < head["index"]
            < right["index"]
        ):
            continue

        if (
            head["price"]
            >= left["price"]
        ):
            continue

        between = [
            x for x in highs
            if (
                left["index"]
                < x["index"]
                < head["index"]
            )
        ]

        if not between:
            continue

        neckline = between[-1]

        broken = False

        for j in range(
            head["index"] + 1,
            right["index"] + 1
        ):

            if (
                candles[j]["close"]
                > neckline["price"]
            ):

                broken = True
                break

        if not broken:
            continue

        if (
            right["price"]
            <= head["price"]
        ):
            continue

        levels.append({
            "type": "QMR",
            "level":
                left["price"],
            "time":
                left["time"],
            "direction": "BUY"
        })

    return levels


def get_key_levels(candles):

    levels = []

    levels.extend(
        find_support_resistance(
            candles
        )
    )

    levels.extend(
        find_rbs_sbr(
            candles
        )
    )

    levels.extend(
        find_ocl(
            candles
        )
    )

    levels.extend(
        find_qmr(
            candles
        )
    )

    return levels


# ============================================================
# HTF REJECTION
# ============================================================

def is_rejection(
    candle,
    level,
    direction
):

    if not (
        candle["high"]
        and candle["low"]
        and candle["close"]
    ):
        return False

    touched = (
        candle["low"]
        <= level
        <= candle["high"]
    )

    if not touched:
        return False

    if direction == "BUY":

        return (
            candle["close"]
            > level
        )

    if direction == "SELL":

        return (
            candle["close"]
            < level
        )

    return False


def find_htf_rejection(
    candles,
    brs,
    levels
):

    direction = brs["direction"]

    candidates = []

    for level_data in levels:

        level = level_data[
            "level"
        ]

        level_type = level_data[
            "type"
        ]

        if (
            level_type == "QMR"
            and level_data.get(
                "direction"
            ) != direction
        ):
            continue

        for i in range(
            brs["index"] + 1,
            len(candles)
        ):

            if is_rejection(
                candles[i],
                level,
                direction
            ):

                candidates.append({
                    "direction":
                        direction,

                    "level_type":
                        level_type,

                    "level":
                        level,

                    "level_time":
                        level_data["time"],

                    "rejection_index":
                        i,

                    "rejection_time":
                        candles[i]["datetime"],

                    "close":
                        candles[i]["close"]
                })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x:
            x["rejection_index"]
    )

    return candidates[-1]


# ============================================================
# EXTERNAL BREAKOUT
# ============================================================

def find_external_bo(
    candles,
    direction,
    rejection_time
):

    highs, lows = get_pivots(
        candles
    )

    if direction == "BUY":

        candidates = [
            p for p in highs
            if p["time"] > rejection_time
        ]

        if not candidates:
            return None

        external = candidates[-1]

        for i in range(
            external["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                > external["price"]
            ):

                return {
                    "direction": "BUY",
                    "level":
                        external["price"],
                    "pivot_time":
                        external["time"],
                    "break_time":
                        candles[i]["datetime"],
                    "close":
                        candles[i]["close"]
                }

    if direction == "SELL":

        candidates = [
            p for p in lows
            if p["time"] > rejection_time
        ]

        if not candidates:
            return None

        external = candidates[-1]

        for i in range(
            external["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                < external["price"]
            ):

                return {
                    "direction": "SELL",
                    "level":
                        external["price"],
                    "pivot_time":
                        external["time"],
                    "break_time":
                        candles[i]["datetime"],
                    "close":
                        candles[i]["close"]
                }

    return None


# ============================================================
# BUILD BIAS
# ============================================================

def build_bias(
    symbol,
    htf_name,
    htf_interval,
    lower_name,
    lower_interval
):

    raw_htf = get_candles(
        symbol,
        htf_interval,
        180
    )

    if len(raw_htf) < MIN_BARS:
        return None

    htf = remove_current_candle(
        raw_htf
    )

    brs = find_latest_brs(
        htf
    )

    if not brs:
        return None

    levels = get_key_levels(
        htf
    )

    rejection = find_htf_rejection(
        htf,
        brs,
        levels
    )

    if not rejection:
        return None

    raw_lower = get_candles(
        symbol,
        lower_interval,
        220
    )

    if len(raw_lower) < MIN_BARS:
        return None

    lower = remove_current_candle(
        raw_lower
    )

    external = find_external_bo(
        lower,
        rejection["direction"],
        rejection["rejection_time"]
    )

    if not external:
        return None

    return {
        "symbol": symbol,

        "bias":
            rejection["direction"],

        "htf":
            htf_name,

        "lower_tf":
            lower_name,

        "brs_type":
            brs["type"],

        "brs_level":
            brs["level"],

        "brs_time":
            brs["time"],

        "key_type":
            rejection["level_type"],

        "key_level":
            rejection["level"],

        "key_time":
            rejection["level_time"],

        "rejection_time":
            rejection["rejection_time"],

        "htf_close":
            rejection["close"],

        "external_level":
            external["level"],

        "external_pivot_time":
            external["pivot_time"],

        "external_break_time":
            external["break_time"],

        "external_close":
            external["close"]
    }


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def get_signal_id(signal):

    return "|".join([
        signal["symbol"],
        signal["bias"],
        signal["htf"],
        signal["brs_time"],
        signal["key_type"],
        signal["rejection_time"],
        signal["external_break_time"]
    ])


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id":
            TELEGRAM_CHAT_ID,
        "text":
            message
    }).encode("utf-8")

    try:

        request = urllib.request.Request(
            url,
            data=data,
            method="POST"
        )

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            response.read()

        return True

    except Exception as error:

        print(
            f"Telegram error: {error}"
        )

        return False


def make_alert(signal):

    name = signal["symbol"]

    direction = signal[
        "bias"
    ]

    return (
        f"🚨 {direction} · "
        f"{name} · "
        f"{signal['htf']}→"
        f"{signal['lower_tf']}\n\n"

        f"External breakout confirmed\n\n"

        f"HTF BRS: "
        f"{signal['brs_type']}\n"

        f"BRS level: "
        f"{format_price(signal['brs_level'])}\n"

        f"BRS formed: "
        f"{signal['brs_time']}\n\n"

        f"Key Level: "
        f"{signal['key_type']} @ "
        f"{format_price(signal['key_level'])}\n"

        f"Key level formed: "
        f"{signal['key_time']}\n\n"

        f"HTF rejection: "
        f"{signal['rejection_time']}\n"

        f"HTF close: "
        f"{format_price(signal['htf_close'])}\n\n"

        f"External BO: "
        f"{signal['lower_tf']}\n"

        f"Broke: "
        f"{format_price(signal['external_level'])}\n"

        f"Break confirmed: "
        f"{signal['external_break_time']}\n"

        f"Close: "
        f"{format_price(signal['external_close'])}\n\n"

        f"✅ SLK Bias Confirmed\n"

        f"⚠️ Not an entry signal. "
        f"Bias only — wait for "
        f"your entry model."
    )


# ============================================================
# SCAN
# ============================================================

def scan_instrument(
    name,
    symbol
):

    signals = []

    print(
        f"Checking {name} -> {symbol}"
    )

    # W1 -> D1
    try:

        signal = build_bias(
            symbol,
            "W1",
            "1week",
            "D1",
            "1day"
        )

        if signal:
            signals.append(
                signal
            )

    except Exception as error:

        print(
            f"W1 error {name}: "
            f"{error}"
        )

    # D1 -> H4
    try:

        signal = build_bias(
            symbol,
            "D1",
            "1day",
            "H4",
            "4h"
        )

        if signal:
            signals.append(
                signal
            )

    except Exception as error:

        print(
            f"D1 error {name}: "
            f"{error}"
        )

    return signals


def scan_market():

    global last_scan_time

    print(
        "\n================================"
    )

    print(
        "STARTING SLK MARKET SCAN"
    )

    print(
        "14 instruments"
    )

    print(
        "Rate limit protected"
    )

    print(
        "================================"
    )

    for name, symbol in INSTRUMENTS.items():

        signals = scan_instrument(
            name,
            symbol
        )

        for signal in signals:

            sid = get_signal_id(
                signal
            )

            with state_lock:

                if sid in sent_signals:

                    print(
                        "Duplicate signal "
                        "ignored."
                    )

                    continue

            message = make_alert(
                signal
            )

            print(
                "\nNEW SIGNAL\n"
                + message
            )

            if send_telegram(
                message
            ):

                with state_lock:

                    sent_signals[
                        sid
                    ] = time.time()

    last_scan_time = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    print(
        "\nSCAN COMPLETED: "
        + last_scan_time
    )


# ============================================================
# LOOP
# ============================================================

def scanner_loop():

    time.sleep(5)

    while True:

        try:

            scan_market()

        except Exception as error:

            print(
                f"Scanner error: "
                f"{error}"
            )

        print(
            f"Next scan in "
            f"{SCAN_INTERVAL} seconds."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY missing."
        )

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN missing."
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID missing."
        )

    print(
        "================================"
    )

    print(
        "SLK BIAS TRADING BOT"
    )

    print(
        "================================"
    )

    print(
        "14 instruments loaded."
    )

    print(
        "W1 -> D1 External BO"
    )

    print(
        "D1 -> H4 External BO"
    )

    print(
        "API rate protection ON"
    )

    print(
        "Scan interval: "
        f"{SCAN_INTERVAL}s"
    )

    print(
        "================================"
    )

    Thread(
        target=start_server,
        daemon=True
    ).start()

    Thread(
        target=scanner_loop,
        daemon=True
    ).start()

    while True:

        time.sleep(60)


if __name__ == "__main__":
    main()
