import os
import time
import threading
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# CONFIG
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Lightweight scheduler.
# It does NOT mean the whole market is rescanned every 5 minutes.
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

PORT = int(os.getenv("PORT", "10000"))

# Used only as the clock for detecting a new completed Daily candle.
DAILY_CLOCK_SYMBOL = os.getenv("DAILY_CLOCK_SYMBOL", "EUR/USD")

TWELVE_DATA_URL = "https://api.twelvedata.com"


# ============================================================
# SPECIAL MARKETS
# ============================================================

SPECIAL_MARKETS = {
    "XAUUSD": ["XAU/USD", "XAUUSD"],
    "JP225": ["JP225", "NIKKEI", "NI225"],
    "UK100": ["UK100", "FTSE"],
    "GERMAN": ["DE40", "DE30", "DAX"],
    "NAS100": ["NAS100", "NDX"],
}


# ============================================================
# STATE
# ============================================================

# Active Daily setups waiting for H4 confirmation.
ACTIVE_SETUPS = {}

# Prevents the same setup from alerting twice.
ALERTED_SETUPS = set()

# Resolved Twelve Data symbols.
RESOLVED_SYMBOLS = {}

# Last completed Daily candle used as the market clock.
LAST_DAILY_CLOCK = None

INSTRUMENTS = []


# ============================================================
# DATE/TIME
# ============================================================

def parse_dt(value):

    if not value:
        return None

    text = str(value).strip()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d"
    ]

    for fmt in formats:

        try:
            dt = datetime.strptime(text, fmt)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt

        except ValueError:
            continue

    try:

        dt = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except ValueError:
        return None


# ============================================================
# HTTP HELPER
# ============================================================

def http_get(url, params=None):

    if params:
        url = url + "?" + urlencode(params)

    request = Request(
        url,
        headers={
            "User-Agent": "SLK-Bias-Trading-Bot/2.0"
        }
    )

    try:

        with urlopen(request, timeout=30) as response:

            raw = response.read().decode("utf-8")

            return json.loads(raw)

    except Exception as e:

        print(f"HTTP error: {e}")

        return None


# ============================================================
# TWELVE DATA
# ============================================================

def get_time_series(symbol, interval, outputsize=200):

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON"
    }

    data = http_get(
        f"{TWELVE_DATA_URL}/time_series",
        params
    )

    if not data:

        return []

    if "values" not in data:

        if "message" in data:
            print(
                f"{symbol} {interval}: "
                f"{data.get('message')}"
            )

        return []

    candles = []

    for item in data["values"]:

        try:

            candles.append({
                "datetime": item["datetime"],
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"])
            })

        except (
            KeyError,
            TypeError,
            ValueError
        ):

            continue

    candles.sort(
        key=lambda x: (
            parse_dt(x["datetime"])
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        )
    )

    return candles


# ============================================================
# FOREX PAIRS
# ============================================================

def get_forex_pairs():

    data = http_get(
        f"{TWELVE_DATA_URL}/forex_pairs",
        {
            "apikey": TWELVE_DATA_API_KEY
        }
    )

    pairs = []

    if not data:
        return pairs

    raw = data.get("data", [])

    for item in raw:

        if isinstance(item, dict):

            symbol = item.get("symbol")

        else:

            symbol = item

        if symbol:
            pairs.append(symbol)

    return pairs


def normalize_symbol(symbol):

    if not symbol:
        return ""

    return (
        symbol
        .replace("/", "")
        .replace(":", "")
        .upper()
    )


# ============================================================
# FIXED INSTRUMENT LIST
# ============================================================

def build_instrument_list():

    instruments = [
        {
            "name": "EURUSD",
            "symbols": ["EUR/USD", "EURUSD"]
        },
        {
            "name": "GBPUSD",
            "symbols": ["GBP/USD", "GBPUSD"]
        },
        {
            "name": "USDJPY",
            "symbols": ["USD/JPY", "USDJPY"]
        },
        {
            "name": "USDCHF",
            "symbols": ["USD/CHF", "USDCHF"]
        },
        {
            "name": "AUDUSD",
            "symbols": ["AUD/USD", "AUDUSD"]
        },
        {
            "name": "USDCAD",
            "symbols": ["USD/CAD", "USDCAD"]
        },
        {
            "name": "EURGBP",
            "symbols": ["EUR/GBP", "EURGBP"]
        },
        {
            "name": "EURJPY",
            "symbols": ["EUR/JPY", "EURJPY"]
        },
        {
            "name": "GBPJPY",
            "symbols": ["GBP/JPY", "GBPJPY"]
        },
        {
            "name": "AUDJPY",
            "symbols": ["AUD/JPY", "AUDJPY"]
        },
        {
            "name": "JP225",
            "symbols": ["JP225", "NIKKEI", "NI225"]
        },
        {
            "name": "UK100",
            "symbols": ["UK100", "FTSE"]
        },
        {
            "name": "NAS100",
            "symbols": ["NAS100", "NDX"]
        },
        {
            "name": "XAUUSD",
            "symbols": ["XAU/USD", "XAUUSD"]
        }
    ]

    return instruments


