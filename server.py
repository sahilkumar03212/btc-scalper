"""
Web Server Wrapper for Render.com — Scalping Bot
"""
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from flask import Flask, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scalp_bot

app = Flask(__name__)

bot_status = {"state": "starting", "last_error": None, "started_at": None}


def run_bot_with_recovery():
    """Run the scalping bot with automatic restart on crash."""
    import time
    bot_status["started_at"] = datetime.now(timezone.utc).isoformat()

    while True:
        try:
            bot_status["state"] = "running"
            scalp_bot.main()
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            bot_status["state"] = "crashed"
            bot_status["last_error"] = error_msg
            print(f"\n[FATAL] Scalp bot crashed: {error_msg}")
            traceback.print_exc()
            try:
                scalp_bot.send_telegram(f"⚠️ <b>Scalp Bot Crashed!</b>\nRestarting in 30s...\n{error_msg}")
            except Exception:
                pass
            print("[RECOVERY] Restarting in 30 seconds...")
            time.sleep(30)


bot_thread = threading.Thread(target=run_bot_with_recovery, daemon=True)
bot_thread.start()


@app.route('/')
def home():
    return jsonify({
        "status": bot_status["state"],
        "bot": "BTC Scalper (1m)",
        "started_at": bot_status["started_at"],
        "last_error": bot_status["last_error"],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
