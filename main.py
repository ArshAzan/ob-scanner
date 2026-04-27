import requests
import time
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = "8618398305:AAHOrELkeYLHTTliL3xnEHe6onZ7AOy-0Os"
TELEGRAM_CHAT_ID   = "-1003930522837"
MEXC_BASE_URL      = "https://contract.mexc.com/api/v1"
TIMEFRAME          = "Min60"
CHECK_INTERVAL     = 60
OB_MAX_AGE_HOURS   = 48
TOUCHED_ALERTS     = {}

def send_telegram(message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[TG ERROR] {e}")
        return False

def get_top_symbols(limit=500):
    try:
        r    = requests.get(f"{MEXC_BASE_URL}/contract/ticker", timeout=20)
        data = r.json()
        if data.get("success"):
            tickers = [t for t in data["data"]
                       if "_USDT" in t.get("symbol", "")
                       and float(t.get("volume24", 0) or 0) > 0]
            tickers.sort(key=lambda x: float(x.get("volume24", 0) or 0), reverse=True)
            syms = [t["symbol"] for t in tickers]
            print(f"[INFO] {len(syms)} symbols found — monitoring top {min(limit, len(syms))}")
            return syms[:limit]
    except Exception as e:
        print(f"[SYMBOL ERROR] {e}")
    return []

def get_candles(symbol, limit=100):
    try:
        r    = requests.get(f"{MEXC_BASE_URL}/contract/kline/{symbol}",
                            params={"interval": TIMEFRAME, "limit": limit}, timeout=15)
        data = r.json()
        if data.get("success") and data.get("data"):
            raw = data["data"]
            return [
                {"time":  int(raw["time"][i]),
                 "open":  float(raw["open"][i]),
                 "high":  float(raw["high"][i]),
                 "low":   float(raw["low"][i]),
                 "close": float(raw["close"][i]),
                 "vol":   float(raw["vol"][i])}
                for i in range(len(raw["time"]))
            ]
    except Exception as e:
        print(f"[CANDLE ERROR] {symbol}: {e}")
    return []

def is_fresh(ts):
    return (time.time() - ts) / 3600 <= OB_MAX_AGE_HOURS

def detect_order_blocks(candles):
    obs = []
    for i in range(2, len(candles) - 1):
        prev, curr, nxt = candles[i-1], candles[i], candles[i+1]
        if not is_fresh(prev["time"]):
            continue
        body        = abs(curr["close"] - curr["open"])
        strong_move = abs(nxt["close"] - curr["close"]) > body * 0.5
        if (prev["close"] < prev["open"] and curr["close"] > curr["open"]
                and strong_move and nxt["close"] > curr["high"]):
            obs.append({"type": "BUY",  "top": prev["high"], "bottom": prev["low"], "time": prev["time"]})
        if (prev["close"] > prev["open"] and curr["close"] < curr["open"]
                and strong_move and nxt["close"] < curr["low"]):
            obs.append({"type": "SELL", "top": prev["high"], "bottom": prev["low"], "time": prev["time"]})
    return obs

def is_touching(price, ob):
    spread = ob["top"] - ob["bottom"]
    tol    = spread * 0.1
    return (ob["bottom"] - tol) <= price <= (ob["top"] + tol)

def get_price_map():
    try:
        r    = requests.get(f"{MEXC_BASE_URL}/contract/ticker", timeout=15)
        data = r.json()
        if data.get("success"):
            return {t["symbol"]: float(t["lastPrice"])
                    for t in data["data"] if t.get("lastPrice")}
    except Exception as e:
        print(f"[PRICE MAP ERROR] {e}")
    return {}

def age_str(ts):
    s = time.time() - ts
    return f"{int(s//3600)}h {int((s%3600)//60)}m ago"

def fmt_price(p):
    dec = 6 if p < 0.01 else 4 if p < 1 else 2
    return f"{p:.{dec}f}"

def make_alert(symbol, ob, price):
    coin  = symbol.replace("_USDT", "")
    emoji = "🟢" if ob["type"] == "BUY" else "🔴"
    zone  = "BUY ZONE" if ob["type"] == "BUY" else "SELL ZONE"
    return (
        f"{emoji} <b>ORDER BLOCK ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Coin:</b> #{coin}/USDT\n"
        f"⚡ <b>Signal:</b> {zone} TOUCHED\n"
        f"💰 <b>Price:</b> {fmt_price(price)}\n"
        f"📊 <b>OB Zone:</b> {fmt_price(ob['bottom'])} — {fmt_price(ob['top'])}\n"
        f"🕐 <b>Timeframe:</b> 1 Hour\n"
        f"⏳ <b>OB Formed:</b> {age_str(ob['time'])}\n"
        f"⏰ <b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Exchange:</b> MEXC Futures"
    )

def run():
    print("=" * 55)
    print("  MEXC 1H Order Block Scanner  |  Top 500 Coins")
    print("  OB Filter : Last 48 Hours")
    print("  Alerts    : @futureforalphapro")
    print("=" * 55)

    send_telegram(
        "🚀 <b>Order Block Scanner LIVE!</b>\n"
        "📊 Monitoring: Top 500 MEXC Futures\n"
        "⏱ Timeframe: 1 Hour\n"
        "🔍 OB Filter: Last 48 hours only\n"
        "✅ Alerts will appear here automatically."
    )

    symbols              = []
    last_symbol_refresh  = 0

    while True:
        try:
            now = time.time()

            if now - last_symbol_refresh > 7200:
                symbols             = get_top_symbols(500)
                last_symbol_refresh = now

            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Scanning {len(symbols)} coins...")
            price_map    = get_price_map()
            alerts_sent  = 0

            for idx, symbol in enumerate(symbols):
                try:
                    price = price_map.get(symbol)
                    if not price:
                        continue

                    candles = get_candles(symbol, 100)
                    if not candles or len(candles) < 15:
                        continue

                    obs = detect_order_blocks(candles)
                    for ob in obs:
                        key = f"{symbol}_{ob['type']}_{ob['time']}"
                        if key in TOUCHED_ALERTS and now - TOUCHED_ALERTS[key] < 14400:
                            continue
                        if is_touching(price, ob):
                            coin = symbol.replace("_USDT","")
                            print(f"  ⚡ {coin} | {ob['type']} OB touched @ {fmt_price(price)}")
                            if send_telegram(make_alert(symbol, ob, price)):
                                TOUCHED_ALERTS[key] = now
                                alerts_sent += 1
                                print(f"     ✅ Alert sent!")
                            time.sleep(1.5)

                    time.sleep(0.35)

                    if (idx + 1) % 100 == 0:
                        print(f"  ... {idx+1}/{len(symbols)} done")

                except Exception as e:
                    print(f"  [ERR] {symbol}: {e}")
                    continue

            print(f"[DONE] Alerts sent: {alerts_sent} | Next scan in {CHECK_INTERVAL}s")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\nStopped.")
            send_telegram("⛔ Scanner stopped.")
            break
        except Exception as e:
            print(f"[MAIN ERROR] {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
