import requests
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = "8618398305:AAHOrELkeYLHTTliL3xnEHe6onZ7AOy-0Os"
TELEGRAM_CHAT_ID    = "-1003930522837"
MEXC_BASE_URL       = "https://contract.mexc.com/api/v1"
TIMEFRAME           = "Min60"
CHECK_INTERVAL      = 60
OB_MAX_AGE_HOURS    = 48
ALERT_COOLDOWN      = 1 * 3600       # 1h gap between alerts per coin
MAX_ALERTS_PER_DAY  = 4
MIN_VOLUME_USDT     = 5_000_000
VOLUME_MULTIPLIER   = 1.5
MIN_BODY_RATIO      = 0.4

# ── FILTERS v5 (TIGHTENED) ────────────────────
OPPOSING_OB_BLOCK_PCT = 5.0
MITIGATION_MOVE_PCT   = 2.0

RSI_PERIOD            = 14
RSI_BUY_MAX           = 55
RSI_SELL_MIN          = 45

# ── v5 CONFIGS ────────────────────────────────
EMA_FAST              = 20
EMA_SLOW              = 50
MIN_OB_RANGE_PCT      = 0.3
RETEST_VOL_RATIO_MAX  = 0.85
MIN_PROB_SCORE        = 60
CLOSE_INSIDE_OB       = True

TOUCHED_ALERTS:    dict = {}
DAILY_ALERT_COUNT: dict = {}

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return False

# ─────────────────────────────────────────────
#  SYMBOL LIST
# ─────────────────────────────────────────────
def get_top_symbols(limit: int = 500) -> list[str]:
    try:
        r    = requests.get(f"{MEXC_BASE_URL}/contract/ticker", timeout=20)
        data = r.json()
        if data.get("success"):
            tickers = []
            for t in data["data"]:
                if "_USDT" not in t.get("symbol", ""):
                    continue
                vol      = float(t.get("volume24", 0) or 0)
                price    = float(t.get("lastPrice",  0) or 0)
                vol_usdt = vol * price
                if vol_usdt < MIN_VOLUME_USDT:
                    continue
                tickers.append({"symbol": t["symbol"], "vol_usdt": vol_usdt})
            tickers.sort(key=lambda x: x["vol_usdt"], reverse=True)
            syms = [t["symbol"] for t in tickers[:limit]]
            print(f"[INFO] {len(syms)} coins passed volume filter")
            return syms
    except Exception as e:
        print(f"[SYMBOL ERROR] {e}")
    return []

# ─────────────────────────────────────────────
#  CANDLES
# ─────────────────────────────────────────────
def get_candles(symbol: str, limit: int = 150) -> list[dict]:
    """
    Fetch candles and SORT by time ascending.
    ✅ FIX: MEXC may return candles in any order.
    Sorting ensures OB detection, EMA, and mitigation logic
    all run on correctly-ordered (oldest → newest) data.
    Without this, signals can be completely reversed.
    """
    try:
        r    = requests.get(
            f"{MEXC_BASE_URL}/contract/kline/{symbol}",
            params={"interval": TIMEFRAME, "limit": limit},
            timeout=15,
        )
        data = r.json()
        if data.get("success") and data.get("data"):
            raw = data["data"]
            candles = [
                {
                    "time":  int(raw["time"][i]),
                    "open":  float(raw["open"][i]),
                    "high":  float(raw["high"][i]),
                    "low":   float(raw["low"][i]),
                    "close": float(raw["close"][i]),
                    "vol":   float(raw["vol"][i]),
                }
                for i in range(len(raw["time"]))
            ]
            # ✅ CRITICAL FIX: Always sort oldest → newest
            candles.sort(key=lambda x: x["time"])
            return candles
    except Exception as e:
        print(f"[CANDLE ERROR] {symbol}: {e}")
    return []

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_fresh(ts: int) -> bool:
    return (time.time() - ts) / 3600 <= OB_MAX_AGE_HOURS

def fmt_price(p: float) -> str:
    dec = 6 if p < 0.01 else 4 if p < 1 else 2
    return f"{p:.{dec}f}"

def make_tradingview_link(symbol: str) -> str:
    coin = symbol.replace("_USDT", "")
    return f"https://www.tradingview.com/chart/?symbol=MEXC%3A{coin}USDT&interval=60"

