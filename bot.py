import os
import time
import json
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock
from datetime import datetime, timezone


# ============================================================
# SLK BIAS TRADING BOT
#
# WEEKLY PATH:
# W1 BRS -> W1 KEY-LEVEL REJECTION -> D1 EXTERNAL BO
#
# DAILY PATH:
# D1 BRS -> D1 KEY-LEVEL REJECTION -> H4 EXTERNAL BO
#
# BIAS ONLY
# NO ENTRY
# NO SL
# NO TP
# NO TRADE EXECUTION
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

SCAN_INTERVAL = int(
    os.getenv("SCAN_INTERVAL", "900")
)

PORT = int(
    os.getenv("PORT", "10000")
)


# ============================================================
# FIXED 14 INSTRUMENTS
# ============================================================

INSTRUMENTS = {
    "EURUSD": ["EUR/USD"],
    "GBPUSD": ["GBP/USD"],
    "USDJPY": ["USD/JPY"],
    "USDCHF": ["USD/CHF"],
    "AUDUSD": ["AUD/USD"],
    "USDCAD": ["USD/CAD"],

    "EURGBP": ["EUR/GBP"],
    "EURJPY": ["EUR/JPY"],
    "GBPJPY": ["GBP/JPY"],
    "AUDJPY": ["AUD/JPY"],

    "JP225": [
        "JP225",
        "NIKKEI",
        "NI225"
    ],

    "UK100": [
        "UK100",
        "FTSE"
    ],

    "NAS100": [
        "NAS100",
        "NDX"
    ],

    "XAUUSD": [
        "XAU/USD",
        "XAUUSD"
    ],
}


DISPLAY_NAMES = {
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "USD/CHF": "USDCHF",
    "AUD/USD": "AUDUSD",
    "USD/CAD": "USDCAD",

    "EUR/GBP": "EURGBP",
    "EUR/JPY": "EURJPY",
    "GBP/JPY": "GBPJPY",
    "AUD/JPY": "AUDJPY",

    "JP225": "JP225",
    "NIKKEI": "JP225",
    "NI225": "JP225",

    "UK100": "UK100",
    "FTSE": "UK100",

    "NAS100": "NAS100",
    "NDX": "NAS100",

    "XAU/USD": "XAUUSD",
    "XAUUSD": "XAUUSD",
}


# ============================================================
# SETTINGS
# ============================================================

PIVOT_STRENGTH = int(
    os.getenv("PIVOT_STRENGTH", "2")
)

MIN_BARS = 40

KEY_LOOKBACK = int(
    os.getenv("KEY_LOOKBACK", "100")
)

LEVEL_TOLERANCE = float(
    os.getenv("LEVEL_TOLERANCE", "0.0025")
)


# ============================================================
# STATE
# ============================================================

cache = {}

resolved_symbols = {}

sent_signals = {}

state_lock = Lock()

last_scan_time = None


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        response = {
            "status": "ok",
            "bot": "SLK Bias Trading Bot",
            "instruments": 14,
            "last_scan": last_scan_time
        }

        body = json.dumps(
            response
        ).encode("utf-8")

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

    # Twelve Data can return the currently forming candle.
    # We only use completed candles.
    return candles[:-1]


# ============================================================
# TWELVE DATA
# ============================================================

def api_get(endpoint, params):

    params = dict(params)

    params["apikey"] = TWELVE_DATA_API_KEY

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

        if (
            time.time()
            - cached_time
            < 60
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
                ),
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
# SYMBOL RESOLUTION
# ============================================================

def resolve_symbol(name):

    if name in resolved_symbols:

        return resolved_symbols[name]

    candidates = INSTRUMENTS[name]

    for candidate in candidates:

        test = get_candles(
            candidate,
            "1day",
            5
        )

        if test:

            resolved_symbols[
                name
            ] = candidate

            print(
                f"{name} -> {candidate}"
            )

            return candidate

    print(
        f"Could not resolve {name}"
    )

    return None


