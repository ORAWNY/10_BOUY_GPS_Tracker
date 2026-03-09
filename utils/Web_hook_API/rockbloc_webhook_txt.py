#!/usr/bin/env python3
"""
Super-simple RockBLOCK MO webhook → append to TXT.

POST /webhook  — Ground Control calls this
GET  /healthz  — liveness check

Set RB_TXT_PATH env var to change the output file location.
"""

import os
import sys
from flask import Flask, jsonify

from utils.Web_hook_API.rb_webhook_base import extract_params, write_txt_block, health_response

DEFAULT_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rockblock_messages.txt")
TXT_PATH    = os.getenv("RB_TXT_PATH", DEFAULT_TXT)
HOST        = os.getenv("RB_HOST", "127.0.0.1")
PORT        = int(os.getenv("RB_PORT", "8080"))

app = Flask(__name__)


@app.post("/webhook")
def webhook():
    try:
        write_txt_block(TXT_PATH, extract_params())
        return jsonify({"ok": True}), 200
    except Exception as e:
        # Return 200 to stop Ground Control retrying; switch to 500 to allow retries.
        return jsonify({"ok": False, "error": str(e)}), 200


@app.get("/healthz")
def health():
    return health_response()


if __name__ == "__main__":
    print(f"→ Writing TXT to: {os.path.abspath(TXT_PATH)}")
    print(f"→ Listening on  : http://{HOST}:{PORT}/webhook")
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT)
    except ImportError:
        print("Waitress not available; using Flask dev server.")
        app.run(host=HOST, port=PORT)