def average_volume(candles: list[dict]) -> float:
    vols = [c["vol"] for c in candles if c["vol"] > 0]
    return sum(vols) / len(vols) if vols else 0

# ─────────────────────────────────────────────
#  EMA CALCULATION
# ─────────────────────────────────────────────
def calculate_ema(candles: list[dict], period: int) -> list[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return []
    k      = 2 / (period + 1)
    ema    = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    pad = [None] * (period - 1)
    return pad + ema  # type: ignore


def get_trend(candles: list[dict]) -> str:
    """
    EMA20 vs EMA50 trend filter.
    Returns: "BULLISH", "BEARISH", or "NEUTRAL"
    NOTE: candles must be sorted oldest→newest (guaranteed by get_candles fix).
    """
    ema_fast = calculate_ema(candles, EMA_FAST)
    ema_slow = calculate_ema(candles, EMA_SLOW)

    if len(ema_fast) < 3 or len(ema_slow) < 3:
        return "NEUTRAL"

    valid_fast = [v for v in ema_fast if v is not None]
    valid_slow = [v for v in ema_slow if v is not None]

    if len(valid_fast) < 3 or len(valid_slow) < 1:
        return "NEUTRAL"

    fast_now  = valid_fast[-1]
    fast_prev = valid_fast[-2]
    fast_pp   = valid_fast[-3]
    slow_now  = valid_slow[-1]

    ema_rising  = fast_now > fast_prev > fast_pp
    ema_falling = fast_now < fast_prev < fast_pp

    if fast_now > slow_now and ema_rising:
        return "BULLISH"
    elif fast_now < slow_now and ema_falling:
        return "BEARISH"
    else:
        return "NEUTRAL"


# ─────────────────────────────────────────────
#  ALERT RATE LIMITER
# ─────────────────────────────────────────────
def can_alert(coin: str, now: float) -> tuple[bool, str]:
    coin_key  = f"{coin}_last_alert"
    today_str = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")

    last_sent = TOUCHED_ALERTS.get(coin_key, 0)
    if now - last_sent < ALERT_COOLDOWN:
        wait_min = int((ALERT_COOLDOWN - (now - last_sent)) / 60)
        return False, f"cooldown ({wait_min}m left)"

    entry = DAILY_ALERT_COUNT.get(coin, {"date": "", "count": 0})
    if entry["date"] != today_str:
        entry = {"date": today_str, "count": 0}
        DAILY_ALERT_COUNT[coin] = entry

    if entry["count"] >= MAX_ALERTS_PER_DAY:
        return False, f"daily cap reached ({MAX_ALERTS_PER_DAY}/day)"

    return True, "ok"


def record_alert(coin: str, ob_key: str, now: float) -> None:
    coin_key  = f"{coin}_last_alert"
    today_str = datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")

    TOUCHED_ALERTS[ob_key]   = now
    TOUCHED_ALERTS[coin_key] = now

    entry = DAILY_ALERT_COUNT.get(coin, {"date": today_str, "count": 0})
    if entry["date"] != today_str:
        entry = {"date": today_str, "count": 0}
    entry["count"] += 1
    DAILY_ALERT_COUNT[coin] = entry


# ─────────────────────────────────────────────
#  RSI
# ─────────────────────────────────────────────
def calculate_rsi(candles: list[dict], period: int = RSI_PERIOD) -> float:
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return -1

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        diff     = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0))  / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ─────────────────────────────────────────────
#  ORDER BLOCK DETECTION
# ─────────────────────────────────────────────
def detect_order_blocks(candles: list[dict]) -> list[dict]:
    """
    Detect BUY and SELL Order Blocks.
    Candles must be sorted oldest→newest (guaranteed by get_candles fix).

    BUY OB  = last bearish candle before a bullish impulse → expect LONG on retest
    SELL OB = last bullish candle before a bearish impulse → expect SHORT on retest
    """
    if len(candles) < 10:
        return []

    avg_vol = average_volume(candles)
    obs     = []

    for i in range(2, len(candles) - 1):
        ob_c = candles[i - 1]   # OB candle (chronologically before trigger)
        trig = candles[i]       # trigger candle (impulse)
        conf = candles[i + 1]   # confirmation candle

        if not is_fresh(ob_c["time"]):
            continue

        ob_range = ob_c["high"] - ob_c["low"]
        if ob_range == 0:
            continue

        ob_range_pct = (ob_range / ob_c["close"]) * 100
        if ob_range_pct < MIN_OB_RANGE_PCT:
            continue

        ob_body = abs(ob_c["close"] - ob_c["open"])
        if ob_body / ob_range < MIN_BODY_RATIO:
            continue

        if avg_vol > 0 and ob_c["vol"] < avg_vol * VOLUME_MULTIPLIER:
            continue

        # BUY OB: bearish OB candle → bullish trigger → bullish confirmation
        if (ob_c["close"] < ob_c["open"]
                and trig["close"] > trig["open"]
                and conf["close"] > trig["high"]):
            obs.append({
                "type":   "BUY",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
                "vol":    ob_c["vol"],
            })

        # SELL OB: bullish OB candle → bearish trigger → bearish confirmation
        elif (ob_c["close"] > ob_c["open"]
                and trig["close"] < trig["open"]
                and conf["close"] < trig["low"]):
            obs.append({
                "type":   "SELL",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
                "vol":    ob_c["vol"],
            })

    return obs