# ============================================================
# LINE-CHART PIVOTS
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

    end = (
        len(candles)
        - PIVOT_STRENGTH
    )

    for i in range(
        PIVOT_STRENGTH,
        end
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
# HIGHER-TIMEFRAME BRS
# ============================================================

def find_latest_brs(candles):

    highs, lows = get_pivots(
        candles
    )

    events = []

    # --------------------------------------------------------
    # BULLISH BRS
    # Latest confirmed pivot HIGH broken by a close above it.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # BEARISH BRS
    # Latest confirmed pivot LOW broken by a close below it.
    # --------------------------------------------------------

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
# KEY LEVEL HELPERS
# ============================================================

def near_level(
    price,
    level
):

    if price == 0:
        return False

    return (
        abs(price - level)
        / abs(price)
        <= LEVEL_TOLERANCE
    )


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

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


# ============================================================
# OCL
# ============================================================

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

        previous = candles[i - 1]
        current = candles[i]

        previous_bull = (
            previous["close"]
            > previous["open"]
        )

        current_bull = (
            current["close"]
            > current["open"]
        )

        previous_bear = (
            previous["close"]
            < previous["open"]
        )

        current_bear = (
            current["close"]
            < current["open"]
        )

        if (
            previous_bull
            and current_bull
        ):

            level = (
                previous["open"]
                + previous["close"]
            ) / 2

            levels.append({
                "type": "OCL",
                "level": level,
                "time":
                    previous["datetime"]
            })

        elif (
            previous_bear
            and current_bear
        ):

            level = (
                previous["open"]
                + previous["close"]
            ) / 2

            levels.append({
                "type": "OCL",
                "level": level,
                "time":
                    previous["datetime"]
            })

    return levels


# ============================================================
# RBS / SBR
# ============================================================

def find_rbs_sbr(candles):

    highs, lows = get_pivots(
        candles
    )

    levels = []

    # ---------------- RBS ----------------

    for pivot in highs[-12:]:

        level = pivot["price"]

        broken = False
        break_index = None

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                > level
            ):

                broken = True
                break_index = i
                break

        if not broken:
            continue

        for i in range(
            break_index + 1,
            len(candles)
        ):

            candle = candles[i]

            if (
                candle["low"] <= level
                and candle["close"] > level
            ):

                levels.append({
                    "type": "RBS",
                    "level": level,
                    "time":
                        candle["datetime"]
                })

                break

    # ---------------- SBR ----------------

    for pivot in lows[-12:]:

        level = pivot["price"]

        broken = False
        break_index = None

        for i in range(
            pivot["index"] + 1,
            len(candles)
        ):

            if (
                candles[i]["close"]
                < level
            ):

                broken = True
                break_index = i
                break

        if not broken:
            continue

        for i in range(
            break_index + 1,
            len(candles)
        ):

            candle = candles[i]

            if (
                candle["high"] >= level
                and candle["close"] < level
            ):

                levels.append({
                    "type": "SBR",
                    "level": level,
                    "time":
                        candle["datetime"]
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

    # --------------------------------------------------------
    # BEARISH QMR
    #
    # Left Shoulder High
    # -> Head Higher High
    # -> break of intervening low
    # -> Right Shoulder Lower High
    #
    # QMR LEVEL = LEFT SHOULDER HIGH
    # --------------------------------------------------------

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

        middle_lows = [
            x for x in lows
            if (
                left["index"]
                < x["index"]
                < head["index"]
            )
        ]

        if not middle_lows:
            continue

        neckline = middle_lows[-1]

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

    # --------------------------------------------------------
    # BULLISH QMR
    #
    # Left Shoulder Low
    # -> Head Lower Low
    # -> break of intervening high
    # -> Right Shoulder Higher Low
    #
    # QMR LEVEL = LEFT SHOULDER LOW
    # --------------------------------------------------------

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

        middle_highs = [
            x for x in highs
            if (
                left["index"]
                < x["index"]
                < head["index"]
            )
        ]

        if not middle_highs:
            continue

        neckline = middle_highs[-1]

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


# ============================================================
# ALL KEY LEVELS
# ============================================================

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

    high = candle["high"]
    low = candle["low"]
    close = candle["close"]

    if (
        high is None
        or low is None
        or close is None
    ):
        return False

    touched = (
        low <= level <= high
        or near_level(close, level)
    )

    if not touched:
        return False

    if direction == "BUY":

        # Bullish rejection:
        # price reaches level and closes above it.
        return close > level

    if direction == "SELL":

        # Bearish rejection:
        # price reaches level and closes below it.
        return close < level

    return False


def find_htf_rejection(
    candles,
    brs,
    levels
):

    direction = brs["direction"]

    candidates = []

    for level_data in levels:

        level_type = level_data[
            "type"
        ]

        level = level_data[
            "level"
        ]

        # QMR direction must agree.
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

            candle = candles[i]

            if is_rejection(
                candle,
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
                        candle["datetime"],

                    "close":
                        candle["close"]
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

    # --------------------------------------------------------
    # ONLY STRUCTURE AFTER HTF REJECTION
    # --------------------------------------------------------

    if direction == "BUY":

        external_candidates = [
            p for p in highs
            if p["time"] > rejection_time
        ]

        if not external_candidates:
            return None

        # Most recent external high.
        external = external_candidates[-1]

        for i in range(
            external["index"] + 1,
            len(candles)
        ):

            candle = candles[i]

            # External BO requires a CLOSE above the high.
            if (
                candle["close"]
                > external["price"]
            ):

                return {
                    "direction": "BUY",
                    "type":
                        "External BO",
                    "level":
                        external["price"],
                    "pivot_time":
                        external["time"],
                    "break_time":
                        candle["datetime"],
                    "close":
                        candle["close"]
                }

    elif direction == "SELL":

        external_candidates = [
            p for p in lows
            if p["time"] > rejection_time
        ]

        if not external_candidates:
            return None

        # Most recent external low.
        external = external_candidates[-1]

        for i in range(
            external["index"] + 1,
            len(candles)
        ):

            candle = candles[i]

            # External BO requires a CLOSE below the low.
            if (
                candle["close"]
                < external["price"]
            ):

                return {
                    "direction": "SELL",
                    "type":
                        "External BO",
                    "level":
                        external["price"],
                    "pivot_time":
                        external["time"],
                    "break_time":
                        candle["datetime"],
                    "close":
                        candle["close"]
                }

    return None


# ============================================================
# BUILD ONE BIAS
# ============================================================

def build_bias(
    symbol,
    htf_name,
    htf_interval,
    lower_name,
    lower_interval
):

    # ========================================================
    # 1. HIGHER TIMEFRAME
    # ========================================================

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

    if len(htf) < MIN_BARS:
        return None

    # ========================================================
    # 2. FIND CURRENT HTF BRS
    # ========================================================

    brs = find_latest_brs(
        htf
    )

    if not brs:
        return None

    # ========================================================
    # 3. FIND KEY LEVEL
    # ========================================================

    key_levels = get_key_levels(
        htf
    )

    if not key_levels:
        return None

    # ========================================================
    # 4. WAIT FOR HTF REJECTION
    # ========================================================

    rejection = find_htf_rejection(
        htf,
        brs,
        key_levels
    )

    if not rejection:
        return None

    # ========================================================
    # 5. MOVE ONE TIMEFRAME LOWER
    # ========================================================

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

    if len(lower) < MIN_BARS:
        return None

    # ========================================================
    # 6. WAIT FOR EXTERNAL BO
    # ========================================================

    external_bo = find_external_bo(
        lower,
        rejection["direction"],
        rejection["rejection_time"]
    )

    if not external_bo:
        return None

    # ========================================================
    # 7. FINAL BIAS
    # ========================================================

    return {
        "symbol":
            symbol,

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
            external_bo["level"],

        "external_pivot_time":
            external_bo["pivot_time"],

        "external_break_time":
            external_bo["break_time"],

        "external_close":
            external_bo["close"],
    }


# ============================================================
# SIGNAL ID
# ============================================================

def get_signal_id(signal):

    return "|".join([
        signal["symbol"],
        signal["bias"],
        signal["htf"],
        signal["lower_tf"],
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

    symbol = DISPLAY_NAMES.get(
        signal["symbol"],
        signal["symbol"]
    )

    direction = signal[
        "bias"
    ]

    return (
        f"🚨 {direction} · "
        f"{symbol} · "
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

        f"Trend aligned with "
        f"{signal['htf']}\n"

        f"✅ SLK Bias Confirmed\n"

        f"⚠️ Not an entry signal. "
        f"Bias only — wait for "
        f"your entry model."
    )


# ============================================================
# SCAN ONE INSTRUMENT
# ============================================================

def scan_instrument(name):

    symbol = resolve_symbol(
        name
    )

    if not symbol:
        return []

    signals = []

    # ========================================================
    # WEEKLY BIAS -> DAILY EXTERNAL BO
    # ========================================================

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

    # ========================================================
    # DAILY BIAS -> H4 EXTERNAL BO
    # ========================================================

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


# ============================================================
# FULL MARKET SCAN
# ============================================================

def scan_market():

    global last_scan_time

    print(
        "\n========================================"
    )

    print(
        "Starting SLK market scan"
    )

    print(
        "14 instruments"
    )

    print(
        "W1 -> D1 External BO"
    )

    print(
        "D1 -> H4 External BO"
    )

    print(
        "========================================"
    )

    for name in INSTRUMENTS:

        print(
            f"Checking {name}..."
        )

        try:

            signals = scan_instrument(
                name
            )

            for signal in signals:

                sid = get_signal_id(
                    signal
                )

                with state_lock:

                    if sid in sent_signals:

                        continue

                message = make_alert(
                    signal
                )

                print(
                    "\nNEW SLK SIGNAL\n"
                    + message
                )

                if send_telegram(
                    message
                ):

                    with state_lock:

                        sent_signals[
                            sid
                        ] = time.time()

        except Exception as error:

            print(
                f"Instrument error "
                f"{name}: {error}"
            )

    last_scan_time = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    print(
        "\nScan completed:"
        f" {last_scan_time}"
    )


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    time.sleep(5)

    while True:

        try:

            scan_market()

        except Exception as error:

            print(
                f"Scanner loop error: "
                f"{error}"
            )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START BOT
# ============================================================

def main():

    if not TWELVE_DATA_API_KEY:

        raise RuntimeError(
            "TWELVE_DATA_API_KEY "
            "is missing."
        )

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN "
            "is missing."
        )

    if not TELEGRAM_CHAT_ID:

        raise RuntimeError(
            "TELEGRAM_CHAT_ID "
            "is missing."
        )

    print(
        "========================================"
    )

    print(
        "SLK BIAS TRADING BOT"
    )

    print(
        "========================================"
    )

    print(
        "14 instruments loaded."
    )

    print(
        "Weekly bias -> Daily BO"
    )

    print(
        "Daily bias -> H4 BO"
    )

    print(
        "Bias only."
    )

    print(
        "========================================"
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
