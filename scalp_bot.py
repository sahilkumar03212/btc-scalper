"""
BTC/USDT 1-Minute Scalping Bot — Paper Trading on Binance Testnet
Runs every 60 seconds, fetches 1m candles, predicts, and scalps.
"""
import os
import sys
import time
import ccxt
import pandas as pd
import requests
from datetime import datetime, timezone
from pathlib import Path

# Add scalping directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features_1m import build_scalp_features
from predictor_1m import ScalpPredictor
from decision_1m import decide_scalp, SL_PCT, TP_PCT

# ─── Configuration ───────────────────────────────────────────────
TESTNET_API_KEY = os.environ.get("TESTNET_API_KEY", "")
TESTNET_API_SECRET = os.environ.get("TESTNET_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CYCLE_INTERVAL = 60          # Run every 60 seconds
MAX_HOLD_BARS = 5            # 5 minutes max hold for a scalp
POSITION_SIZE_PCT = 0.02     # 2% of balance per trade
MAX_DAILY_LOSS_PCT = 0.02    # 2% max daily loss (circuit breaker)
MAX_CONSECUTIVE_LOSSES = 10  # Pause after 10 losses in a row


# ─── Telegram Notifier ───────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[WARN] Telegram send failed: {resp.text}")
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")


# ─── Exchange Setup ──────────────────────────────────────────────
def get_exchange():
    return ccxt.binance({
        "apiKey": TESTNET_API_KEY,
        "secret": TESTNET_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
        "urls": {
            "api": {
                "public": "https://testnet.binance.vision/api",
                "private": "https://testnet.binance.vision/api",
            }
        },
    })


def get_current_price(exchange):
    ticker = exchange.fetch_ticker("BTC/USDT")
    return float(ticker["last"])


def fetch_1m_candles(exchange, limit=100):
    ohlcv = exchange.fetch_ohlcv("BTC/USDT", timeframe="1m", limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


def get_balance(exchange):
    bal = exchange.fetch_balance()
    return float(bal.get("USDT", {}).get("free", 0))


# ─── Position Tracker ────────────────────────────────────────────
class ScalpPosition:
    def __init__(self):
        self.is_open = False
        self.side = None
        self.entry_price = 0.0
        self.size_usd = 0.0
        self.btc_qty = 0.0
        self.stop_loss = 0.0
        self.take_profit = 0.0
        self.bars_held = 0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consecutive_losses = 0
        self.last_reset_date = None

    def reset_daily_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.consecutive_losses = 0
            self.last_reset_date = today

    def open(self, side, entry_price, size_usd, btc_qty, stop_loss, take_profit):
        self.is_open = True
        self.side = side
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.btc_qty = btc_qty
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.bars_held = 0

    def close(self, exit_price, exit_reason=""):
        pnl_pct = (exit_price - self.entry_price) / self.entry_price * 100
        pnl_usd = pnl_pct / 100 * self.size_usd
        self.daily_pnl += pnl_usd
        self.daily_trades += 1

        if pnl_usd < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        result = {
            "entry": self.entry_price,
            "exit": exit_price,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usd": round(pnl_usd, 4),
            "bars_held": self.bars_held,
            "reason": exit_reason
        }
        
        # Log to CSV
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)
        csv_file = log_dir / "trade_history.csv"
        
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        is_new_file = not csv_file.exists()
        
        try:
            with open(csv_file, "a") as f:
                if is_new_file:
                    f.write("Timestamp,Side,Entry,Exit,PnL_Pct,PnL_USD,Bars_Held,Reason\n")
                f.write(f"{timestamp},{self.side},{self.entry_price},{exit_price},{result['pnl_pct']},{result['pnl_usd']},{self.bars_held},{exit_reason}\n")
        except Exception as e:
            print(f"[WARN] Failed to write to CSV log: {e}")

        self.is_open = False
        self.entry_price = 0.0
        self.size_usd = 0.0
        self.btc_qty = 0.0
        self.bars_held = 0
        return result

    def check_exit(self, current_price):
        if not self.is_open:
            return None
        self.bars_held += 1
        if current_price <= self.stop_loss:
            return "stop_loss"
        if current_price >= self.take_profit:
            return "take_profit"
        if self.bars_held >= MAX_HOLD_BARS:
            return "max_hold"
        return None


# ─── Main Bot Loop ───────────────────────────────────────────────
def main():
    print("=" * 60)
    print("BTC/USDT 1-Minute Scalping Bot")
    print("Mode: PAPER TRADING (Testnet)")
    print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Cycle: Every {CYCLE_INTERVAL}s | Max Hold: {MAX_HOLD_BARS} bars")
    print(f"SL: {SL_PCT*100:.2f}% | TP: {TP_PCT*100:.2f}%")
    print("=" * 60)

    exchange = get_exchange()
    predictor = ScalpPredictor()
    pos = ScalpPosition()

    send_telegram(
        f"🏎️ <b>Scalping Bot Started</b>\n"
        f"Mode: Paper Trading\n"
        f"Cycle: Every {CYCLE_INTERVAL}s\n"
        f"SL: {SL_PCT*100:.2f}% | TP: {TP_PCT*100:.2f}%\n"
        f"Max Hold: {MAX_HOLD_BARS} bars (minutes)\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    while True:
        try:
            now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
            pos.reset_daily_if_needed()

            # ── Circuit Breaker ──
            balance = get_balance(exchange)
            if balance > 0 and pos.daily_pnl < 0 and abs(pos.daily_pnl) > balance * MAX_DAILY_LOSS_PCT:
                print(f"[{now_utc}] 🛑 CIRCUIT BREAKER — Daily loss ${pos.daily_pnl:.2f} exceeds {MAX_DAILY_LOSS_PCT*100}%")
                send_telegram(f"🛑 <b>Circuit Breaker Active</b>\nDaily loss: ${pos.daily_pnl:.2f}\nBot paused until midnight UTC.")
                time.sleep(3600)
                continue

            if pos.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                print(f"[{now_utc}] 🛑 {MAX_CONSECUTIVE_LOSSES} consecutive losses — cooling down 1 hour")
                send_telegram(f"🛑 <b>Cooling Down</b>\n{MAX_CONSECUTIVE_LOSSES} consecutive losses. Pausing 1 hour.")
                pos.consecutive_losses = 0
                time.sleep(3600)
                continue

            # ── Fetch Data ──
            price = get_current_price(exchange)
            candles = fetch_1m_candles(exchange, limit=100)
            features = build_scalp_features(candles)
            latest = features.iloc[-1]

            # Drop rows with NaN (needed for RSI warmup)
            if latest.isna().any():
                print(f"[{now_utc}] ⏳ Warming up features... (NaN detected)")
                time.sleep(CYCLE_INTERVAL)
                continue

            # ── Check Exit Conditions ──
            if pos.is_open:
                exit_reason = pos.check_exit(price)
                if exit_reason:
                    result = pos.close(price, exit_reason)
                    emoji = "💰" if result["pnl_usd"] > 0 else "💸"
                    print(f"[{now_utc}] {emoji} CLOSED SCALP | {exit_reason} | PnL: {result['pnl_pct']:+.4f}% (${result['pnl_usd']:+.4f})")

                    send_telegram(
                        f"{emoji} <b>Scalp Closed</b> ({exit_reason})\n"
                        f"Entry: ${result['entry']:,.2f} → Exit: ${result['exit']:,.2f}\n"
                        f"PnL: {result['pnl_pct']:+.4f}% (${result['pnl_usd']:+.4f})\n"
                        f"Held: {result['bars_held']} min | Daily PnL: ${pos.daily_pnl:+.2f} ({pos.daily_trades} trades)"
                    )

            # ── Predict & Decide ──
            p_up = predictor.predict(features)
            decision = decide_scalp(p_up, price, has_open_position=pos.is_open)

            print(f"[{now_utc}] BTC: ${price:,.2f} | P(up): {p_up:.3f} | {decision['action']} — {decision['reason']}")

            # ── Execute Trade ──
            if decision["action"] == "BUY":
                size_usd = balance * POSITION_SIZE_PCT
                btc_qty = round(size_usd / price, 5)

                try:
                    order = exchange.create_market_buy_order("BTC/USDT", btc_qty)
                    fill_price = float(order.get("average", price))
                    sl = round(fill_price * (1 - SL_PCT), 2)
                    tp = round(fill_price * (1 + TP_PCT), 2)

                    pos.open("long", fill_price, size_usd, btc_qty, sl, tp)
                    print(f"[{now_utc}] 🟢 SCALP BUY {btc_qty} BTC @ ${fill_price:,.2f} | SL: ${sl:,.2f} | TP: ${tp:,.2f}")

                    send_telegram(
                        f"🟢 <b>SCALP BUY</b> @ ${fill_price:,.2f}\n"
                        f"Size: ${size_usd:.2f} ({btc_qty} BTC)\n"
                        f"SL: ${sl:,.2f} ({SL_PCT*100:.2f}%) | TP: ${tp:,.2f} ({TP_PCT*100:.2f}%)\n"
                        f"P(up): {p_up:.3f}"
                    )
                except Exception as e:
                    print(f"[{now_utc}] ❌ Order failed: {e}")
                    send_telegram(f"⚠️ <b>Order Failed</b>\n{e}")

        except Exception as e:
            print(f"[ERROR] {e}")
            send_telegram(f"⚠️ <b>Scalp Bot Error</b>\n{e}")

        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    main()