# ============================================================
# SYMBOL RESOLUTION
# ============================================================

def resolve_symbol(item):

    name = item["name"]

    if name in RESOLVED_SYMBOLS:

        return RESOLVED_SYMBOLS[name]

    for symbol in item["symbols"]:

        candles = get_time_series(
            symbol,
            "1day",
            3
        )

        if candles:

            RESOLVED_SYMBOLS[name] = symbol

            return symbol

    RESOLVED_SYMBOLS[name] = None

    return None


# ============================================================
# BASIC CANDLE HELPERS
# ============================================================

def closes(candles):

    return [
        candle["close"]
        for candle in candles
    ]


def completed_daily(candles):

    if len(candles) < 2:
        return []

    # Last candle is treated as currently forming.
    return candles[:-1]


# ============================================================
# SWING DETECTION
# ============================================================

def is_swing_high(values, index, strength=2):

    if index < strength:
        return False

    if index + strength >= len(values):
        return False

    current = values[index]

    for i in range(1, strength + 1):

        if current <= values[index - i]:
            return False

        if current <= values[index + i]:
            return False

    return True


def is_swing_low(values, index, strength=2):

    if index < strength:
        return False

    if index + strength >= len(values):
        return False

    current = values[index]

    for i in range(1, strength + 1):

        if current >= values[index - i]:
            return False

        if current >= values[index + i]:
            return False

    return True


def get_swing_highs(
    candles,
    end_index=None
):

    values = closes(candles)

    if end_index is None:

        end_index = len(values) - 1

    end_index = min(
        end_index,
        len(values) - 1
    )

    result = []

    for i in range(
        2,
        end_index + 1
    ):

        if is_swing_high(
            values,
            i
        ):

            result.append(
                (i, values[i])
            )

    return result


def get_swing_lows(
    candles,
    end_index=None
):

    values = closes(candles)

    if end_index is None:

        end_index = len(values) - 1

    end_index = min(
        end_index,
        len(values) - 1
    )

    result = []

    for i in range(
        2,
        end_index + 1
    ):

        if is_swing_low(
            values,
            i
        ):

            result.append(
                (i, values[i])
            )

    return result


# ============================================================
# DAILY BOS
# ============================================================

def detect_daily_bos_before_index(
    candles,
    end_index
):

    if end_index < 4:
        return None

    values = closes(candles)

    highs = get_swing_highs(
        candles[:end_index]
    )

    lows = get_swing_lows(
        candles[:end_index]
    )

    candidates = []

    for i in range(
        1,
        end_index
    ):

        previous_highs = [
            (idx, level)
            for idx, level in highs
            if idx < i
        ]

        if previous_highs:

            _, level = previous_highs[-1]

            if values[i] > level:

                candidates.append({
                    "index": i,
                    "datetime": candles[i]["datetime"],
                    "level": level,
                    "type": "Bullish BOS"
                })

        previous_lows = [
            (idx, level)
            for idx, level in lows
            if idx < i
        ]

        if previous_lows:

            _, level = previous_lows[-1]

            if values[i] < level:

                candidates.append({
                    "index": i,
                    "datetime": candles[i]["datetime"],
                    "level": level,
                    "type": "Bearish BOS"
                })

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda x: x["index"]
    )


# ============================================================
# A-SHAPE
# ============================================================

def detect_a_shape(candles):

    if len(candles) < 5:
        return None

    highs = get_swing_highs(candles)

    if not highs:
        return None

    index, level = highs[-1]

    if index >= len(candles) - 1:
        return None

    return {
        "pattern": "A-shape",
        "level": level,
        "index": index,
        "direction": "SELL"
    }


