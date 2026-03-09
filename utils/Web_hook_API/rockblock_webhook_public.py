#!/usr/bin/env python3
"""
RockBLOCK MO webhook with public URL via pyngrok.

Starts a Flask server, opens an ngrok tunnel, and prints the public URL
to paste into Rock7 CORE.

Install deps once:
    pip install flask waitress pyngrok

Set NGROK_AUTHTOKEN env var (free token from https://dashboard.ngrok.com).
"""

import os
import sys
from threading import Thread
from flask import Flask, jsonify

from utils.Web_hook_API.rb_webhook_base import extract_params, write_txt_block, health_response

DEFAULT_TXT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rockblock_messages.txt")
TXT_PATH        = os.getenv("RB_TXT_PATH", DEFAULT_TXT)
HOST            = os.getenv("RB_HOST", "127.0.0.1")
PORT            = int(os.getenv("RB_PORT", "8080"))
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "").strip()

app = Flask(__name__)


@app.post("/webhook")
def webhook():
    try:
        write_txt_block(TXT_PATH, extract_params())
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.get("/healthz")
def health():
    return health_response()


def _start_server():
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT)
    except ImportError:
        app.run(host=HOST, port=PORT)


def main():
    print(f"→ Writing TXT to: {os.path.abspath(TXT_PATH)}")
    Thread(target=_start_server, daemon=True).start()

    try:
        from pyngrok import ngrok, conf
        if NGROK_AUTHTOKEN:
            conf.get_default().auth_token = NGROK_AUTHTOKEN
        tunnel     = ngrok.connect(addr=PORT, proto="http", bind_tls=True)
        public_url = tunnel.public_url
        print(f"→ Public webhook URL: {public_url}/webhook")
        print(f"→ Health check      : {public_url}/healthz")
        print("   Keep this running. Ctrl+C to stop.")
    except Exception as e:
        print(f"! Could not start ngrok tunnel: {e}")
        print(f"→ Local only: http://{HOST}:{PORT}/webhook")

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
