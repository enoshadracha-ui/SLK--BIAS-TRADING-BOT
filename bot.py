import os
import time
import threading
import json
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# CONFIG
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))  # 15 minutes
PORT = int(os.getenv("PORT", "10000"))

TWELVE_DATA_URL = "https://api.twelvedata.com"


# ============================================================
# BASIC HTTP SERVER FOR RENDER WEB SERVICE
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


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Web service running on port {PORT}")
    server.serve_forever()


# ============================================================
# HTTP HELPERS
# ============================================================

def get_json(url, params=None):
    try:
        if params:
            url += "?" + urlencode(params)

        request = Request(
            url,
            headers={
                "User-Agent": "SLK-Bias-Bot/1.0"
            }
        )

        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    except Exception as e:
        print(f"Request error: {e}")
        return None


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(symbol, interval, outputsize=120):
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON"
    }

    data = get_json(
        f"{TWELVE_DATA_URL}/time_series",
        params
    )

    if not data or "values" not in data:
        return []

    candles = []

    for item in reversed(data["values"]):
        try:
            candles.append({
                "datetime": item["datetime"],
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"])
            })
        except (KeyError, ValueError):
            continue

    return candles


def get_forex_pairs():
    params = {
        "apikey": TWELVE_DATA_API_KEY
    }

    data = get_json(
        f"{TWELVE_DATA_URL}/forex_pairs",
        params
    )

    if not data:
        return []

    pairs = []

    for item in data.get("data", []):
        group = str(item.get("currency_group", "")).lower()

        if group in ["major", "minor"]:
            symbol = item.get("symbol")

            if symbol:
                pairs.append(symbol)

    return sorted(set(pairs))


# ============================================================
# SPECIAL MARKETS
# ============================================================

SPECIAL_MARKETS = {
    "XAUUSD": [
        "XAU/USD",
        "XAUUSD"
    ],

    "JP225": [
        "JP225",
        "NIKKEI",
        "NI225"
    ],

    "NAS100": [
        "NAS100",
        "NDX"
    ],

    "UK100": [
        "UK100",
        "FTSE"
    ],

    "GERMAN": [
        "DE40",
        "DE30",
        "DAX"
    ]
}


def get_special_symbol(name):
    for symbol in SPECIAL_MARKETS.get(name, []):
        candles = get_candles(symbol, "1day", 5)

        if candles:
            return symbol

    return None


# ============================================================
# CANDLE UTILITIES
# ============================================================

def bullish(candle):
    return candle["close"] > candle["open"]


def bearish(candle):
    return candle["close"] < candle["open"]


def candle_range(candle):
    return candle["high"] - candle["low"]


def body_size(candle):
    return abs(candle["close"] - candle["open"])


# ============================================================
# KEY LEVEL DETECTION
# ============================================================

def detect_resistance(candles):
    if len(candles) < 3:
        return None

    for i in range(len(candles) - 2, 0, -1):
        left = candles[i - 1]
        middle = candles[i]
        right = candles[i + 1]

        if (
            middle["high"] >= left["high"]
            and middle["high"] >= right["high"]
            and bearish(right)
        ):
            return {
                "type": "Resistance",
                "level": middle["high"]
            }

    return None


def detect_support(candles):
    if len(candles) < 3:
        return None

    for i in range(len(candles) - 2, 0, -1):
        left = candles[i - 1]
        middle = candles[i]
        right = candles[i + 1]

        if (
            middle["low"] <= left["low"]
            and middle["low"] <= right["low"]
            and bullish(right)
        ):
            return {
                "type": "Support",
                "level": middle["low"]
            }

    return None


def detect_ocl(candles):
    if len(candles) < 3:
        return None

    for i in range(len(candles) - 2, 0, -1):
        first = candles[i]
        second = candles[i + 1]

        if bullish(first) and bullish(second):
            return {
                "type": "OCL",
                "level": first["open"]
            }

        if bearish(first) and bearish(second):
            return {
                "type": "OCL",
                "level": first["open"]
            }

    return None


def detect_rbs_sbr(candles):
    if len(candles) < 5:
        return None

    for i in range(len(candles) - 3, 1, -1):

        old_level = candles[i]["high"]

        # Resistance becoming support
        if (
            candles[i + 1]["close"] > old_level
            and candles[i + 2]["low"] <= old_level
            and candles[i + 2]["close"] > old_level
        ):
            return {
                "type": "RBS",
                "level": old_level
            }

        old_level = candles[i]["low"]

        # Support becoming resistance
        if (
            candles[i + 1]["close"] < old_level
            and candles[i + 2]["high"] >= old_level
            and candles[i + 2]["close"] < old_level
        ):
            return {
                "type": "SBR",
                "level": old_level
            }

    return None