# ============================================================
# V-SHAPE
# ============================================================

def detect_v_shape(candles):

    if len(candles) < 5:
        return None

    lows = get_swing_lows(candles)

    if not lows:
        return None

    index, level = lows[-1]

    if index >= len(candles) - 1:
        return None

    return {
        "pattern": "V-shape",
        "level": level,
        "index": index,
        "direction": "BUY"
    }


# ============================================================
# RBS / SBR
# ============================================================

def detect_rbs_sbr(candles):

    if len(candles) < 8:
        return None

    values = closes(candles)

    highs = get_swing_highs(candles)

    lows = get_swing_lows(candles)

    # RBS
    for index, level in reversed(highs):

        for break_index in range(
            index + 1,
            len(values)
        ):

            if values[break_index] > level:

                for retest_index in range(
                    break_index + 1,
                    len(values)
                ):

                    if values[retest_index] >= level:

                        return {
                            "pattern": "RBS",
                            "level": level,
                            "index": retest_index,
                            "direction": "BUY"
                        }

                    if values[retest_index] < level:

                        break

    # SBR
    for index, level in reversed(lows):

        for break_index in range(
            index + 1,
            len(values)
        ):

            if values[break_index] < level:

                for retest_index in range(
                    break_index + 1,
                    len(values)
                ):

                    if values[retest_index] <= level:

                        return {
                            "pattern": "SBR",
                            "level": level,
                            "index": retest_index,
                            "direction": "SELL"
                        }

                    if values[retest_index] > level:

                        break

    return None


# ============================================================
# OCL
# ============================================================

def detect_ocl(candles):

    if len(candles) < 4:
        return None

    for i in range(
        len(candles) - 2,
        0,
        -1
    ):

        previous = candles[i - 1]

        current = candles[i]

        previous_body = (
            previous["close"]
            - previous["open"]
        )

        current_body = (
            current["close"]
            - current["open"]
        )

        # Bullish OCL
        if (
            previous_body > 0
            and current_body > 0
        ):

            return {
                "pattern": "OCL",
                "level": previous["open"],
                "index": i,
                "direction": "BUY"
            }

        # Bearish OCL
        if (
            previous_body < 0
            and current_body < 0
        ):

            return {
                "pattern": "OCL",
                "level": previous["open"],
                "index": i,
                "direction": "SELL"
            }

    return None


# ============================================================
# QMR
# ============================================================

def detect_qmr(candles):

    if len(candles) < 9:
        return None

    values = closes(candles)

    highs = get_swing_highs(candles)

    lows = get_swing_lows(candles)

    # --------------------------------------------------------
    # BEARISH QMR
    # --------------------------------------------------------

    if (
        len(highs) >= 2
        and len(lows) >= 1
    ):

        for a in range(
            len(highs) - 1
        ):

            left_index, left_high = highs[a]

            head_index, head_high = highs[a + 1]

            if head_index <= left_index:
                continue

            if head_high <= left_high:
                continue

            middle_lows = [
                (index, level)
                for index, level in lows
                if (
                    left_index
                    < index
                    < head_index
                )
            ]

            if not middle_lows:
                continue

            neckline = middle_lows[-1][1]

            for right_index, right_high in highs:

                if right_index <= head_index:
                    continue

                if right_high >= head_high:
                    continue

                for break_index in range(
                    right_index + 1,
                    len(values)
                ):

                    if values[break_index] < neckline:

                        return {
                            "pattern": "QMR",
                            "level": neckline,
                            "index": break_index,
                            "direction": "SELL"
                        }

    # --------------------------------------------------------
    # BULLISH QMR
    # --------------------------------------------------------

    if (
        len(lows) >= 2
        and len(highs) >= 1
    ):

        for a in range(
            len(lows) - 1
        ):

            left_index, left_low = lows[a]

            head_index, head_low = lows[a + 1]

            if head_index <= left_index:
                continue

            if head_low >= left_low:
                continue

            middle_highs = [
                (index, level)
                for index, level in highs
                if (
                    left_index
                    < index
                    < head_index
                )
            ]

            if not middle_highs:
                continue

            neckline = middle_highs[-1][1]

            for right_index, right_low in lows:

                if right_index <= head_index:
                    continue

                if right_low <= head_low:
                    continue

                for break_index in range(
                    right_index + 1,
                    len(values)
                ):

                    if values[break_index] > neckline:

                        return {
                            "pattern": "QMR",
                            "level": neckline,
                            "index": break_index,
                            "direction": "BUY"
                        }

    return None