# ─────────────────────────────────────────────
#  MITIGATION CHECK
# ─────────────────────────────────────────────
def was_mitigated(ob: dict, candles: list[dict]) -> bool:
    """
    Returns True when price has RETURNED to the OB zone after the initial impulse.
    This is the retest we want to trade.

    BUY OB:
      1. After OB, price moved UP ≥ MITIGATION_MOVE_PCT% (impulse away)
      2. Then price came BACK DOWN into OB zone (retest = mitigation)

    SELL OB:
      1. After OB, price moved DOWN ≥ MITIGATION_MOVE_PCT% (impulse away)
      2. Then price came BACK UP into OB zone (retest = mitigation)
    """
    post = [c for c in candles if c["time"] > ob["time"]]
    if len(post) < 2:
        return False

    if ob["type"] == "BUY":
        # Step 1: price must first rally 2%+ above OB top
        threshold_up = ob["top"] * (1 + MITIGATION_MOVE_PCT / 100)
        moved_up     = False
        moved_up_idx = -1
        for idx, c in enumerate(post):
            if c["high"] >= threshold_up:
                moved_up     = True
                moved_up_idx = idx
                break
        if not moved_up:
            return False
        # Step 2: price must then pull back INTO the OB zone
        for c in post[moved_up_idx + 1:]:
            if c["low"] <= ob["top"]:
                return True
        return False

    else:  # SELL OB
        # Step 1: price must first drop 2%+ below OB bottom
        threshold_dn = ob["bottom"] * (1 - MITIGATION_MOVE_PCT / 100)
        moved_dn     = False
        moved_dn_idx = -1
        for idx, c in enumerate(post):
            if c["low"] <= threshold_dn:
                moved_dn     = True
                moved_dn_idx = idx
                break
        if not moved_dn:
            return False
        # Step 2: price must then bounce back INTO the OB zone
        for c in post[moved_dn_idx + 1:]:
            if c["high"] >= ob["bottom"]:
                return True
        return False

# ─────────────────────────────────────────────
#  RETEST VOLUME CHECK
# ─────────────────────────────────────────────
def is_retest_volume_weak(ob: dict, candles: list[dict], price: float) -> bool:
    """
    Checks if the volume on RETEST is weaker than the OB candle.
    Weak retest volume = sellers/buyers exhausted = good signal.
    Strong retest volume = conviction to break through = bad signal.
    """
    ob_vol = ob.get("vol", 0)
    if ob_vol == 0:
        return True

    retest_candles = []
    for c in candles[-10:]:
        if c["time"] <= ob["time"]:
            continue
        if ob["type"] == "BUY":
            if c["low"] <= ob["top"]:
                retest_candles.append(c)
        else:
            if c["high"] >= ob["bottom"]:
                retest_candles.append(c)

    if not retest_candles:
        return True

    avg_retest_vol = sum(c["vol"] for c in retest_candles[-3:]) / len(retest_candles[-3:])
    ratio          = avg_retest_vol / ob_vol

    return ratio <= RETEST_VOL_RATIO_MAX