# ============================================================
# QMR / QUASIMODO REVERSAL
# ============================================================

def detect_qmr(candles):
    """
    QMR reference:

    Bearish QM:
    Left Shoulder high
    -> Head higher high
    -> break of prior low
    -> Right Shoulder lower high

    QM level = Left Shoulder high

    Bullish QM:
    Left Shoulder low
    -> Head lower low
    -> break of prior high
    -> Right Shoulder higher low

    This is a structural approximation and should be
    validated against the user's SLK reference examples.
    """

    if len(candles) < 10:
        return None

    # Search recent structures.
    start = max(2, len(candles) - 50)

    for i in range(len(candles) - 3, start - 1, -1):

        ls = candles[i - 2]
        pullback = candles[i - 1]
        head = candles[i]

        # ----------------------------------------------------
        # BEARISH QMR
        # ----------------------------------------------------

        if (
            ls["high"] > pullback["high"]
            and head["high"] > ls["high"]
        ):

            left_low = min(
                ls["low"],
                pullback["low"]
            )

            # Find break below prior low
            for j in range(i + 1, min(i + 5, len(candles))):

                if candles[j]["close"] < left_low:

                    # Look for right shoulder
                    for k in range(j + 1, min(j + 5, len(candles))):

                        if (
                            candles[k]["high"] < head["high"]
                            and candles[k]["high"] < ls["high"]
                        ):
                            return {
                                "type": "QMR",
                                "direction": "SELL",
                                "level": ls["high"]
                            }

        # ----------------------------------------------------
        # BULLISH QMR
        # ----------------------------------------------------

        if (
            ls["low"] < pullback["low"]
            and head["low"] < ls["low"]
        ):

            left_high = max(
                ls["high"],
                pullback["high"]
            )

            # Find break above prior high
            for j in range(i + 1, min(i + 5, len(candles))):

                if candles[j]["close"] > left_high:

                    # Look for right shoulder
                    for k in range(j + 1, min(j + 5, len(candles))):

                        if (
                            candles[k]["low"] > head["low"]
                            and candles[k]["low"] > ls["low"]
                        ):
                            return {
                                "type": "QMR",
                                "direction": "BUY",
                                "level": ls["low"]
                            }

    return None


# ============================================================
# DAILY REJECTION
# ============================================================

def confirm_daily_rejection(candles, key_level):
    if not candles or not key_level:
        return None

    latest = candles[-1]
    level = key_level["level"]

    touched = (
        latest["low"] <= level <= latest["high"]
    )

    if not touched:
        return None

    if bullish(latest) and latest["close"] > level:
        return {
            "bias": "BUY",
            "rejection": "Bullish rejection",
            "close": latest["close"]
        }

    if bearish(latest) and latest["close"] < level:
        return {
            "bias": "SELL",
            "rejection": "Bearish rejection",
            "close": latest["close"]
        }

    return None


# ============================================================
# FIND DAILY BIAS
# ============================================================

def find_daily_bias(candles):
    detectors = [
        detect_qmr,
        detect_rbs_sbr,
        detect_resistance,
        detect_support,
        detect_ocl
    ]

    for detector in detectors:

        key = detector(candles)

        if not key:
            continue

        rejection = confirm_daily_rejection(
            candles,
            key
        )

        if rejection:
            return {
                "pattern": key["type"],
                "level": key["level"],
                "bias": rejection["bias"],
                "rejection": rejection["rejection"],
                "close": rejection["close"]
            }

    return None


# ============================================================
# H4 BOS
# ============================================================

def detect_h4_bos(candles):
    if len(candles) < 6:
        return None

    recent = candles[-6:]

    # BUY BOS
    previous_high = max(
        candle["high"]
        for candle in recent[:-1]
    )

    latest = recent[-1]

    if latest["close"] > previous_high:
        return "BUY BOS"

    # SELL BOS
    previous_low = min(
        candle["low"]
        for candle in recent[:-1]
    )

    if latest["close"] < previous_low:
        return "SELL BOS"

    return None


# ============================================================
# H4 SWEEP + BREAK
# ============================================================

