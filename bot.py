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

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))
PORT = int(os.getenv("PORT", "10000"))

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

# Stores setups that have already produced an alert.
# This prevents the same Daily setup from sending an alert
# repeatedly every 15 minutes.
ALERTED_SETUPS = {}


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        response = {
            "status": "running",
            "service": "SLK Bias Trading Bot"
        }

        body = json.dumps(response).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Health server running on port {PORT}")
    server.serve_forever()


# ============================================================
# HTTP HELPER
# ============================================================

def http_get(url, params=None):

    if params:
        url = url + "?" + urlencode(params)

    request = Request(
        url,
        headers={
            "User-Agent": "SLK-Bias-Trading-Bot/1.0"
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

        except Exception:
            continue

    # Twelve Data normally returns newest first.
    # We need oldest -> newest for structural analysis.
    candles.sort(key=lambda x: x["datetime"])

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

    if isinstance(data, dict):
        raw = data.get("data", [])

        for item in raw:

            if isinstance(item, dict):

                symbol = item.get("symbol")

                if symbol:
                    pairs.append(symbol)

            elif isinstance(item, str):
                pairs.append(item)

    return pairs


def normalize_forex_symbol(symbol):

    if not symbol:
        return symbol

    return symbol.replace("/", "").upper()


def build_instrument_list():

    instruments = []

    forex_pairs = get_forex_pairs()

    for pair in forex_pairs:

        normalized = normalize_forex_symbol(pair)

        if normalized:
            instruments.append({
                "name": normalized,
                "symbols": [pair]
            })

    # Add special markets.
    for name, aliases in SPECIAL_MARKETS.items():

        instruments.append({
            "name": name,
            "symbols": aliases
        })

    # Remove duplicates.
    seen = set()
    result = []

    for item in instruments:

        if item["name"] not in seen:

            seen.add(item["name"])
            result.append(item)

    return result


# ============================================================
# SYMBOL RESOLUTION
# ============================================================

def resolve_symbol(symbols):

    for symbol in symbols:

        candles = get_time_series(
            symbol,
            "1day",
            5
        )

        if candles:
            return symbol

    return None


# ============================================================
# LINE CHART VALUES
# ============================================================

def closes(candles):

    return [c["close"] for c in candles]


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


def get_swing_highs(candles, end_index=None):

    values = closes(candles)

    if end_index is None:
        end_index = len(values) - 1

    result = []

    for i in range(2, min(end_index, len(values) - 3) + 1):

        if is_swing_high(values, i):
            result.append((i, values[i]))

    return result


def get_swing_lows(candles, end_index=None):

    values = closes(candles)

    if end_index is None:
        end_index = len(values) - 1

    result = []

    for i in range(2, min(end_index, len(values) - 3) + 1):

        if is_swing_low(values, i):
            result.append((i, values[i]))

    return result


# ============================================================
# DAILY BOS
# ============================================================

def detect_daily_bos_before_index(candles, end_index):

    if end_index < 4:
        return None

    values = closes(candles)

    highs = get_swing_highs(
        candles[:end_index]
    )

    lows = get_swing_lows(
        candles[:end_index]
    )

    latest_bullish = None
    latest_bearish = None

    # Bullish BOS:
    # closing price breaks a previous swing high.
    for i in range(1, end_index):

        previous_highs = [
            (idx, level)
            for idx, level in highs
            if idx < i
        ]

        if previous_highs:

            swing_index, level = previous_highs[-1]

            if values[i] > level:

                latest_bullish = {
                    "index": i,
                    "datetime": candles[i]["datetime"],
                    "level": level,
                    "type": "Bullish BOS"
                }

    # Bearish BOS:
    # closing price breaks a previous swing low.
    for i in range(1, end_index):

        previous_lows = [
            (idx, level)
            for idx, level in lows
            if idx < i
        ]

        if previous_lows:

            swing_index, level = previous_lows[-1]

            if values[i] < level:

                latest_bearish = {
                    "index": i,
                    "datetime": candles[i]["datetime"],
                    "level": level,
                    "type": "Bearish BOS"
                }

    candidates = []

    if latest_bullish:
        candidates.append(latest_bullish)

    if latest_bearish:
        candidates.append(latest_bearish)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["index"])

    return candidates[-1]


# ============================================================
# A-SHAPE
# ============================================================

def detect_a_shape(candles):

    if len(candles) < 5:
        return None

    values = closes(candles)

    highs = get_swing_highs(candles)

    if not highs:
        return None

    idx, level = highs[-1]

    if idx >= len(values) - 1:
        return None

    return {
        "pattern": "A-shape",
        "level": level,
        "index": idx,
        "direction": "SELL"
    }


# ============================================================
# V-SHAPE
# ============================================================

def detect_v_shape(candles):

    if len(candles) < 5:
        return None

    values = closes(candles)

    lows = get_swing_lows(candles)

    if not lows:
        return None

    idx, level = lows[-1]

    if idx >= len(values) - 1:
        return None

    return {
        "pattern": "V-shape",
        "level": level,
        "index": idx,
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

    # RBS:
    # Previous resistance -> close above it -> return and hold above it.
    for idx, level in reversed(highs):

        for break_index in range(idx + 1, len(values)):

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

    # SBR:
    # Previous support -> close below it -> return and hold below it.
    for idx, level in reversed(lows):

        for break_index in range(idx + 1, len(values)):

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

    for i in range(len(candles) - 2, 0, -1):

        current = candles[i]
        previous = candles[i - 1]

        current_body = current["close"] - current["open"]
        previous_body = previous["close"] - previous["open"]

        # Two consecutive bullish candles.
        if current_body > 0 and previous_body > 0:

            level = previous["open"]

            return {
                "pattern": "OCL",
                "level": level,
                "index": i,
                "direction": "BUY"
            }

        # Two consecutive bearish candles.
        if current_body < 0 and previous_body < 0:

            level = previous["open"]

            return {
                "pattern": "OCL",
                "level": level,
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
    # Bearish QMR
    #
    # Left shoulder
    # Head higher
    # Neckline
    # Right shoulder lower than head
    # --------------------------------------------------------

    if len(highs) >= 2 and len(lows) >= 1:

        for a in range(len(highs) - 1):

            left_idx, left_high = highs[a]
            head_idx, head_high = highs[a + 1]

            if head_idx <= left_idx:
                continue

            if head_high <= left_high:
                continue

            middle_lows = [
                (idx, level)
                for idx, level in lows
                if left_idx < idx < head_idx
            ]

            if not middle_lows:
                continue

            neckline_idx, neckline = middle_lows[-1]

            for right_idx, right_high in highs:

                if right_idx <= head_idx:
                    continue

                if right_high >= head_high:
                    continue

                # Neckline must subsequently break downward.
                for break_index in range(
                    right_idx + 1,
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
    # Bullish QMR
    #
    # Left shoulder
    # Head lower
    # Neckline
    # Right shoulder higher than head
    # --------------------------------------------------------

    if len(lows) >= 2 and len(highs) >= 1:

        for a in range(len(lows) - 1):

            left_idx, left_low = lows[a]
            head_idx, head_low = lows[a + 1]

            if head_idx <= left_idx:
                continue

            if head_low >= left_low:
                continue

            middle_highs = [
                (idx, level)
                for idx, level in highs
                if left_idx < idx < head_idx
            ]

            if not middle_highs:
                continue

            neckline_idx, neckline = middle_highs[-1]

            for right_idx, right_low in lows:

                if right_idx <= head_idx:
                    continue

                if right_low <= head_low:
                    continue

                # Neckline must subsequently break upward.
                for break_index in range(
                    right_idx + 1,
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

    # Most recent valid structure wins.
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

    candidates.sort(
        key=lambda x: x["index"]
    )

    return candidates[-1]


# ============================================================
# DAILY REJECTION
# ============================================================

def confirm_daily_rejection(candle, key_level):

    level = key_level["level"]

    high = candle["high"]
    low = candle["low"]
    close = candle["close"]

    touched = (
        low <= level <= high
    )

    if not touched:
        return None

    # Bullish rejection.
    if close > level:

        return {
            "bias": "BUY",
            "rejection": "Bullish rejection",
            "close": close
        }

    # Bearish rejection.
    if close < level:

        return {
            "bias": "SELL",
            "rejection": "Bearish rejection",
            "close": close
        }

    return None


# ============================================================
# FIND VALID DAILY SETUP
#
# IMPORTANT:
#
# The rejection candle does NOT need to have H4 confirmation
# immediately.
#
# Once the rejection happens, the setup becomes ACTIVE.
# H4 can confirm on:
#
#   - the rejection day
#   - the next Daily candle
#   - a later Daily candle
#
# until the setup becomes invalid.
# ============================================================

def find_daily_setup(candles):

    if len(candles) < 20:
        return None

    # Ignore the currently forming Daily candle.
    completed = candles[:-1]

    # Start from the newest completed candle and work backwards.
    for rejection_index in range(
        len(completed) - 1,
        5,
        -1
    ):

        rejection_candle = completed[rejection_index]

        history = completed[:rejection_index]

        key_level = find_key_level(history)

        if not key_level:
            continue

        # Key level must exist before the rejection.
        if key_level["index"] >= rejection_index:
            continue

        daily_rejection = confirm_daily_rejection(
            rejection_candle,
            key_level
        )

        if not daily_rejection:
            continue

        # Find the Daily BOS that existed BEFORE
        # the key-level rejection sequence.
        prior_bos = detect_daily_bos_before_index(
            completed,
            key_level["index"]
        )

        if not prior_bos:
            continue

        # BOS must agree with rejection.
        if (
            daily_rejection["bias"] == "BUY"
            and prior_bos["type"] != "Bullish BOS"
        ):
            continue

        if (
            daily_rejection["bias"] == "SELL"
            and prior_bos["type"] != "Bearish BOS"
        ):
            continue

        return {
            "rejection_index": rejection_index,
            "rejection_datetime": rejection_candle["datetime"],
            "pattern": key_level["pattern"],
            "key_level": key_level["level"],
            "daily_bias": daily_rejection["bias"],
            "rejection": daily_rejection["rejection"],
            "close": daily_rejection["close"],
            "prior_bos": prior_bos
        }

    return None


# ============================================================
# H4 STRUCTURE
# ============================================================

def detect_h4_sweep_choch(candles, direction):

    if len(candles) < 20:
        return None

    values = closes(candles)

    highs = get_swing_highs(candles)
    lows = get_swing_lows(candles)

    # ========================================================
    # BUY
    #
    # Required:
    #
    # Downside BOS / sweep
    #       ↓
    # Upside BOS / CHOCH
    #
    # ========================================================

    if direction == "BUY":

        for low_pos in range(len(lows) - 1, -1, -1):

            sweep_index, sweep_level = lows[low_pos]

            # We need a previous structural low that gets broken.
            previous_lows = [
                item
                for item in lows[:low_pos]
            ]

            if not previous_lows:
                continue

            previous_low_index, previous_low = previous_lows[-1]

            # Find the actual downside closing break AFTER
            # the previous swing low.
            sweep_break_index = None

            for i in range(
                previous_low_index + 1,
                len(values)
            ):

                if i >= sweep_index:
                    break

                if values[i] < previous_low:

                    sweep_break_index = i
                    break

            if sweep_break_index is None:
                continue

            # Now find a swing high formed after the sweep.
            highs_after_sweep = [
                item
                for item in highs
                if item[0] > sweep_break_index
            ]

            if not highs_after_sweep:
                continue

            for choch_index, choch_level in highs_after_sweep:

                # Price must CLOSE above the counter-swing high.
                for i in range(
                    choch_index + 1,
                    len(values)
                ):

                    if values[i] > choch_level:

                        return {
                            "confirmation": "BUY Sweep + CHOCH",
                            "sweep_index": sweep_break_index,
                            "sweep_level": previous_low,
                            "choch_index": i,
                            "choch_level": choch_level,
                            "datetime": candles[i]["datetime"]
                        }

        return None

    # ========================================================
    # SELL
    #
    # Required:
    #
    # Upside BOS / sweep
    #       ↓
    # Downside BOS / CHOCH
    #
    # ========================================================

    if direction == "SELL":

        for high_pos in range(len(highs) - 1, -1, -1):

            sweep_index, sweep_level = highs[high_pos]

            previous_highs = [
                item
                for item in highs[:high_pos]
            ]

            if not previous_highs:
                continue

            previous_high_index, previous_high = previous_highs[-1]

            # Find actual upside closing break AFTER
            # the previous swing high.
            sweep_break_index = None

            for i in range(
                previous_high_index + 1,
                len(values)
            ):

                if i >= sweep_index:
                    break

                if values[i] > previous_high:

                    sweep_break_index = i
                    break

            if sweep_break_index is None:
                continue

            # Find a swing low after the upside sweep.
            lows_after_sweep = [
                item
                for item in lows
                if item[0] > sweep_break_index
            ]

            if not lows_after_sweep:
                continue

            for choch_index, choch_level in lows_after_sweep:

                # Price must CLOSE below counter-swing low.
                for i in range(
                    choch_index + 1,
                    len(values)
                ):

                    if values[i] < choch_level:

                        return {
                            "confirmation": "SELL Sweep + CHOCH",
                            "sweep_index": sweep_break_index,
                            "sweep_level": previous_high,
                            "choch_index": i,
                            "choch_level": choch_level,
                            "datetime": candles[i]["datetime"]
                        }

        return None

    return None


# ============================================================
# FIND H4 CONFIRMATION AFTER DAILY REJECTION
#
# This is the major update.
#
# We don't require H4 confirmation to occur on the same
# Daily candle as the rejection.
#
# We search H4 data AFTER the Daily rejection candle closes.
# ============================================================

def find_h4_confirmation_after_rejection(
    h4_candles,
    daily_setup
):

    if not h4_candles:
        return None

    rejection_datetime = daily_setup[
        "rejection_datetime"
    ]

    direction = daily_setup[
        "daily_bias"
    ]

    # Find the H4 candles that belong to or occur after
    # the completed Daily rejection candle.
    #
    # The rejection candle's date/time is used as the
    # activation point.
    eligible = []

    for candle in h4_candles:

        if candle["datetime"] >= rejection_datetime:
            eligible.append(candle)

    if len(eligible) < 12:
        return None

    confirmation = detect_h4_sweep_choch(
        eligible,
        direction
    )

    return confirmation


# ============================================================
# SETUP INVALIDATION
# ============================================================

def setup_is_invalidated(
    daily_candles,
    setup
):

    if not setup:
        return True

    level = setup["key_level"]
    bias = setup["daily_bias"]

    # Only completed Daily candles.
    completed = daily_candles[:-1]

    rejection_index = setup["rejection_index"]

    if rejection_index >= len(completed):
        return True

    later_candles = completed[
        rejection_index + 1:
    ]

    # We allow later candles to produce confirmation.
    #
    # But if price decisively closes through the key level
    # in the opposite direction, invalidate the setup.
    for candle in later_candles:

        close = candle["close"]

        if bias == "BUY" and close < level:
            return True

        if bias == "SELL" and close > level:
            return True

    return False


# ============================================================
# SETUP ID
# ============================================================

def make_setup_id(symbol, setup):

    return (
        f"{symbol}|"
        f"{setup['rejection_datetime']}|"
        f"{setup['pattern']}|"
        f"{setup['daily_bias']}"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(message):

    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot token missing.")
        return False

    if not TELEGRAM_CHAT_ID:
        print("Telegram chat ID missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:

        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json"
            },
            method="POST"
        )

        with urlopen(request, timeout=30) as response:

            result = json.loads(
                response.read().decode()
            )

            if result.get("ok"):
                print("Telegram alert sent.")
                return True

            print(
                f"Telegram error: {result}"
            )
            return False

    except Exception as e:

        print(
            f"Telegram send error: {e}"
        )

        return False


# ============================================================
# ALERT FORMAT
# ============================================================

def build_alert(
    symbol,
    setup,
    confirmation
):

    prior_bos = setup["prior_bos"]

    message = f"""
📊 SLK BIAS ALERT

Symbol: {symbol}
Pattern: {setup["pattern"]}
Key Level: {setup["key_level"]}
Rejection: {setup["rejection"]}
Close: {setup["close"]}

Prior Daily BOS: {prior_bos["type"]}
Daily Bias: {setup["daily_bias"]}

H4 Confirmation: {confirmation["confirmation"]}

Daily Rejection Candle: {setup["rejection_datetime"]}
H4 Confirmation Candle: {confirmation["datetime"]}

Reason:
Daily {setup["daily_bias"]} rejection at {setup["pattern"]}
after prior {prior_bos["type"]}, followed by valid
H4 Sweep + CHOCH confirmation.
""".strip()

    return message


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(display_symbol, api_symbol):

    print(
        f"Scanning {display_symbol} "
        f"using {api_symbol}"
    )

    daily = get_time_series(
        api_symbol,
        "1day",
        200
    )

    h4 = get_time_series(
        api_symbol,
        "4h",
        300
    )

    if len(daily) < 20:
        print(
            f"{display_symbol}: "
            f"not enough Daily data."
        )
        return

    if len(h4) < 20:
        print(
            f"{display_symbol}: "
            f"not enough H4 data."
        )
        return

    setup = find_daily_setup(daily)

    if not setup:
        print(
            f"{display_symbol}: "
            f"No valid Daily setup."
        )
        return

    print(
        f"{display_symbol}: "
        f"Daily {setup['daily_bias']} setup found."
    )

    print(
        f"Pattern: {setup['pattern']} | "
        f"Level: {setup['key_level']} | "
        f"Rejection: {setup['rejection_datetime']}"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # If Daily rejection happened previously, we continue
    # checking H4 confirmation.
    # --------------------------------------------------------

    if setup_is_invalidated(
        daily,
        setup
    ):

        print(
            f"{display_symbol}: "
            f"Daily setup invalidated."
        )
        return

    confirmation = find_h4_confirmation_after_rejection(
        h4,
        setup
    )

    if not confirmation:

        print(
            f"{display_symbol}: "
            f"Waiting for H4 confirmation."
        )
        return

    print(
        f"{display_symbol}: "
        f"H4 confirmation found: "
        f"{confirmation['confirmation']}"
    )

    setup_id = make_setup_id(
        display_symbol,
        setup
    )

    # Prevent repeated alerts.
    if ALERTED_SETUPS.get(setup_id):

        print(
            f"{display_symbol}: "
            f"Already alerted for this setup."
        )
        return

    message = build_alert(
        display_symbol,
        setup,
        confirmation
    )

    sent = send_telegram_message(
        message
    )

    if sent:

        ALERTED_SETUPS[setup_id] = True

        print(
            f"{display_symbol}: "
            f"ALERT COMPLETE."
        )


# ============================================================
# SCAN ALL MARKETS
# ============================================================

def scan_all():

    print("=" * 60)

    print(
        "SLK scanner started:"
        f" {datetime.now(timezone.utc).isoformat()}"
    )

    instruments = build_instrument_list()

    print(
        f"Found {len(instruments)} instruments."
    )

    for instrument in instruments:

        display_symbol = instrument["name"]

        api_symbol = resolve_symbol(
            instrument["symbols"]
        )

        if not api_symbol:

            print(
                f"{display_symbol}: "
                f"No Twelve Data symbol found."
            )

            continue

        try:

            scan_symbol(
                display_symbol,
                api_symbol
            )

        except Exception as e:

            print(
                f"{display_symbol}: "
                f"scan error: {e}"
            )

    print("=" * 60)


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    print(
        "SLK scanner thread started."
    )

    while True:

        try:

            scan_all()

        except Exception as e:

            print(
                f"Scanner loop error: {e}"
            )

        print(
            f"Next scan in {SCAN_INTERVAL} seconds."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if not TWELVE_DATA_API_KEY:
        print(
            "WARNING: "
            "TWELVE_DATA_API_KEY is missing."
        )

    if not TELEGRAM_BOT_TOKEN:
        print(
            "WARNING: "
            "TELEGRAM_BOT_TOKEN is missing."
        )

    if not TELEGRAM_CHAT_ID:
        print(
            "WARNING: "
            "TELEGRAM_CHAT_ID is missing."
        )

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