# ─────────────────────────────────────────────
#  OPPOSING OB BLOCK CHECK  ← ✅ CRITICAL FIX
# ─────────────────────────────────────────────
def has_opposing_ob_in_path(
    signal_type: str,
    price: float,
    obs: list[dict],
    candles: list[dict],
) -> tuple[bool, float | None]:
    """
    ✅ FIXED v5.1 — Previously had INVERTED logic.

    OLD (WRONG): blocked signal only if opposing OB was MITIGATED (already consumed/weak)
    NEW (CORRECT): blocks signal if opposing OB is FRESH (not yet mitigated = strong S/R)

    Logic:
      BUY  signal → block if there is a fresh (unmitigated) SELL OB above price.
                    A fresh SELL OB above = overhead resistance that could stop the rally.
      SELL signal → block if there is a fresh (unmitigated) BUY OB below price.
                    A fresh BUY OB below = support that could stop the drop.

    A mitigated opposing OB has already been retested and is considered consumed —
    it no longer acts as strong S/R, so we do NOT block the signal for those.
    """
    if signal_type == "BUY":
        upper_limit = price * (1 + OPPOSING_OB_BLOCK_PCT / 100)
        for ob in obs:
            if ob["type"] != "SELL":
                continue
            if price < ob["bottom"] <= upper_limit:
                # ✅ FIXED: block if FRESH (not mitigated) = real resistance
                if not was_mitigated(ob, candles):
                    return True, ob["bottom"]
        return False, None

    else:  # SELL signal
        lower_limit = price * (1 - OPPOSING_OB_BLOCK_PCT / 100)
        for ob in obs:
            if ob["type"] != "BUY":
                continue
            if lower_limit <= ob["top"] < price:
                # ✅ FIXED: block if FRESH (not mitigated) = real support
                if not was_mitigated(ob, candles):
                    return True, ob["top"]
        return False, None

# ─────────────────────────────────────────────
#  TOUCH CHECK
# ─────────────────────────────────────────────
def is_touching(price: float, ob: dict, last_candle: dict) -> bool:
    """
    If CLOSE_INSIDE_OB = True (strict mode):
      Require the last candle's BODY to overlap the OB zone.
      Avoids wick-touches that reverse before candle close.

    If CLOSE_INSIDE_OB = False (loose mode):
      Just check if live price is in the OB zone (with 10% tolerance).
    """
    spread = ob["top"] - ob["bottom"]
    tol    = spread * 0.10

    if CLOSE_INSIDE_OB:
        body_high = max(last_candle["open"], last_candle["close"])
        body_low  = min(last_candle["open"], last_candle["close"])
        return body_low <= (ob["top"] + tol) and body_high >= (ob["bottom"] - tol)
    else:
        return (ob["bottom"] - tol) <= price <= (ob["top"] + tol)

# ─────────────────────────────────────────────
#  LIVE PRICE MAP
# ─────────────────────────────────────────────
def get_price_map() -> dict[str, float]:
    try:
        r    = requests.get(f"{MEXC_BASE_URL}/contract/ticker", timeout=15)
        data = r.json()
        if data.get("success"):
            return {
                t["symbol"]: float(t["lastPrice"])
                for t in data["data"] if t.get("lastPrice")
            }
    except Exception as e:
        print(f"[PRICE MAP ERROR] {e}")
    return {}

# ─────────────────────────────────────────────
#  SUGGEST SL / TP
# ─────────────────────────────────────────────
def suggest_sl_tp(ob: dict, price: float) -> tuple[float, float]:
    if ob["type"] == "BUY":
        sl   = ob["bottom"] * 0.99
        risk = price - sl
        tp   = price + risk * 2
    else:
        sl   = ob["top"] * 1.01
        risk = sl - price
        tp   = price - risk * 2
    return round(sl, 6), round(tp, 6)