def detect_h4_sweep_break(candles):
    if len(candles) < 8:
        return None

    recent = candles[-8:]

    # --------------------------------------------------------
    # BUY:
    # downside sweep / bearish BOS first
    # followed by bullish CHOCH
    # --------------------------------------------------------

    for i in range(2, len(recent) - 1):

        before = recent[:i]
        sweep = recent[i]

        prior_low = min(
            c["low"] for c in before
        )

        if sweep["low"] < prior_low:

            after = recent[i + 1:]

            if not after:
                continue

            previous_high = max(
                c["high"] for c in before
            )

            for confirmation in after:

                if confirmation["close"] > previous_high:
                    return "BUY Sweep + CHOCH"

    # --------------------------------------------------------
    # SELL:
    # upside sweep / bullish BOS first
    # followed by bearish CHOCH
    # --------------------------------------------------------

    for i in range(2, len(recent) - 1):

        before = recent[:i]
        sweep = recent[i]

        prior_high = max(
            c["high"] for c in before
        )

        if sweep["high"] > prior_high:

            after = recent[i + 1:]

            if not after:
                continue

            previous_low = min(
                c["low"] for c in before
            )

            for confirmation in after:

                if confirmation["close"] < previous_low:
                    return "SELL Sweep + CHOCH"

    return None


# ============================================================
# H4 CONFIRMATION
# ============================================================

def find_h4_confirmation(candles, daily_bias):
    sweep_break = detect_h4_sweep_break(candles)

    if sweep_break:

        if daily_bias == "BUY" and sweep_break.startswith("BUY"):
            return sweep_break

        if daily_bias == "SELL" and sweep_break.startswith("SELL"):
            return sweep_break

    bos = detect_h4_bos(candles)

    if bos:

        if daily_bias == "BUY" and bos.startswith("BUY"):
            return bos

        if daily_bias == "SELL" and bos.startswith("SELL"):
            return bos

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }).encode()

    try:
        request = Request(
            url,
            data=payload,
            method="POST"
        )

        with urlopen(request, timeout=30) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        return result.get("ok", False)

    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(symbol, daily, h4_confirmation):
    return (
        "📊 SLK BIAS ALERT\n\n"
        f"Symbol: {symbol}\n"
        f"Pattern: {daily['pattern']}\n"
        f"Key Level: {daily['level']}\n"
        f"Rejection: {daily['rejection']}\n"
        f"Close: {daily['close']}\n"
        f"Daily Bias: {daily['bias']}\n"
        f"H4 Confirmation: {h4_confirmation}\n\n"
        f"Reason: Daily {daily['bias']} rejection "
        f"at {daily['pattern']} + H4 confirmation."
    )


# ============================================================
# SYMBOL SCANNER
# ============================================================

def scan_symbol(symbol):
    print(f"Scanning {symbol}...")

    daily = get_candles(
        symbol,
        "1day",
        120
    )

    h4 = get_candles(
        symbol,
        "4h",
        120
    )

    if len(daily) < 10 or len(h4) < 10:
        print(f"Not enough data for {symbol}")
        return None

    daily_bias = find_daily_bias(daily)

    if not daily_bias:
        return None

    h4_confirmation = find_h4_confirmation(
        h4,
        daily_bias["bias"]
    )

    if not h4_confirmation:
        return None

    return format_alert(
        symbol,
        daily_bias,
        h4_confirmation
    )


# ============================================================
# DUPLICATE ALERT PROTECTION
# ============================================================

sent_signals = {}


def should_send(symbol, message):
    previous = sent_signals.get(symbol)

    if previous == message:
        return False

    sent_signals[symbol] = message
    return True


# ============================================================
# FULL MARKET SCAN
# ============================================================

def run_scan():
    print("\n================================")
    print("Starting SLK market scan")
    print("================================")

    symbols = get_forex_pairs()

    print(f"Found {len(symbols)} forex pairs.")

    # Add special markets
    for market_name in SPECIAL_MARKETS:

        symbol = get_special_symbol(market_name)

        if symbol:
            symbols.append(symbol)
            print(
                f"{market_name} available as {symbol}"
            )

    symbols = sorted(set(symbols))

    for symbol in symbols:

        try:
            alert = scan_symbol(symbol)

            if alert:

                if should_send(symbol, alert):

                    print(alert)
                    send_telegram(alert)

                else:
                    print(
                        f"Duplicate signal skipped: {symbol}"
                    )

        except Exception as e:
            print(
                f"Error scanning {symbol}: {e}"
            )

        # Small delay to reduce API pressure
        time.sleep(1)

    print("Scan completed.")


# ============================================================
# BACKGROUND SCANNER
# ============================================================

def scanner_loop():

    print("SLK scanner started.")

    while True:

        try:
            run_scan()

        except Exception as e:
            print(
                f"Scanner error: {e}"
            )

        print(
            f"Waiting {SCAN_INTERVAL} seconds "
            f"until next scan..."
        )

        time.sleep(SCAN_INTERVAL)


# ============================================================
# START EVERYTHING
# ============================================================

if __name__ == "__main__":

    if not TWELVE_DATA_API_KEY:
        print("WARNING: TWELVE_DATA_API_KEY is missing.")

    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN is missing.")

    if not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_CHAT_ID is missing.")

    # Start Render web server
    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    # Start SLK scanner
    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

  
    
        