# ============================================================
# FIND KEY LEVEL
# ============================================================

def find_key_level(candles):

    detectors = [
        detect_qmr,
        detect_rbs_sbr,
        detect_a_shape,
        detect_v_shape,
        detect_ocl
    ]

    candidates = []

    for detector in detectors:

        try:

            result = detector(candles)

            if result:
                candidates.append(result)

        except Exception as e:

            print(
                f"Key-level detector error "
                f"{detector.__name__}: {e}"
            )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda x: x["index"]
    )


# ============================================================
# DAILY REJECTION
# ============================================================

def confirm_daily_rejection(
    candle,
    key_level
):

    level = key_level["level"]

    touched = (
        candle["low"]
        <= level
        <= candle["high"]
    )

    if not touched:
        return None

    close = candle["close"]

    if close > level:

        return {
            "bias": "BUY",
            "rejection": "Bullish rejection",
            "close": close
        }

    if close < level:

        return {
            "bias": "SELL",
            "rejection": "Bearish rejection",
            "close": close
        }

    return None


# ============================================================
# DAILY SETUP
#
# DAILY BOS
#     ↓
# KEY LEVEL
#     ↓
# DAILY REJECTION
#     ↓
# ACTIVE SETUP
#     ↓
# WAIT FOR H4
#
# H4 can confirm on the rejection day or later.
# ============================================================

def find_daily_setup(candles):

    completed = completed_daily(candles)

    if len(completed) < 20:
        return None

    for rejection_index in range(
        len(completed) - 1,
        5,
        -1
    ):

        rejection_candle = completed[
            rejection_index
        ]

        history = completed[
            :rejection_index
        ]

        key_level = find_key_level(
            history
        )

        if not key_level:
            continue

        if (
            key_level["index"]
            >= rejection_index
        ):
            continue

        daily_rejection = confirm_daily_rejection(
            rejection_candle,
            key_level
        )

        if not daily_rejection:
            continue

        prior_bos = detect_daily_bos_before_index(
            completed,
            key_level["index"]
        )

        if not prior_bos:
            continue

        if (
            daily_rejection["bias"] == "BUY"
            and prior_bos["type"]
            != "Bullish BOS"
        ):
            continue

        if (
            daily_rejection["bias"] == "SELL"
            and prior_bos["type"]
            != "Bearish BOS"
        ):
            continue

        return {
            "rejection_index": rejection_index,
            "rejection_datetime":
                rejection_candle["datetime"],
            "key_level":
                key_level["level"],
            "pattern":
                key_level["pattern"],
            "daily_bias":
                daily_rejection["bias"],
            "rejection":
                daily_rejection["rejection"],
            "close":
                daily_rejection["close"],
            "prior_bos":
                prior_bos
        }

    return None


# ============================================================
# SETUP ID
# ============================================================

def setup_key(setup):

    return (
        f"{setup['rejection_datetime']}|"
        f"{setup['pattern']}|"
        f"{setup['key_level']}|"
        f"{setup['daily_bias']}"
    )


# ============================================================
# DAILY INVALIDATION
# ============================================================

def daily_invalidated(
    candles,
    setup
):

    completed = completed_daily(candles)

    start = (
        setup["rejection_index"]
        + 1
    )

    for candle in completed[start:]:

        close = candle["close"]

        if (
            setup["daily_bias"] == "BUY"
            and close < setup["key_level"]
        ):

            return True

        if (
            setup["daily_bias"] == "SELL"
            and close > setup["key_level"]
        ):

            return True

    return False


# ============================================================
# H4 SWEEP
#
# BUY:
#     downside liquidity attack first
#
# SELL:
#     upside liquidity attack first
# ============================================================