# ─────────────────────────────────────────────
#  PROBABILITY SCORE
# ─────────────────────────────────────────────
def calc_probability_score(
    ob:          dict,
    rsi:         float,
    candles:     list[dict],
    trend:       str,
    retest_weak: bool,
) -> float:
    """
    Score 0–100:
      RSI confluence     20 pts
      OB age             20 pts
      Volume strength    20 pts
      Body quality       15 pts
      Trend alignment    15 pts
      Retest volume weak 10 pts
    """
    score  = 0.0
    is_buy = ob["type"] == "BUY"

    # ── RSI (20 pts) ──────────────────────────
    if rsi >= 0:
        if is_buy:
            if 25 <= rsi <= 45:
                score += 20
            elif 45 < rsi <= 55:
                score += 12
            elif rsi < 25:
                score += 8
            else:
                score += 2
        else:
            if 55 <= rsi <= 75:
                score += 20
            elif 45 <= rsi < 55:
                score += 12
            elif rsi > 75:
                score += 8
            else:
                score += 2

    # ── OB Age (20 pts) ───────────────────────
    age_h = (time.time() - ob["time"]) / 3600
    if age_h <= 6:
        score += 20
    elif age_h <= 12:
        score += 16
    elif age_h <= 24:
        score += 10
    elif age_h <= 36:
        score += 5
    else:
        score += 2

    # ── Volume strength (20 pts) ──────────────
    avg_vol   = average_volume(candles)
    ob_candle = next((c for c in candles if c["time"] == ob["time"]), None)
    if ob_candle and avg_vol > 0:
        vol_ratio = ob_candle["vol"] / avg_vol
        if vol_ratio >= 3.0:
            score += 20
        elif vol_ratio >= 2.0:
            score += 15
        elif vol_ratio >= 1.5:
            score += 10
        else:
            score += 5

    # ── Body quality (15 pts) ─────────────────
    if ob_candle:
        ob_range = ob_candle["high"] - ob_candle["low"]
        if ob_range > 0:
            body_ratio = abs(ob_candle["close"] - ob_candle["open"]) / ob_range
            if body_ratio >= 0.8:
                score += 15
            elif body_ratio >= 0.65:
                score += 11
            elif body_ratio >= 0.5:
                score += 7
            else:
                score += 3

    # ── Trend alignment (15 pts) ──────────────
    ob_needs = "BULLISH" if is_buy else "BEARISH"
    if trend == ob_needs:
        score += 15
    elif trend == "NEUTRAL":
        score += 7
    else:
        score += 0

    # ── Retest volume weak (10 pts) ───────────
    if retest_weak:
        score += 10

    return round(min(score, 100.0), 1)

# ─────────────────────────────────────────────
#  10x LEVERAGE PROFIT CALC
# ─────────────────────────────────────────────
def calc_10x_profit(entry: float, tp: float, sl: float, is_buy: bool) -> dict:
    leverage = 10
    if is_buy:
        profit_pct = ((tp - entry) / entry) * 100 * leverage
        loss_pct   = ((entry - sl) / entry) * 100 * leverage
    else:
        profit_pct = ((entry - tp) / entry) * 100 * leverage
        loss_pct   = ((sl - entry) / entry) * 100 * leverage
    return {
        "profit": round(profit_pct, 1),
        "loss":   round(loss_pct, 1),
    }

# ─────────────────────────────────────────────
#  BREAKOUT PROBABILITY
# ─────────────────────────────────────────────
def calc_breakout_probability(candles: list[dict], perc: float = 1.0) -> dict:
    if len(candles) < 20:
        return {
            "bias": "NEUTRAL", "prob_up": 0.0, "prob_down": 0.0,
            "prob_up_l1": 0.0, "prob_dn_l1": 0.0,
            "total_green": 0, "total_red": 0,
        }

    total_green = 0
    total_red   = 0
    counts      = [[0, 0, 0, 0] for _ in range(3)]

    for i in range(len(candles) - 1):
        cur      = candles[i]
        nxt      = candles[i + 1]
        step     = cur["close"] * (perc / 100)
        is_green = cur["close"] > cur["open"]
        is_red   = cur["close"] < cur["open"]

        if is_green:
            total_green += 1
        elif is_red:
            total_red += 1
        else:
            continue

        for lvl in range(3):
            x  = step * lvl
            hh = nxt["high"] >= cur["high"] + x
            ll = nxt["low"]  <= cur["low"]  - x
            if is_green:
                if hh: counts[lvl][0] += 1
                if ll: counts[lvl][1] += 1
            else:
                if hh: counts[lvl][2] += 1
                if ll: counts[lvl][3] += 1

    last       = candles[-1]
    last_green = last["close"] > last["open"]

    def pct(num, denom):
        return round((num / denom) * 100, 2) if denom > 0 else 0.0

    if last_green:
        prob_up    = pct(counts[0][0], total_green)
        prob_down  = pct(counts[0][1], total_green)
        prob_up_l1 = pct(counts[1][0], total_green)
        prob_dn_l1 = pct(counts[1][1], total_green)
    else:
        prob_up    = pct(counts[0][2], total_red)
        prob_down  = pct(counts[0][3], total_red)
        prob_up_l1 = pct(counts[1][2], total_red)
        prob_dn_l1 = pct(counts[1][3], total_red)

    bias = "BULLISH" if prob_up >= prob_down else "BEARISH"
    return {
        "bias": bias, "prob_up": prob_up, "prob_down": prob_down,
        "prob_up_l1": prob_up_l1, "prob_dn_l1": prob_dn_l1,
        "total_green": total_green, "total_red": total_red,
        "last_green": last_green,
    }

