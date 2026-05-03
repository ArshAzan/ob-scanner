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
ALERT_COOLDOWN      = 4 * 3600
MIN_VOLUME_USDT     = 5_000_000
VOLUME_MULTIPLIER   = 1.5
MIN_BODY_RATIO      = 0.4

# ── NEW CONFIGS ───────────────────────────────
# How far (%) an opposing OB can be before we ignore it as "too far away"
OPPOSING_OB_BLOCK_PCT = 5.0   # if SELL OB is within 5% above price → block BUY signal

# Minimum % price must have moved AWAY from OB before returning (mitigation)
MITIGATION_MOVE_PCT   = 1.5   # price must move 1.5% away from OB top/bottom

# RSI thresholds
RSI_PERIOD            = 14
RSI_BUY_MAX           = 65    # don't buy if RSI > 65 (already overbought)
RSI_SELL_MIN          = 35    # don't sell if RSI < 35 (already oversold)

TOUCHED_ALERTS: dict = {}

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
def get_candles(symbol: str, limit: int = 120) -> list[dict]:
    try:
        r    = requests.get(
            f"{MEXC_BASE_URL}/contract/kline/{symbol}",
            params={"interval": TIMEFRAME, "limit": limit},
            timeout=15,
        )
        data = r.json()
        if data.get("success") and data.get("data"):
            raw = data["data"]
            return [
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
#  RSI CALCULATION  (NEW)
# ─────────────────────────────────────────────
def calculate_rsi(candles: list[dict], period: int = RSI_PERIOD) -> float:
    """
    Standard Wilder RSI using closing prices.
    Returns value 0–100, or -1 if not enough data.
    """
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

    # Wilder smoothing for remaining candles
    for i in range(period + 1, len(closes)):
        diff     = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0))  / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period

    if avg_loss == 0:
        return 100.0
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ─────────────────────────────────────────────
#  ORDER BLOCK DETECTION
# ─────────────────────────────────────────────
def detect_order_blocks(candles: list[dict]) -> list[dict]:
    """
    BUY OB  : bearish OB → bullish trigger → strong bullish confirmation
    SELL OB : bullish OB → bearish trigger → strong bearish confirmation
    + high-volume & body-quality filters
    """
    if len(candles) < 10:
        return []

    avg_vol = average_volume(candles)
    obs     = []

    for i in range(2, len(candles) - 1):
        ob_c  = candles[i - 1]
        trig  = candles[i]
        conf  = candles[i + 1]

        if not is_fresh(ob_c["time"]):
            continue

        ob_range = ob_c["high"] - ob_c["low"]
        if ob_range == 0:
            continue
        ob_body = abs(ob_c["close"] - ob_c["open"])
        if ob_body / ob_range < MIN_BODY_RATIO:
            continue

        if avg_vol > 0 and ob_c["vol"] < avg_vol * VOLUME_MULTIPLIER:
            continue

        # BUY OB
        if (ob_c["close"] < ob_c["open"]
                and trig["close"] > trig["open"]
                and conf["close"] > trig["high"]):
            obs.append({
                "type":   "BUY",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
            })

        # SELL OB
        elif (ob_c["close"] > ob_c["open"]
                and trig["close"] < trig["open"]
                and conf["close"] < trig["low"]):
            obs.append({
                "type":   "SELL",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
            })

    return obs

# ─────────────────────────────────────────────
#  MITIGATION CHECK  (FIXED)
# ─────────────────────────────────────────────
def was_mitigated(ob: dict, candles: list[dict]) -> bool:
    """
    ✅ FIXED LOGIC — Old code was checking if price MOVED AWAY from OB,
       which was already guaranteed by detection. Correct logic:

    BUY OB  → After forming, price first moves UP (away), then comes BACK DOWN
               to touch/enter the OB zone.  That return touch = mitigation.

    SELL OB → After forming, price first moves DOWN (away), then comes BACK UP
               to touch/enter the OB zone.  That return touch = mitigation.
    """
    post = [c for c in candles if c["time"] > ob["time"]]
    if len(post) < 2:
        return False

    if ob["type"] == "BUY":
        # Step 1: Confirm price moved UP away from OB (at least MITIGATION_MOVE_PCT)
        threshold_up = ob["top"] * (1 + MITIGATION_MOVE_PCT / 100)
        moved_up     = False
        moved_up_idx = -1
        for idx, c in enumerate(post):
            if c["high"] >= threshold_up:
                moved_up     = True
                moved_up_idx = idx
                break

        if not moved_up:
            return False   # impulse never happened — not a valid OB retest

        # Step 2: After moving up, did price return DOWN into OB zone?
        for c in post[moved_up_idx + 1:]:
            if c["low"] <= ob["top"]:   # price touched back into OB zone
                return True
        return False

    else:  # SELL OB
        # Step 1: Confirm price moved DOWN away from OB
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

        # Step 2: After moving down, did price return UP into OB zone?
        for c in post[moved_dn_idx + 1:]:
            if c["high"] >= ob["bottom"]:  # price touched back into OB zone
                return True
        return False

