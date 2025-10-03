#!/usr/bin/env python3
"""
Super-simple RockBLOCK MO webhook → append to TXT.

- POST /webhook  (Ground Control calls this)
- GET  /healthz  (you can check server is alive)

Writes each message as a small block of text to rockblock_messages.txt
right next to this script, unless RB_TXT_PATH is set.

Fast, no JWT verification (you can add later).
"""

import os
import binascii
from datetime import datetime
from flask import Flask, request, jsonify

# --- Config (optional environment variable to change output path) ---
DEFAULT_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "rockblock_messages.txt")
TXT_PATH = os.getenv("RB_TXT_PATH", DEFAULT_TXT)

app = Flask(__name__)

def _ensure_dir(path: str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)

def _hex_to_text(h: str) -> str:
    """Best-effort decode hex → utf-8 text."""
    h = (h or "").replace(" ", "").replace("\n", "")
    if not h:
        return ""
    try:
        return binascii.unhexlify(h).decode("utf-8", errors="replace")
    except Exception:
        return ""

def _write_block(params: dict):
    """Append a neat text block to the TXT file."""
    _ensure_dir(TXT_PATH)
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    data_hex = (params.get("data") or "").strip()
    data_text = _hex_to_text(data_hex)

    lines = [
        "----------------------------------------",
        f"received_utc:       {now}",
        f"imei:               {params.get('imei','')}",
        f"serial:             {params.get('serial','')}",
        f"momsn:              {params.get('momsn','')}",
        f"transmit_time (raw):{params.get('transmit_time','')}",
        f"iridium_latitude:   {params.get('iridium_latitude','')}",
        f"iridium_longitude:  {params.get('iridium_longitude','')}",
        f"iridium_cep:        {params.get('iridium_cep','')}",
        f"data_hex:           {data_hex}",
        "data_text:",
        data_text,
        ""
    ]
    with open(TXT_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))

@app.post("/webhook")
def webhook():
    # Accept typical form post; also accept JSON just in case.
    params = {}
    if request.is_json:
        j = request.get_json(silent=True) or {}
        params = {k: (v if isinstance(v, str) else str(v)) for k, v in j.items()}
    else:
        form = request.form or {}
        params = {k: form.get(k, "") for k in form.keys()}

    # Minimal work: write to TXT and immediately return HTTP 200.
    try:
        _write_block(params)
        return jsonify({"ok": True}), 200
    except Exception as e:
        # Even on failure, return 200 if you want Ground Control to stop retrying.
        # If you WANT retries, return 500 instead.
        return jsonify({"ok": False, "error": str(e)}), 200

@app.get("/healthz")
def health():
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    # Run with waitress (better on Windows) if available; fallback to Flask dev server otherwise.
    host = "127.0.0.1"
    port = 8080
    try:
        from waitress import serve
        print(f"→ Writing TXT to: {os.path.abspath(TXT_PATH)}")
        print(f"→ Listening on  : http://{host}:{port}/webhook")
        print(f"→ Health check  : http://{host}:{port}/healthz")
        serve(app, host=host, port=port)
    except Exception:
        print("Waitress not available; using Flask dev server.")
        app.run(host=host, port=port)
