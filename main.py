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
CHECK_INTERVAL      = 60          # seconds between full scans
OB_MAX_AGE_HOURS    = 48          # ignore OBs older than this
ALERT_COOLDOWN      = 4 * 3600    # 4h per OB zone
MIN_VOLUME_USDT     = 5_000_000   # 24h volume filter for coin selection
VOLUME_MULTIPLIER   = 1.5         # OB candle must be 1.5x avg volume
MIN_BODY_RATIO      = 0.4         # OB candle body must be 40%+ of its range
MITIGATION_RATIO    = 0.5         # price must have moved 50%+ away from OB before returning

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
#  ORDER BLOCK DETECTION  (improved)
# ─────────────────────────────────────────────
def detect_order_blocks(candles: list[dict]) -> list[dict]:
    """
    Rules for a valid OB candle (index i-1 = OB, i = trigger, i+1 = confirmation):

    BUY OB:
      - OB candle is bearish (close < open)
      - Trigger candle is bullish (close > open)
      - Next candle closes above trigger high  →  strong continuation
      - OB candle body ≥ MIN_BODY_RATIO of its range  (no dojis)
      - OB candle volume ≥ VOLUME_MULTIPLIER × rolling avg  (high-volume)

    SELL OB:
      - OB candle is bullish (close > open)
      - Trigger candle is bearish (close < open)
      - Next candle closes below trigger low  →  strong continuation
      - same body & volume filters
    """
    if len(candles) < 10:
        return []

    avg_vol = average_volume(candles)
    obs     = []

    for i in range(2, len(candles) - 1):
        ob_c  = candles[i - 1]   # candidate OB candle
        trig  = candles[i]       # trigger (impulse) candle
        conf  = candles[i + 1]   # confirmation candle

        if not is_fresh(ob_c["time"]):
            continue

        # ── body quality filter ──────────────────────────
        ob_range = ob_c["high"] - ob_c["low"]
        if ob_range == 0:
            continue
        ob_body = abs(ob_c["close"] - ob_c["open"])
        if ob_body / ob_range < MIN_BODY_RATIO:
            continue   # doji / small-body candle — skip

        # ── high-volume filter ───────────────────────────
        if avg_vol > 0 and ob_c["vol"] < avg_vol * VOLUME_MULTIPLIER:
            continue   # not a high-volume OB candle

        # ── BUY OB ──────────────────────────────────────
        if (ob_c["close"] < ob_c["open"]          # OB is bearish
                and trig["close"] > trig["open"]   # trigger is bullish
                and conf["close"] > trig["high"]): # strong continuation up
            obs.append({
                "type":   "BUY",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
            })

        # ── SELL OB ─────────────────────────────────────
        elif (ob_c["close"] > ob_c["open"]         # OB is bullish
                and trig["close"] < trig["open"]   # trigger is bearish
                and conf["close"] < trig["low"]):  # strong continuation down
            obs.append({
                "type":   "SELL",
                "top":    ob_c["high"],
                "bottom": ob_c["low"],
                "time":   ob_c["time"],
            })

    return obs

# ─────────────────────────────────────────────
#  MITIGATION CHECK
# ─────────────────────────────────────────────
def was_mitigated(ob: dict, candles: list[dict]) -> bool:
    """
    After the OB formed, did price travel at least MITIGATION_RATIO × OB-size
    away from the zone before returning?

    BUY OB  → price must have gone above ob['top'] + mitigation_distance
    SELL OB → price must have gone below ob['bottom'] - mitigation_distance
    """
    ob_size  = ob["top"] - ob["bottom"]
    min_move = ob_size * MITIGATION_RATIO

    # candles AFTER the OB (time-sorted, OB time is ob["time"])
    post_candles = [c for c in candles if c["time"] > ob["time"]]
    if not post_candles:
        return False

    if ob["type"] == "BUY":
        # price must have reached above ob["top"] + min_move at some point
        threshold = ob["top"] + min_move
        return any(c["high"] >= threshold for c in post_candles)
    else:
        # price must have reached below ob["bottom"] - min_move
        threshold = ob["bottom"] - min_move
        return any(c["low"] <= threshold for c in post_candles)