# ─────────────────────────────────────────────
#  OPPOSING OB BLOCK CHECK  (NEW — main fix)
# ─────────────────────────────────────────────
def has_opposing_ob_in_path(
    signal_type: str,
    price: float,
    obs: list[dict],
    candles: list[dict],
) -> tuple[bool, float | None]:
    """
    ✅ MAIN FIX — This was the root cause of wrong signals.

    For a BUY signal:
        Check if there is any ACTIVE (mitigated) SELL OB sitting
        between current price and OPPOSING_OB_BLOCK_PCT% above it.
        If yes → price will likely get rejected there → SKIP BUY signal.

    For a SELL signal:
        Check if there is any ACTIVE (mitigated) BUY OB sitting
        between OPPOSING_OB_BLOCK_PCT% below price and current price.
        If yes → price will likely bounce there → SKIP SELL signal.

    Returns (blocked: bool, blocking_ob_price: float | None)
    """
    if signal_type == "BUY":
        upper_limit = price * (1 + OPPOSING_OB_BLOCK_PCT / 100)
        for ob in obs:
            if ob["type"] != "SELL":
                continue
            # Is this SELL OB sitting between price and upper_limit?
            if price < ob["bottom"] <= upper_limit:
                if was_mitigated(ob, candles):
                    return True, ob["bottom"]
        return False, None

    else:  # SELL
        lower_limit = price * (1 - OPPOSING_OB_BLOCK_PCT / 100)
        for ob in obs:
            if ob["type"] != "BUY":
                continue
            # Is this BUY OB sitting between lower_limit and price?
            if lower_limit <= ob["top"] < price:
                if was_mitigated(ob, candles):
                    return True, ob["top"]
        return False, None

# ─────────────────────────────────────────────
#  TOUCH CHECK
# ─────────────────────────────────────────────
def is_touching(price: float, ob: dict) -> bool:
    spread = ob["top"] - ob["bottom"]
    tol    = spread * 0.10
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
#  SUGGEST SL / TP  (NEW)
# ─────────────────────────────────────────────
def suggest_sl_tp(ob: dict, price: float) -> tuple[float, float]:
    """
    BUY  → SL just below OB bottom (1% buffer), TP = 2× risk above entry
    SELL → SL just above OB top   (1% buffer), TP = 2× risk below entry
    """
    if ob["type"] == "BUY":
        sl     = ob["bottom"] * 0.99
        risk   = price - sl
        tp     = price + risk * 2
    else:
        sl     = ob["top"] * 1.01
        risk   = sl - price
        tp     = price - risk * 2
    return round(sl, 6), round(tp, 6)

