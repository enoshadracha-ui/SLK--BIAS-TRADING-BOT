import os
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# ============================================================
# CONFIG
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))  # 15 minutes


if not TWELVE_DATA_API_KEY:
    raise RuntimeError("Missing TWELVE_DATA_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

if not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_CHAT_ID")


# ============================================================
# HTTP HELPERS
# ============================================================

def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SLK-Bias-Bot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def twelve_data(endpoint, params):
    params["apikey"] = TWELVE_DATA_API_KEY

    url = "https://api.twelvedata.com/" + endpoint

    data = get_json(url, params)

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data error"))

    return data


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }).encode()

    request = urllib.request.Request(url, data=payload)

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


# ============================================================
# MARKET DATA
# ============================================================

def get_candles(symbol, interval, outputsize=120):
    data = twelve_data(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "order": "asc"
        }
    )

    values = data.get("values", [])

    candles = []

    for c in values:
        candles.append({
            "datetime": c["datetime"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"])
        })

    return candles


# ============================================================
# FOREX UNIVERSE
# ============================================================

def get_forex_pairs():
    data = twelve_data("forex_pairs", {})

    pairs = data.get("data", [])

    symbols = []

    for pair in pairs:
        group = str(pair.get("currency_group", "")).lower()

        if group in ("major", "minor"):
            symbol = pair.get("symbol")

            if symbol:
                symbols.append(symbol)

    return sorted(set(symbols))


SPECIAL_MARKETS = {
    "XAUUSD": ["XAU/USD", "XAUUSD"],
    "JP225": ["JP225", "NIKKEI", "NI225"],
    "NAS100": ["NAS100", "NDX"],
    "UK100": ["UK100", "FTSE"],
    "GERMAN": ["DE40", "DE30", "DAX"]
}


def build_market_list():
    markets = []

    try:
        markets.extend(get_forex_pairs())
    except Exception as e:
        print("Forex pair discovery failed:", e)

    for name, candidates in SPECIAL_MARKETS.items():
        markets.append(candidates[0])

    return sorted(set(markets))


# ============================================================
# CANDLE HELPERS
# ============================================================

def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def body(c):
    return abs(c["close"] - c["open"])


def midpoint(a, b):
    return (a + b) / 2


# ============================================================
# KEY LEVEL DETECTION
# ============================================================

def detect_resistance(candles):
    if len(candles) < 3:
        return None

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    if bullish(a) and bearish(b):
        level = max(a["high"], b["high"])

        return {
            "pattern": "Resistance",
            "level": level,
            "direction": "SELL"
        }

    return None


def detect_support(candles):
    if len(candles) < 3:
        return None

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    if bearish(a) and bullish(b):
        level = min(a["low"], b["low"])

        return {
            "pattern": "Support",
            "level": level,
            "direction": "BUY"
        }

    return None


def detect_ocl(candles):
    if len(candles) < 3:
        return None

    a = candles[-3]
    b = candles[-2]

    if bullish(a) and bullish(b):
        level = midpoint(a["open"], a["close"])

        return {
            "pattern": "Bullish OCL",
            "level": level,
            "direction": "BUY"
        }

    if bearish(a) and bearish(b):
        level = midpoint(a["open"], a["close"])

        return {
            "pattern": "Bearish OCL",
            "level": level,
            "direction": "SELL"
        }

    return None


def detect_rbs_sbr(candles):
    if len(candles) < 6:
        return None

    old = candles[-6]
    break_candle = candles[-4]
    retest = candles[-2]

    old_high = old["high"]
    old_low = old["low"]

    # Resistance broken upward and later rejected as support
    if (
        break_candle["close"] > old_high
        and retest["low"] <= old_high
        and retest["close"] > old_high
    ):
        return {
            "pattern": "RBS",
            "level": old_high,
            "direction": "BUY"
        }

    # Support broken downward and later rejected as resistance
    if (
        break_candle["close"] < old_low
        and retest["high"] >= old_low
        and retest["close"] < old_low
    ):
        return {
            "pattern": "SBR",
            "level": old_low,
            "direction": "SELL"
        }

    return None


# ============================================================
# QMR
# ============================================================

def detect_qmr(candles):
    if len(candles) < 7:
        return None

    a = candles[-7]
    b = candles[-6]
    c = candles[-5]
    d = candles[-4]
    e = candles[-3]
    f = candles[-2]

    # --------------------------------------------------------
    # BEARISH QUASIMODO
    #
    # Left shoulder
    # Pullback
    # Head = higher high
    # Break below previous structure
    # Right shoulder = lower high
    # QMR level = left shoulder high
    # --------------------------------------------------------

    if (
        a["high"] < c["high"]
        and c["high"] > e["high"]
        and e["high"] < c["high"]
        and f["close"] < a["low"]
    ):
        return {
            "pattern": "Bearish QMR",
            "level": a["high"],
            "direction": "SELL"
        }

    # --------------------------------------------------------
    # BULLISH QUASIMODO
    #
    # Left shoulder
    # Pullback
    # Head = lower low
    # Break above previous structure
    # Right shoulder = higher low
    # QMR level = left shoulder low
    # --------------------------------------------------------

    if (
        a["low"] > c["low"]
        and c["low"] < e["low"]
        and e["low"] > c["low"]
        and f["close"] > a["high"]
    ):
        return {
            "pattern": "Bullish QMR",
            "level": a["low"],
            "direction": "BUY"
        }

    return None


# ============================================================
# DAILY KEY LEVEL
# ============================================================

def find_daily_key_level(candles):
    detectors = [
        detect_qmr,
        detect_rbs_sbr,
        detect_ocl,
        detect_resistance,
        detect_support
    ]

    for detector in detectors:
        result = detector(candles)

        if result:
            return result

    return None


# ============================================================
# DAILY REJECTION
# ============================================================

def confirm_daily_bias(candles, key):
    if not key:
        return None

    candle = candles[-1]

    level = key["level"]

    # BUY rejection
    if key["direction"] == "BUY":
        if (
            candle["low"] <= level
            and candle["close"] > level
        ):
            return "BUY"

    # SELL rejection
    if key["direction"] == "SELL":
        if (
            candle["high"] >= level
            and candle["close"] < level
        ):
            return "SELL"

    return None


# ============================================================
# H4 BOS
# ============================================================

def h4_bos(candles):
    if len(candles) < 6:
        return None

    previous = candles[-3]
    current = candles[-1]

    previous_high = previous["high"]
    previous_low = previous["low"]

    if current["close"] > previous_high:
        return "BUY"

    if current["close"] < previous_low:
        return "SELL"

    return None


# ============================================================
# H4 SWEEP + BREAK
# ============================================================

def h4_sweep_and_break(candles):
    if len(candles) < 8:
        return None

    sweep = candles[-4]
    confirmation = candles[-1]

    earlier_high = max(c["high"] for c in candles[-8:-4])
    earlier_low = min(c["low"] for c in candles[-8:-4])

    # BUY:
    # downside sweep first, then upside break
    if (
        sweep["low"] < earlier_low
        and confirmation["close"] > sweep["high"]
    ):
        return "BUY"

    # SELL:
    # upside sweep first, then downside break
    if (
        sweep["high"] > earlier_high
        and confirmation["close"] < sweep["low"]
    ):
        return "SELL"

    return None


# ============================================================
# H4 CONFIRMATION
# ============================================================

def get_h4_confirmation(candles):
    sweep_break = h4_sweep_and_break(candles)

    if sweep_break:
        return sweep_break, "Sweep + Break"

    bos = h4_bos(candles)

    if bos:
        return bos, "BOS"

    return None, None


# ============================================================
# FINAL SLK BIAS
# ============================================================

def analyze_market(symbol):
    try:
        daily = get_candles(symbol, "1day", 120)
        h4 = get_candles(symbol, "4h", 120)

        if len(daily) < 10 or len(h4) < 10:
            return None

        key = find_daily_key_level(daily)

        if not key:
            return None

        daily_bias = confirm_daily_bias(daily, key)

        if not daily_bias:
            return None

        h4_bias, confirmation = get_h4_confirmation(h4)

        if not h4_bias:
            return None

        # Daily and H4 must agree
        if daily_bias != h4_bias:
            return None

        return {
            "symbol": symbol,
            "pattern": key["pattern"],
            "level": key["level"],
            "daily_bias": daily_bias,
            "h4_confirmation": confirmation,
            "last_daily_close": daily[-1]["close"],
            "reason": (
                f"{key['pattern']} rejection confirmed on Daily, "
                f"followed by H4 {confirmation} in the same direction."
            )
        }

    except Exception as e:
        print(f"{symbol} error:", e)
        return None


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def format_alert(result):
    bias = result["daily_bias"]

    emoji = "🟢" if bias == "BUY" else "🔴"

    return (
        f"{emoji} SLK BIAS ALERT\n\n"
        f"Symbol: {result['symbol']}\n"
        f"Pattern: {result['pattern']}\n"
        f"Key Level: {result['level']:.5f}\n"
        f"Daily Bias: {bias}\n"
        f"H4 Confirmation: {result['h4_confirmation']}\n"
        f"Daily Close: {result['last_daily_close']:.5f}\n\n"
        f"Reason: {result['reason']}\n\n"
        f"⚠️ Bias only — no trade execution."
    )


# ============================================================
# MAIN SCANNER
# ============================================================

def scan():
    print("Building market list...")

    markets = build_market_list()

    print(f"Markets found: {len(markets)}")

    alerts = 0

    for symbol in markets:
        print("Scanning:", symbol)

        result = analyze_market(symbol)

        if result:
            message = format_alert(result)

            try:
                send_telegram(message)
                alerts += 1
                print("Alert sent:", symbol)
            except Exception as e:
                print("Telegram error:", e)

    print(f"Scan completed. Alerts sent: {alerts}")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("SLK Bias Bot started.")

    while True:
        try:
            scan()
        except Exception as e:
            print("Scanner error:", e)

        print(f"Waiting {SCAN_INTERVAL} seconds...")
        time.sleep(SCAN_INTERVAL)