# ─────────────────────────────────────────────
#  TOUCH CHECK
# ─────────────────────────────────────────────
def is_touching(price: float, ob: dict) -> bool:
    spread = ob["top"] - ob["bottom"]
    tol    = spread * 0.10   # 10 % tolerance inside zone edges
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
#  ALERT MESSAGE
# ─────────────────────────────────────────────
def make_alert(symbol: str, ob: dict, price: float) -> str:
    coin    = symbol.replace("_USDT", "")
    emoji   = "🟢" if ob["type"] == "BUY" else "🔴"
    zone    = "BUY ZONE"  if ob["type"] == "BUY" else "SELL ZONE"
    tv_link = make_tradingview_link(symbol)
    ob_age  = round((time.time() - ob["time"]) / 3600, 1)

    return (
        f"{emoji} <b>ORDER BLOCK ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Coin:</b> #{coin}/USDT\n"
        f"⚡ <b>Signal:</b> {zone} TOUCHED\n"
        f"💰 <b>Current Price:</b> {fmt_price(price)}\n"
        f"📊 <b>OB Zone:</b> {fmt_price(ob['bottom'])} — {fmt_price(ob['top'])}\n"
        f"🕐 <b>OB Age:</b> {ob_age}h ago\n"
        f"✅ <b>Mitigation:</b> Confirmed\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>View Chart (1H):</b>\n"
        f'<a href="{tv_link}">Open {coin}/USDT on TradingView ↗</a>\n'
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>1. Check chart for valid setup\n"
        f"2. Plan Entry, SL &amp; Target\n"
        f"3. Risk manage properly\n\n"
        f"Not Financial Advice | DYOR</i>"
    )

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────
def run():
    print("=" * 55)
    print("  MEXC 1H Order Block Scanner  |  Top 500 Coins")
    print(f"  Volume Filter  : >5M USDT 24h")
    print(f"  OB Vol Filter  : {VOLUME_MULTIPLIER}x avg candle volume")
    print(f"  Body Filter    : ≥{int(MIN_BODY_RATIO*100)}% body/range ratio")
    print(f"  Mitigation     : {int(MITIGATION_RATIO*100)}% move away required")
    print(f"  OB Age Filter  : Last {OB_MAX_AGE_HOURS}h only")
    print(f"  Alert Cooldown : 4h per OB zone")
    print("=" * 55)

    send_telegram(
        "🚀 <b>Order Block Scanner LIVE (v2)</b>\n\n"
        "🔧 <b>New filters active:</b>\n"
        "  ✅ High-volume OBs only (1.5× avg)\n"
        "  ✅ Mitigation check (price must leave zone first)\n"
        "  ✅ Body quality filter (no dojis)\n"
        "  ✅ 4h cooldown per OB zone\n\n"
        "📊 Scanning: Top 500 MEXC Futures | 1H TF\n"
        "🎯 Target: 10–20 quality alerts/day"
    )

    symbols: list[str] = []
    last_symbol_refresh = 0

    while True:
        try:
            now = time.time()

            # refresh symbol list every 2 hours
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

                    # per-coin cooldown (don't even fetch candles if recently alerted)
                    coin_key = f"{symbol}_last_alert"
                    if coin_key in TOUCHED_ALERTS:
                        if now - TOUCHED_ALERTS[coin_key] < ALERT_COOLDOWN:
                            continue

                    candles = get_candles(symbol, 120)
                    if not candles or len(candles) < 15:
                        continue

                    obs = detect_order_blocks(candles)

                    for ob in obs:
                        ob_key = f"{symbol}_{ob['type']}_{ob['time']}"

                        # per-OB cooldown
                        if ob_key in TOUCHED_ALERTS:
                            if now - TOUCHED_ALERTS[ob_key] < ALERT_COOLDOWN:
                                continue

                        # ── NEW: mitigation check ──
                        if not was_mitigated(ob, candles):
                            continue   # price never left the zone — skip

                        # ── touch check ──
                        if is_touching(price, ob):
                            coin = symbol.replace("_USDT", "")
                            print(f"  ⚡ {coin} | {ob['type']} OB @ {fmt_price(price)}")
                            if send_telegram(make_alert(symbol, ob, price)):
                                TOUCHED_ALERTS[ob_key]   = now
                                TOUCHED_ALERTS[coin_key] = now
                                alerts_sent += 1
                                print(f"     ✅ Alert sent! Next for {coin} after 4h")
                            time.sleep(1.5)
                            break   # one alert per coin per scan

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