def find_h4_sweep(
    candles,
    bias,
    after_datetime
):

    if len(candles) < 5:
        return None

    completed = candles[:-1]

    after_dt = parse_dt(
        after_datetime
    )

    if not after_dt:
        return None

    values = closes(completed)

    if bias == "BUY":

        swings = get_swing_lows(
            completed
        )

        for i in range(
            0,
            len(completed)
        ):

            candle_dt = parse_dt(
                completed[i]["datetime"]
            )

            if (
                not candle_dt
                or candle_dt <= after_dt
            ):
                continue

            previous = [
                (index, level)
                for index, level in swings
                if index < i
            ]

            if not previous:
                continue

            _, level = previous[-1]

            # Downside sweep.
            if values[i] < level:

                return {
                    "index": i,
                    "datetime":
                        completed[i]["datetime"],
                    "level": level
                }

    else:

        swings = get_swing_highs(
            completed
        )

        for i in range(
            0,
            len(completed)
        ):

            candle_dt = parse_dt(
                completed[i]["datetime"]
            )

            if (
                not candle_dt
                or candle_dt <= after_dt
            ):
                continue

            previous = [
                (index, level)
                for index, level in swings
                if index < i
            ]

            if not previous:
                continue

            _, level = previous[-1]

            # Upside sweep.
            if values[i] > level:

                return {
                    "index": i,
                    "datetime":
                        completed[i]["datetime"],
                    "level": level
                }

    return None


# ============================================================
# H4 BREAKOUT
#
# IMPORTANT:
# Sweep must happen FIRST.
# Breakout must happen AFTER the sweep.
#
# No standalone H4 BOS.
# ============================================================

def find_h4_breakout_after_sweep(
    candles,
    bias,
    sweep_index
):

    if len(candles) < 5:
        return None

    completed = candles[:-1]

    if (
        sweep_index is None
        or sweep_index >= len(completed) - 1
    ):
        return None

    values = closes(completed)

    if bias == "BUY":

        highs = get_swing_highs(
            completed
        )

        for i in range(
            sweep_index + 1,
            len(completed)
        ):

            previous = [
                (index, level)
                for index, level in highs
                if (
                    sweep_index
                    < index
                    < i
                )
            ]

            if not previous:
                continue

            _, level = previous[-1]

            if values[i] > level:

                return {
                    "index": i,
                    "datetime":
                        completed[i]["datetime"],
                    "level": level
                }

    else:

        lows = get_swing_lows(
            completed
        )

        for i in range(
            sweep_index + 1,
            len(completed)
        ):

            previous = [
                (index, level)
                for index, level in lows
                if (
                    sweep_index
                    < index
                    < i
                )
            ]

            if not previous:
                continue

            _, level = previous[-1]

            if values[i] < level:

                return {
                    "index": i,
                    "datetime":
                        completed[i]["datetime"],
                    "level": level
                }

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        print(
            "Telegram credentials missing."
        )

        return False

    data = urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }).encode()

    request = Request(
        (
            "https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        ),
        data=data,
        method="POST",
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        }
    )

    try:

        with urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode()
            )

            return bool(
                result.get("ok")
            )

    except Exception as e:

        print(
            f"Telegram error: {e}"
        )

        return False


# ============================================================
# ALERT MESSAGE
# ============================================================

def make_alert(
    symbol,
    setup,
    sweep,
    breakout
):

    return (
        "📊 SLK BIAS ALERT\n\n"

        f"Symbol: {symbol}\n"

        f"Pattern: "
        f"{setup['pattern']}\n"

        f"Key Level: "
        f"{setup['key_level']}\n"

        f"Rejection: "
        f"{setup['rejection']}\n"

        f"Close: "
        f"{setup['close']}\n"

        f"Daily Bias: "
        f"{setup['daily_bias']}\n"

        f"H4 Confirmation: "
        f"{setup['daily_bias']} "
        f"Sweep + Breakout\n"

        f"Daily Rejection Candle: "
        f"{setup['rejection_datetime']}\n"

        f"H4 Sweep Candle: "
        f"{sweep['datetime']}\n"

        f"H4 Breakout Candle: "
        f"{breakout['datetime']}\n\n"

        "Reason: Daily directional BOS + "
        "key-level rejection + H4 liquidity "
        "sweep followed by H4 breakout."
    )


# ============================================================
# DAILY SCAN
#
# The whole market is scanned only when the
# reference Daily candle changes.
# ============================================================

