import os
import time
import threading
import json
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# CONFIGURATION
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
    "NAS100": ["NAS100", "NDX"],
    "UK100": ["UK100", "FTSE"],
    "GERMAN": ["DE40", "DE30", "DAX"]
}


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ["/", "/health"]:

            response = {
                "status": "running",
                "service": "SLK Bias Trading Bot"
            }

            body = json.dumps(response).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Health server running on port {PORT}")
    server.serve_forever()


# ============================================================
# BASIC CANDLE FUNCTIONS
# ============================================================

def bullish(candle):
    return candle["close"] > candle["open"]


def bearish(candle):
    return candle["close"] < candle["open"]


# ============================================================
# TWELVE DATA
# ============================================================

def twelve_data_request(endpoint, params):

    params["apikey"] = TWELVE_DATA_API_KEY

    url = f"{TWELVE_DATA_URL}{endpoint}?{urlencode(params)}"

    try:

        request = Request(
            url,
            headers={
                "User-Agent": "SLK-Bias-Bot/1.0"
            }
        )

        with urlopen(request, timeout=30) as response:

            data = json.loads(
                response.read().decode("utf-8")
            )

            return data

    except Exception as e:

        print("Twelve Data error:", e)

        return None


# ============================================================
# GET CANDLES
# ============================================================