# ─────────────────────────────────────────────
#  ALERT MESSAGE
# ─────────────────────────────────────────────
def make_alert(
    symbol:      str,
    ob:          dict,
    price:       float,
    rsi:         float,
    candles:     list[dict],
    bp:          dict,
    trend:       str,
    retest_weak: bool,
) -> str:
    coin     = symbol.replace("_USDT", "")
    is_buy   = ob["type"] == "BUY"
    signal   = "LONG 📈" if is_buy else "SHORT 📉"
    tv_link  = make_tradingview_link(symbol)
    sl, tp   = suggest_sl_tp(ob, price)
    prob     = calc_probability_score(ob, rsi, candles, trend, retest_weak)
    leverage = calc_10x_profit(price, tp, sl, is_buy)

    return (
        f"<b>#{coin}/USDT — {signal}</b>\n"
        f"💰 Entry: {fmt_price(ob['bottom'])} — {fmt_price(ob['top'])}\n"
        f"🎯 Prob : {prob}%\n"
        f"🛡 SL   : {fmt_price(sl)}\n"
        f"🎯 TP   : {fmt_price(tp)}\n"
        f"✅ TP hit: <b>+{leverage['profit']}%</b>  |  ❌ SL hit: <b>-{leverage['loss']}%</b>\n"
        f'<a href="{tv_link}">Chart ↗️</a>'
    )

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    print("=" * 60)
    print("  MEXC 1H Order Block Scanner  |  Top 500 Coins  (v5.1)")
    print(f"  Volume Filter    : >5M USDT 24h")
    print(f"  OB Vol Filter    : {VOLUME_MULTIPLIER}x avg candle volume")
    print(f"  Body Filter      : ≥{int(MIN_BODY_RATIO*100)}% body/range ratio")
    print(f"  Min OB Width     : ≥{MIN_OB_RANGE_PCT}% of price")
    print(f"  Mitigation       : price must move {MITIGATION_MOVE_PCT}% AWAY then return")
    print(f"  Touch Filter     : candle BODY must close inside OB")
    print(f"  Opposing OB Blk  : Block if FRESH (unmitigated) opposing OB within {OPPOSING_OB_BLOCK_PCT}%")
    print(f"  RSI Filter       : BUY ≤{RSI_BUY_MAX} | SELL ≥{RSI_SELL_MIN}")
    print(f"  EMA Trend Filter : EMA{EMA_FAST}/EMA{EMA_SLOW} alignment required")
    print(f"  Retest Volume    : Retest vol must be ≤{int(RETEST_VOL_RATIO_MAX*100)}% of OB vol")
    print(f"  Min Prob Score   : ≥{MIN_PROB_SCORE}%")
    print(f"  OB Age Filter    : Last {OB_MAX_AGE_HOURS}h only")
    print(f"  Alert Cooldown   : 1h per coin")
    print(f"  ✅ FIX: Candles sorted oldest→newest (prevents reversed signals)")
    print(f"  ✅ FIX: Opposing OB check uses FRESH OBs (not mitigated ones)")
    print("=" * 60)

    send_telegram(
        "🚀 <b>Order Block Scanner LIVE (v5.1 — FIXED)</b>\n\n"
        "🔧 <b>Critical fixes in v5.1:</b>\n"
        "  ✅ Candles now sorted oldest→newest (prevents flipped signals)\n"
        "  ✅ Opposing OB check fixed — now blocks FRESH OBs in path\n"
        "     (mitigated OBs no longer block valid signals)\n\n"
        "📊 Scanning: Top 500 MEXC Futures | 1H TF\n"
        "🎯 Stricter = fewer but higher quality alerts"
    )

    symbols: list[str] = []
    last_symbol_refresh = 0

    while True:
        try:
            now = time.time()

            if now - last_symbol_refresh > 7200:
                symbols             = get_top_symbols(500)
                last_symbol_refresh = now

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"\n[{ts} UTC] Scanning {len(symbols)} coins...")
            price_map   = get_price_map()
            alerts_sent = 0

            for idx, symbol in enumerate(symbols):
                try:
                    price = price_map.get(symbol)
                    if not price:
                        continue

                    coin = symbol.replace("_USDT", "")

                    allowed, reason = can_alert(coin, now)
                    if not allowed:
                        continue

                    candles = get_candles(symbol, 150)  # ✅ already sorted inside
                    if not candles or len(candles) < 30:
                        continue

                    obs         = detect_order_blocks(candles)
                    rsi         = calculate_rsi(candles)
                    bp          = calc_breakout_probability(candles)
                    trend       = get_trend(candles)
                    last_candle = candles[-1]   # newest candle (after sort)

                    for ob in obs:
                        ob_key = f"{symbol}_{ob['type']}_{ob['time']}"

                        if ob_key in TOUCHED_ALERTS:
                            if now - TOUCHED_ALERTS[ob_key] < ALERT_COOLDOWN:
                                continue

                        # ── 1. Mitigation check ──────────────────────────
                        if not was_mitigated(ob, candles):
                            continue

                        # ── 2. Touch check (body close inside OB) ────────
                        if not is_touching(price, ob, last_candle):
                            continue

                        # ── 3. RSI filter ────────────────────────────────
                        if rsi >= 0:
                            if ob["type"] == "BUY"  and rsi > RSI_BUY_MAX:
                                print(f"  ⚠ {coin} BUY skipped — RSI {rsi} > {RSI_BUY_MAX}")
                                continue
                            if ob["type"] == "SELL" and rsi < RSI_SELL_MIN:
                                print(f"  ⚠ {coin} SELL skipped — RSI {rsi} < {RSI_SELL_MIN}")
                                continue

                        # ── 4. Opposing OB block check (FIXED) ───────────
                        blocked, blocker_price = has_opposing_ob_in_path(
                            ob["type"], price, obs, candles
                        )
                        if blocked:
                            print(f"  🚫 {coin} {ob['type']} BLOCKED — fresh opposing OB at {fmt_price(blocker_price)}")
                            continue

                        # ── 5. Breakout probability alignment ─────────────
                        ob_needs_bias = "BULLISH" if ob["type"] == "BUY" else "BEARISH"
                        if bp["bias"] != "NEUTRAL" and bp["bias"] != ob_needs_bias:
                            print(f"  📉 {coin} {ob['type']} skipped — BP bias {bp['bias']}")
                            continue

                        # ── 6. EMA Trend filter ───────────────────────────
                        ob_needs_trend = "BULLISH" if ob["type"] == "BUY" else "BEARISH"
                        if trend != "NEUTRAL" and trend != ob_needs_trend:
                            print(f"  📊 {coin} {ob['type']} skipped — EMA trend is {trend}")
                            continue

                        # ── 7. Retest volume check ────────────────────────
                        retest_weak = is_retest_volume_weak(ob, candles, price)

                        # ── 8. Minimum probability score ──────────────────
                        prob = calc_probability_score(ob, rsi, candles, trend, retest_weak)
                        if prob < MIN_PROB_SCORE:
                            print(f"  📉 {coin} {ob['type']} skipped — prob score {prob}% < {MIN_PROB_SCORE}%")
                            continue

                        # ── All checks passed → send alert ────────────────
                        print(
                            f"  ⚡ {coin} | {ob['type']} OB @ {fmt_price(price)} "
                            f"| RSI {rsi} | Trend {trend} | Prob {prob}%"
                        )
                        if send_telegram(make_alert(symbol, ob, price, rsi, candles, bp, trend, retest_weak)):
                            record_alert(coin, ob_key, now)
                            alerts_sent += 1
                            daily = DAILY_ALERT_COUNT[coin]["count"]
                            print(f"     ✅ Alert sent! [{daily}/{MAX_ALERTS_PER_DAY} today]")
                        time.sleep(1.5)
                        break

                    time.sleep(0.35)

                    if (idx + 1) % 100 == 0:
                        print(f"  ... {idx+1}/{len(symbols)} scanned")

                except Exception as e:
                    print(f"  [ERR] {symbol}: {e}")
                    continue

            print(f"[DONE] Alerts sent: {alerts_sent} | Next scan in {CHECK_INTERVAL}s")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopped by user.")
            send_telegram("⛔ Scanner stopped.")
            break
        except Exception as e:
            print(f"[MAIN ERROR] {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