def scan_daily_setups():

    global LAST_DAILY_CLOCK

    clock = get_time_series(
        DAILY_CLOCK_SYMBOL,
        "1day",
        3
    )

    completed = completed_daily(
        clock
    )

    if not completed:

        print(
            "Could not read Daily clock."
        )

        return

    latest_clock = completed[-1][
        "datetime"
    ]

    if (
        LAST_DAILY_CLOCK
        == latest_clock
    ):

        return

    LAST_DAILY_CLOCK = latest_clock

    print(
        "New completed Daily candle: "
        f"{latest_clock}"
    )

    for item in INSTRUMENTS:

        symbol = resolve_symbol(
            item
        )

        if not symbol:
            continue

        candles = get_time_series(
            symbol,
            "1day",
            200
        )

        if not candles:
            continue

        setup = find_daily_setup(
            candles
        )

        if not setup:
            continue

        key = setup_key(
            setup
        )

        if key in ALERTED_SETUPS:
            continue

        setup["symbol"] = symbol

        setup["name"] = item["name"]

        setup["h4_sweep"] = None

        setup["created_at"] = time.time()

        ACTIVE_SETUPS[
            item["name"]
        ] = setup

        print(
            "ACTIVE SETUP: "
            f"{item['name']} | "
            f"{setup['daily_bias']} | "
            f"{setup['pattern']}"
        )


# ============================================================
# H4 ACTIVE-SETUP MONITOR
# ============================================================

def monitor_active_setups():

    for name in list(
        ACTIVE_SETUPS.keys()
    ):

        setup = ACTIVE_SETUPS.get(
            name
        )

        if not setup:
            continue

        symbol = setup["symbol"]

        # ----------------------------------------
        # Check Daily invalidation.
        # ----------------------------------------

        daily = get_time_series(
            symbol,
            "1day",
            50
        )

        if daily:

            if daily_invalidated(
                daily,
                setup
            ):

                print(
                    "Daily setup invalidated: "
                    f"{name}"
                )

                ACTIVE_SETUPS.pop(
                    name,
                    None
                )

                continue

        # ----------------------------------------
        # Only active setups request H4 data.
        # ----------------------------------------

        h4 = get_time_series(
            symbol,
            "4h",
            120
        )

        if not h4:
            continue

        # ----------------------------------------
        # Find sweep first.
        # ----------------------------------------

        if setup.get(
            "h4_sweep"
        ) is None:

            sweep = find_h4_sweep(
                h4,
                setup["daily_bias"],
                setup["rejection_datetime"]
            )

            if sweep:

                setup["h4_sweep"] = sweep

                print(
                    "H4 SWEEP FOUND: "
                    f"{name} | "
                    f"{sweep['datetime']}"
                )

        # ----------------------------------------
        # Breakout must happen AFTER sweep.
        # ----------------------------------------

        if setup.get(
            "h4_sweep"
        ):

            breakout = (
                find_h4_breakout_after_sweep(
                    h4,
                    setup["daily_bias"],
                    setup["h4_sweep"]["index"]
                )
            )

            if not breakout:
                continue

            key = setup_key(
                setup
            )

            if key in ALERTED_SETUPS:
                continue

            message = make_alert(
                name,
                setup,
                setup["h4_sweep"],
                breakout
            )

            sent = send_telegram(
                message
            )

            if sent:

                ALERTED_SETUPS.add(
                    key
                )

                ACTIVE_SETUPS.pop(
                    name,
                    None
                )

                print(
                    "ALERT SENT: "
                    f"{name}"
                )


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    global INSTRUMENTS

    if not TWELVE_DATA_API_KEY:

        print(
            "TWELVE_DATA_API_KEY is missing."
        )

        return

    print(
        "Building instrument list..."
    )

    INSTRUMENTS = (
        build_instrument_list()
    )

    print(
        f"Loaded {len(INSTRUMENTS)} "
        "instruments."
    )

    first_run = True

    while True:

        try:

            # Daily scan happens only when
            # the completed Daily candle changes.
            scan_daily_setups()

            # H4 only runs when there are
            # active Daily setups.
            if ACTIVE_SETUPS:

                monitor_active_setups()

            else:

                print(
                    "No active setups. "
                    "Waiting for next Daily candle."
                )

            first_run = False

        except Exception as e:

            print(
                f"Scanner error: {e}"
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        response = {
            "status": "running",
            "service":
                "SLK Bias Trading Bot"
        }

        body = json.dumps(
            response
        ).encode()

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

        self.wfile.write(
            body
        )

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"Health server running on port "
        f"{PORT}"
    )

    server.serve_forever()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner_thread.start()

    print(
        "SLK Bias Trading Bot is fully running."
    )

    while True:

        time.sleep(60)