def get_candles(symbol, interval, outputsize=120):

    data = twelve_data_request(
        "/time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON"
        }
    )

    if not data:
        return []

    if "values" not in data:
        return []

    candles = []

    for item in data["values"]:

        try:

            candle = {
                "datetime": item.get("datetime"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"])
            }

            candles.append(candle)

        except Exception:
            continue

    # Twelve Data normally returns newest first.
    # We need oldest -> newest.
    candles.reverse()

    return candles


# ============================================================
# FOREX PAIR DISCOVERY
# ============================================================

def get_forex_pairs():

    data = twelve_data_request(
        "/forex_pairs",
        {}
    )

    pairs = []

    if data and "data" in data:

        for item in data["data"]:

            symbol = item.get("symbol")

            if symbol:
                pairs.append(symbol)

    return pairs


# ============================================================
# LINE-CHART DATA
# ============================================================

def line_values(candles):
    """
    A line chart uses closing prices.

    Therefore all structural BOS, sweep and CHOCH
    calculations are performed using candle CLOSES.
    """

    return [c["close"] for c in candles]


# ============================================================
# SWING DETECTION ON LINE CHART
# ============================================================

def find_swing_highs(values, left=2, right=2):

    swings = []

    if len(values) < left + right + 1:
        return swings

    for i in range(left, len(values) - right):

        current = values[i]

        left_values = values[i - left:i]
        right_values = values[i + 1:i + right + 1]

        if (
            current > max(left_values)
            and current >= max(right_values)
        ):
            swings.append({
                "index": i,
                "price": current
            })

    return swings


def find_swing_lows(values, left=2, right=2):

    swings = []

    if len(values) < left + right + 1:
        return swings

    for i in range(left, len(values) - right):

        current = values[i]

        left_values = values[i - left:i]
        right_values = values[i + 1:i + right + 1]

        if (
            current < min(left_values)
            and current <= min(right_values)
        ):
            swings.append({
                "index": i,
                "price": current
            })

    return swings


# ============================================================
# DAILY MARKET DIRECTION
# ============================================================

def detect_daily_bos_before_index(candles, end_index):

    """
    Finds the most recent confirmed line-chart BOS
    BEFORE the supplied index.

    BUY trend:
        close breaks above a previous swing high.

    SELL trend:
        close breaks below a previous swing low.

    This BOS MUST occur before the key-level rejection.
    """

    if end_index < 7:
        return None

    values = line_values(candles[:end_index])

    swing_highs = find_swing_highs(values)
    swing_lows = find_swing_lows(values)

    events = []

    # --------------------------------------------------------
    # Bullish BOS
    # --------------------------------------------------------

    for swing in swing_highs:

        swing_index = swing["index"]
        swing_price = swing["price"]

        for i in range(swing_index + 1, len(values)):

            if values[i] > swing_price:

                events.append({
                    "index": i,
                    "direction": "BUY",
                    "type": "Bullish BOS",
                    "level": swing_price
                })

                break

    # --------------------------------------------------------
    # Bearish BOS
    # --------------------------------------------------------

    for swing in swing_lows:

        swing_index = swing["index"]
        swing_price = swing["price"]

        for i in range(swing_index + 1, len(values)):

            if values[i] < swing_price:

                events.append({
                    "index": i,
                    "direction": "SELL",
                    "type": "Bearish BOS",
                    "level": swing_price
                })

                break

    if not events:
        return None

    events.sort(key=lambda x: x["index"])

    return events[-1]


# ============================================================
# KEY LEVEL: A-SHAPE RESISTANCE
# ============================================================

def detect_a_shape(candles):

    """
    A-shape on line chart:

        rising structure
              /\
             /  \
            /    \

    The central point is the resistance level.
    """

    values = line_values(candles)

    if len(values) < 7:
        return None

    swing_highs = find_swing_highs(values)

    for swing in reversed(swing_highs):

        i = swing["index"]

        if i < 2 or i >= len(values) - 2:
            continue

        left = values[i - 2:i]
        right = values[i + 1:i + 3]

        if (
            values[i] > max(left)
            and values[i] > max(right)
        ):

            return {
                "type": "A-shape",
                "direction": "SELL",
                "level": values[i],
                "index": i
            }

    return None


# ============================================================
# KEY LEVEL: V-SHAPE SUPPORT
# ============================================================

def detect_v_shape(candles):

    """
    V-shape on line chart:

            \      /
             \    /
              \  /
               \/

    The central point is the support level.
    """

    values = line_values(candles)

    if len(values) < 7:
        return None

    swing_lows = find_swing_lows(values)

    for swing in reversed(swing_lows):

        i = swing["index"]

        if i < 2 or i >= len(values) - 2:
            continue

        left = values[i - 2:i]
        right = values[i + 1:i + 3]

        if (
            values[i] < min(left)
            and values[i] < min(right)
        ):

            return {
                "type": "V-shape",
                "direction": "BUY",
                "level": values[i],
                "index": i
            }

    return None


# ============================================================
# RBS / SBR
# ============================================================

def detect_rbs_sbr(candles):

    """
    RBS:
        resistance
        -> close breaks ABOVE it
        -> price returns
        -> closes ABOVE it again

    SBR:
        support
        -> close breaks BELOW it
        -> price returns
        -> closes BELOW it again

    Structure is determined from LINE-CHART CLOSES.
    """

    values = line_values(candles)

    if len(values) < 10:
        return None

    swing_highs = find_swing_highs(values)
    swing_lows = find_swing_lows(values)

    events = []

    # --------------------------------------------------------
    # RBS
    # --------------------------------------------------------

    for swing in swing_highs:

        i = swing["index"]
        level = swing["price"]

        broken = False

        for j in range(i + 1, len(values)):

            if not broken:

                if values[j] > level:
                    broken = True

                continue

            # Retest of broken resistance
            if values[j] <= level:

                for k in range(j + 1, len(values)):

                    if values[k] > level:

                        events.append({
                            "type": "RBS",
                            "direction": "BUY",
                            "level": level,
                            "index": k
                        })

                        break

                break

    # --------------------------------------------------------
    # SBR
    # --------------------------------------------------------

    for swing in swing_lows:

        i = swing["index"]
        level = swing["price"]

        broken = False

        for j in range(i + 1, len(values)):

            if not broken:

                if values[j] < level:
                    broken = True

                continue

            # Retest of broken support
            if values[j] >= level:

                for k in range(j + 1, len(values)):

                    if values[k] < level:

                        events.append({
                            "type": "SBR",
                            "direction": "SELL",
                            "level": level,
                            "index": k
                        })

                        break

                break

    if not events:
        return None

    events.sort(key=lambda x: x["index"])

    return events[-1]


# ============================================================
# OCL
# ============================================================

def detect_ocl(candles):

    """
    OCL is based on the open/close relationship.

    Bullish OCL:
        two consecutive bullish candles.

    Bearish OCL:
        two consecutive bearish candles.
    """

    if len(candles) < 4:
        return None

    for i in range(len(candles) - 2, 0, -1):

        first = candles[i]
        second = candles[i + 1]

        if bullish(first) and bullish(second):

            return {
                "type": "OCL",
                "direction": "BUY",
                "level": first["open"],
                "index": i
            }

        if bearish(first) and bearish(second):

            return {
                "type": "OCL",
                "direction": "SELL",
                "level": first["open"],
                "index": i
            }

    return None


# ============================================================
# QMR
# ============================================================

def detect_qmr(candles):

    """
    QMR structural approximation.

    Bearish:
        Left Shoulder
        -> Head higher
        -> neckline/structure break
        -> lower Right Shoulder

    Bullish:
        Left Shoulder
        -> Head lower
        -> neckline/structure break
        -> higher Right Shoulder

    Structural comparisons use CLOSE prices.
    """

    values = line_values(candles)

    if len(values) < 15:
        return None

    swing_highs = find_swing_highs(values)
    swing_lows = find_swing_lows(values)

    candidates = []

    # --------------------------------------------------------
    # BEARISH QMR
    # --------------------------------------------------------

    for h1 in range(len(swing_highs)):

        ls = swing_highs[h1]

        for h2 in range(h1 + 1, len(swing_highs)):

            head = swing_highs[h2]

            if head["price"] <= ls["price"]:
                continue

            between = values[
                ls["index"]:head["index"] + 1
            ]

            neckline = min(between)

            break_index = None

            for i in range(head["index"] + 1, len(values)):

                if values[i] < neckline:
                    break_index = i
                    break

            if break_index is None:
                continue

            for rs in swing_highs:

                if rs["index"] <= break_index:
                    continue

                if rs["index"] > break_index + 12:
                    break

                if (
                    rs["price"] < head["price"]
                    and rs["price"] < ls["price"]
                ):

                    candidates.append({
                        "type": "QMR",
                        "direction": "SELL",
                        "level": ls["price"],
                        "index": rs["index"]
                    })

                    break

    # --------------------------------------------------------
    # BULLISH QMR
    # --------------------------------------------------------

    for l1 in range(len(swing_lows)):

        ls = swing_lows[l1]

        for l2 in range(l1 + 1, len(swing_lows)):

            head = swing_lows[l2]

            if head["price"] >= ls["price"]:
                continue

            between = values[
                ls["index"]:head["index"] + 1
            ]

            neckline = max(between)

            break_index = None

            for i in range(head["index"] + 1, len(values)):

                if values[i] > neckline:
                    break_index = i
                    break

            if break_index is None:
                continue

            for rs in swing_lows:

                if rs["index"] <= break_index:
                    continue

                if rs["index"] > break_index + 12:
                    break

                if (
                    rs["price"] > head["price"]
                    and rs["price"] > ls["price"]
                ):

                    candidates.append({
                        "type": "QMR",
                        "direction": "BUY",
                        "level": ls["price"],
                        "index": rs["index"]
                    })

                    break

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["index"])

    return candidates[-1]