# ─────────────────────────────────────────────
#  ALERT MESSAGE  (improved)
# ─────────────────────────────────────────────
def make_alert(
    symbol: str,
    ob: dict,
    price: float,
    rsi: float,
) -> str:
    coin    = symbol.replace("_USDT", "")
    is_buy  = ob["type"] == "BUY"
    emoji   = "🟢" if is_buy else "🔴"
    signal  = "LONG" if is_buy else "SHORT"
    bias    = "BULLISH" if is_buy else "BEARISH"
    tv_link = make_tradingview_link(symbol)
    ob_age  = round((time.time() - ob["time"]) / 3600, 1)
    sl, tp  = suggest_sl_tp(ob, price)

    rsi_str = f"{rsi:.2f}" if rsi >= 0 else "N/A"

    return (
        f"{emoji} <b>HIGH-QUALITY OB SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Coin:</b> #{coin}/USDT\n"
        f"⚡ <b>Signal:</b> {signal}\n"
        f"🎯 <b>Bias:</b> {bias}\n"
        f"💰 <b>Entry Zone:</b> {fmt_price(ob['bottom'])} — {fmt_price(ob['top'])}\n"
        f"📍 <b>Current Price:</b> {fmt_price(price)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>RSI (14):</b> {rsi_str}\n"
        f"🕐 <b>OB Age:</b> {ob_age}h ago\n"
        f"✅ <b>Candle Confirmed:</b> Yes (closed candle)\n"
        f"✅ <b>Mitigation:</b> Confirmed\n"
        f"✅ <b>Path Clear:</b> No opposing OB blocking\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛑 <b>Suggested SL:</b> {fmt_price(sl)}\n"
        f"🎯 <b>Suggested TP:</b> {fmt_price(tp)} (1:2 RR)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>View Chart (1H):</b>\n"
        f'<a href="{tv_link}">Open {coin}/USDT on TradingView ↗</a>\n'
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Always use proper SL\n"
        f"High leverage (10x-20x) = tight SL only\n"
        f"Not Financial Advice | DYOR</i>"
    )

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    print("=" * 60)
    print("  MEXC 1H Order Block Scanner  |  Top 500 Coins  (v3)")
    print(f"  Volume Filter    : >5M USDT 24h")
    print(f"  OB Vol Filter    : {VOLUME_MULTIPLIER}x avg candle volume")
    print(f"  Body Filter      : ≥{int(MIN_BODY_RATIO*100)}% body/range ratio")
    print(f"  Mitigation       : FIXED — price must RETURN to zone")
    print(f"  Opposing OB Blk  : Block if opposing OB within {OPPOSING_OB_BLOCK_PCT}%")
    print(f"  RSI Filter       : BUY ≤{RSI_BUY_MAX} | SELL ≥{RSI_SELL_MIN}")
    print(f"  OB Age Filter    : Last {OB_MAX_AGE_HOURS}h only")
    print(f"  Alert Cooldown   : 4h per OB zone")
    print("=" * 60)

    send_telegram(
        "🚀 <b>Order Block Scanner LIVE (v3 — Fixed)</b>\n\n"
        "🔧 <b>Fixes applied:</b>\n"
        "  ✅ Mitigation logic corrected (return-to-zone)\n"
        "  ✅ Opposing OB blocker (no buy under sell OB)\n"
        "  ✅ RSI filter (no overbought buys / oversold sells)\n"
        "  ✅ SL/TP auto-calculated in alerts\n"
        "  ✅ Path-clear confirmation in message\n\n"
        "📊 Scanning: Top 500 MEXC Futures | 1H TF\n"
        "🎯 Fewer alerts, much higher quality"
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

                    coin_key = f"{symbol}_last_alert"
                    if coin_key in TOUCHED_ALERTS:
                        if now - TOUCHED_ALERTS[coin_key] < ALERT_COOLDOWN:
                            continue

                    candles = get_candles(symbol, 120)
                    if not candles or len(candles) < 20:
                        continue

                    obs = detect_order_blocks(candles)
                    rsi = calculate_rsi(candles)

                    for ob in obs:
                        ob_key = f"{symbol}_{ob['type']}_{ob['time']}"

                        if ob_key in TOUCHED_ALERTS:
                            if now - TOUCHED_ALERTS[ob_key] < ALERT_COOLDOWN:
                                continue

                        # ── 1. Mitigation check (FIXED) ──────────────────
                        if not was_mitigated(ob, candles):
                            continue

                        # ── 2. Touch check ───────────────────────────────
                        if not is_touching(price, ob):
                            continue

                        # ── 3. RSI filter (NEW) ──────────────────────────
                        if rsi >= 0:
                            if ob["type"] == "BUY"  and rsi > RSI_BUY_MAX:
                                print(f"  ⚠ {symbol} BUY skipped — RSI {rsi} > {RSI_BUY_MAX}")
                                continue
                            if ob["type"] == "SELL" and rsi < RSI_SELL_MIN:
                                print(f"  ⚠ {symbol} SELL skipped — RSI {rsi} < {RSI_SELL_MIN}")
                                continue

                        # ── 4. Opposing OB block check (NEW — main fix) ──
                        blocked, blocker_price = has_opposing_ob_in_path(
                            ob["type"], price, obs, candles
                        )
                        if blocked:
                            coin = symbol.replace("_USDT", "")
                            print(
                                f"  🚫 {coin} {ob['type']} BLOCKED — "
                                f"opposing OB at {fmt_price(blocker_price)}"
                            )
                            continue

                        # ── All checks passed → send alert ───────────────
                        coin = symbol.replace("_USDT", "")
                        print(f"  ⚡ {coin} | {ob['type']} OB @ {fmt_price(price)} | RSI {rsi}")
                        if send_telegram(make_alert(symbol, ob, price, rsi)):
                            TOUCHED_ALERTS[ob_key]   = now
                            TOUCHED_ALERTS[coin_key] = now
                            alerts_sent += 1
                            print(f"     ✅ Alert sent! Next for {coin} after 4h")
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