# ============================================================
# KEY LEVEL DETECTION
# ============================================================

def find_key_level(candles):

    """
    Priority order:

        QMR
        RBS/SBR
        A-shape
        V-shape
        OCL
    """

    detectors = [
        detect_qmr,
        detect_rbs_sbr,
        detect_a_shape,
        detect_v_shape,
        detect_ocl
    ]

    candidates = []

    for detector in detectors:

        result = detector(candles)

        if result:
            candidates.append(result)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x.get("index", -1)
    )

    return candidates[-1]


# ============================================================
# DAILY REJECTION
# ============================================================

def confirm_daily_rejection(candles, key_level):

    if not candles or not key_level:
        return None

    latest = candles[-1]

    level = key_level["level"]

    # Price must interact with the level.
    touched = (
        latest["low"] <= level <= latest["high"]
    )

    if not touched:
        return None

    # BUY rejection
    if (
        bullish(latest)
        and latest["close"] > level
    ):

        return {
            "bias": "BUY",
            "rejection": "Bullish rejection",
            "close": latest["close"]
        }

    # SELL rejection
    if (
        bearish(latest)
        and latest["close"] < level
    ):

        return {
            "bias": "SELL",
            "rejection": "Bearish rejection",
            "close": latest["close"]
        }

    return None


# ============================================================
# DAILY BIAS
# ============================================================

def find_daily_bias(candles):

    if len(candles) < 20:
        return None

    latest_index = len(candles) - 1

    # --------------------------------------------------------
    # STEP 1
    # FIND KEY LEVEL
    # --------------------------------------------------------

    key_level = find_key_level(
        candles[:-1]
    )

    if not key_level:
        return None

    key_index = key_level.get("index", -1)

    if key_index < 5:
        return None

    # --------------------------------------------------------
    # STEP 2
    # CONFIRM THE MARKET HAD A DIRECTIONAL BOS
    # BEFORE THE REJECTION
    # --------------------------------------------------------

    prior_bos = detect_daily_bos_before_index(
        candles,
        key_index
    )

    if not prior_bos:
        return None

    # --------------------------------------------------------
    # STEP 3
    # BOS DIRECTION MUST AGREE WITH KEY-LEVEL DIRECTION
    # --------------------------------------------------------

    key_direction = key_level.get("direction")

    if (
        key_direction
        and prior_bos["direction"] != key_direction
    ):
        return None

    # --------------------------------------------------------
    # STEP4
     # CHECK THE CURRENT DAILY REJECTION
    # --------------------------------------------------------

    rejection = confirm_daily_rejection(
        candles,
        key_level
    )

    if not rejection:
        return None

    # --------------------------------------------------------
    # STEP 5
    # REJECTION DIRECTION MUST MATCH PRIOR BOS
    # --------------------------------------------------------

    if rejection["bias"] != prior_bos["direction"]:
        return None

    return {
        "pattern": key_level["type"],
        "level": key_level["level"],
        "bias": rejection["bias"],
        "rejection": rejection["rejection"],
        "close": rejection["close"],
        "prior_bos": prior_bos["type"]
    }


# ============================================================
# H4 SWEEP + CHOCH
# ============================================================

def detect_h4_sweep_choch(candles, daily_bias):

    """
    IMPORTANT:

    There is NO standalone H4 BOS anymore.

    BUY:

        1. Downside BOS = sweep
        2. Upside BOS = CHOCH

    SELL:

        1. Upside BOS = sweep
        2. Downside BOS = CHOCH

    Everything uses LINE-CHART CLOSES.
    """

    if len(candles) < 12:
        return None

    values = line_values(candles)

    # Use the recent H4 structure.
    start = max(0, len(values) - 40)

    values = values[start:]

    # ========================================================
    # BUY
    # ========================================================

    if daily_bias == "BUY":

        swing_lows = find_swing_lows(values)

        for sweep in reversed(swing_lows):

            sweep_index = sweep["index"]
            sweep_level = sweep["price"]

            if sweep_index < 2:
                continue

            # Find a prior structural high.
            previous_highs = [
                x for x in find_swing_highs(values)
                if x["index"] < sweep_index
            ]

            if not previous_highs:
                continue

            previous_high = previous_highs[-1]

            # The close must break DOWN through the prior low.
            downside_bos_index = None

            for i in range(
                previous_high["index"] + 1,
                len(values)
            ):

                if i >= sweep_index:
                    break

                if values[i] < sweep_level:

                    downside_bos_index = i
                    break

            if downside_bos_index is None:
                continue

            # After the downside break, find upside CHOCH.
            for i in range(
                downside_bos_index + 1,
                len(values)
            ):

                if values[i] > previous_high["price"]:

                    return {
                        "confirmation": "BUY Sweep + CHOCH",
                        "sweep": "Downside BOS",
                        "choch": "Upside BOS"
                    }

    # ========================================================
    # SELL
    # ========================================================

    if daily_bias == "SELL":

        swing_highs = find_swing_highs(values)

        for sweep in reversed(swing_highs):

            sweep_index = sweep["index"]
            sweep_level = sweep["price"]

            if sweep_index < 2:
                continue

            # Find a prior structural low.
            previous_lows = [
                x for x in find_swing_lows(values)
                if x["index"] < sweep_index
            ]

            if not previous_lows:
                continue

            previous_low = previous_lows[-1]

            # The close must break UP through the prior high.
            upside_bos_index = None

            for i in range(
                previous_low["index"] + 1,
                len(values)
            ):

                if i >= sweep_index:
                    break

                if values[i] > sweep_level:

                    upside_bos_index = i
                    break

            if upside_bos_index is None:
                continue

            # After the upside break, find downside CHOCH.
            for i in range(
                upside_bos_index + 1,
                len(values)
            ):

                if values[i] < previous_low["price"]:

                    return {
                        "confirmation": "SELL Sweep + CHOCH",
                        "sweep": "Upside BOS",
                        "choch": "Downside BOS"
                    }

    return None


# ============================================================
# H4 CONFIRMATION
# ============================================================

def find_h4_confirmation(candles, daily_bias):

    """
    ONLY valid H4 confirmation:

        BUY  = Downside BOS/Sweep -> Upside CHOCH
        SELL = Upside BOS/Sweep -> Downside CHOCH

    Standalone BOS is NOT accepted.
    """

    return detect_h4_sweep_choch(
        candles,
        daily_bias
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
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }).encode("utf-8")

    try:

        request = Request(
            url,
            data=payload,
            headers={
                "Content-Type":
                "application/x-www-form-urlencoded"
            }
        )

        with urlopen(request, timeout=30) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

            return result.get("ok", False)

    except Exception as e:

        print("Telegram error:", e)

        return False


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(symbol, daily, h4):

    message = (
        "📊 SLK BIAS ALERT\n\n"

        f"Symbol: {symbol}\n"
        f"Pattern: {daily['pattern']}\n"
        f"Key Level: {daily['level']}\n"
        f"Prior Daily BOS: {daily['prior_bos']}\n"
        f"Rejection: {daily['rejection']}\n"
        f"Close: {daily['close']}\n"
        f"Daily Bias: {daily['bias']}\n"
        f"H4 Confirmation: {h4['confirmation']}\n\n"

        f"Reason: Daily {daily['prior_bos']} "
        f"→ {daily['pattern']} rejection "
        f"→ {daily['rejection']} "
        f"→ H4 {h4['confirmation']}."
    )

    return message


# ============================================================
# SCAN ONE SYMBOL
# ============================================================

def scan_symbol(symbol):

    print(f"Scanning {symbol}...")

    # --------------------------------------------------------
    # DAILY
    # --------------------------------------------------------

    daily = get_candles(
        symbol,
        "1day",
        120
    )

    if len(daily) < 20:

        print(
            f"{symbol}: insufficient daily data"
        )

        return

    daily_bias = find_daily_bias(daily)

    if not daily_bias:

        return

    print(
        f"{symbol}: Daily bias = "
        f"{daily_bias['bias']} "
        f"({daily_bias['pattern']})"
    )

    # --------------------------------------------------------
    # H4
    # --------------------------------------------------------

    h4 = get_candles(
        symbol,
        "4h",
        120
    )

    if len(h4) < 12:

        print(
            f"{symbol}: insufficient H4 data"
        )

        return

    h4_confirmation = find_h4_confirmation(
        h4,
        daily_bias["bias"]
    )

    if not h4_confirmation:

        return

    # --------------------------------------------------------
    # SEND ALERT
    # --------------------------------------------------------

    message = format_alert(
        symbol,
        daily_bias,
        h4_confirmation
    )

    print(message)

    send_telegram_message(message)


# ============================================================
# SPECIAL MARKET SYMBOL RESOLUTION
# ============================================================

def resolve_special_symbol(candidates):

    for symbol in candidates:

        candles = get_candles(
            symbol,
            "1day",
            5
        )

        if candles:

            return symbol

    return None


# ============================================================
# COMPLETE MARKET LIST
# ============================================================

def get_all_symbols():

    symbols = []

    # --------------------------------------------------------
    # FOREX
    # --------------------------------------------------------

    forex_pairs = get_forex_pairs()

    for pair in forex_pairs:

        if pair not in symbols:
            symbols.append(pair)

    # --------------------------------------------------------
    # SPECIAL MARKETS
    # --------------------------------------------------------

    for name, candidates in SPECIAL_MARKETS.items():

        resolved = resolve_special_symbol(
            candidates
        )

        if resolved and resolved not in symbols:

            symbols.append(resolved)

            print(
                f"{name} resolved as {resolved}"
            )

    return symbols


# ============================================================
# FULL SCAN
# ============================================================

def run_scan():

    print("\n===================================")
    print("SLK MARKET SCAN STARTED")
    print("===================================\n")

    if not TWELVE_DATA_API_KEY:

        print("ERROR: TWELVE_DATA_API_KEY missing.")
        return

    if not TELEGRAM_BOT_TOKEN:

        print("ERROR: TELEGRAM_BOT_TOKEN missing.")
        return

    symbols = get_all_symbols()

    print(
        f"Total symbols discovered: {len(symbols)}"
    )

    for symbol in symbols:

        try:

            scan_symbol(symbol)

        except Exception as e:

            print(
                f"Error scanning {symbol}: {e}"
            )

        # Avoid hitting Twelve Data too aggressively.
        time.sleep(1)

    print("\n===================================")
    print("SLK MARKET SCAN FINISHED")
    print("===================================\n")


# ============================================================
# SCANNER LOOP
# ============================================================

def scanner_loop():

    print(
        "SLK Bias Trading Bot scanner started."
    )

    while True:

        try:

            run_scan()

        except Exception as e:

            print(
                "Scanner error:",
                e
            )

        print(
            f"Next scan in {SCAN_INTERVAL} seconds."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    # Start health server.
    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    # Start scanner.
    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner_thread.start()

    print(
        "SLK Bias Trading Bot is fully running."
    )

    # Keep Render service alive.
    while True:

        time.sleep(60)
