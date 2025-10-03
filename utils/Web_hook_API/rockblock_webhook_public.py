#!/usr/bin/env python3
"""
RockBLOCK MO webhook with public URL (via pyngrok).

What this single file does:
- Starts a Flask server with /webhook (POST) and /healthz (GET)
- Opens an ngrok tunnel from code (using pyngrok) and prints the PUBLIC URL
- Appends every received message to a .txt file

Install once:
    python -m pip install flask waitress pyngrok

Run:
    python rockblock_webhook_public.py

Then copy the printed PUBLIC URL ending with /webhook into Rock 7 CORE.
"""

import os
import binascii
from datetime import datetime
from flask import Flask, request, jsonify

# --- TXT output path (change if you like) ---
DEFAULT_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "rockblock_messages.txt")
TXT_PATH = os.getenv("RB_TXT_PATH", DEFAULT_TXT)

NGROK_AUTHTOKEN = "33NU5zU91V5hYfKh2lXBzCIzifp_5hKYNZjp77F1oRoojrQVi"


# --- Web server bind (local) ---
HOST = "127.0.0.1"
PORT = 8080

# --- Optional: Ngrok auth token (improves reliability/rate limits)
# Sign up free at https://dashboard.ngrok.com/get-started/your-authtoken
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "").strip()

# ---------------- Flask app ----------------
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
    # Accept standard form post; also accept JSON
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        params = {k: (v if isinstance(v, str) else str(v)) for k, v in payload.items()}
    else:
        form = request.form or {}
        params = {k: form.get(k, "") for k in form.keys()}

    try:
        _write_block(params)
        return jsonify({"ok": True}), 200
    except Exception as e:
        # Return 200 to prevent endless retries; switch to 500 if you want GC to retry.
        return jsonify({"ok": False, "error": str(e)}), 200

@app.get("/healthz")
def health():
    return jsonify({"ok": True}), 200

# --------------- Start server + tunnel ---------------
def main():
    print(f"→ Writing TXT to: {os.path.abspath(TXT_PATH)}")

    # Start the Flask app with waitress (better on Windows)
    from threading import Thread
    def _serve():
        try:
            from waitress import serve
            serve(app, host=HOST, port=PORT)
        except Exception:
            # dev server fallback
            app.run(host=HOST, port=PORT)

    Thread(target=_serve, daemon=True).start()

    # Start ngrok tunnel to your local port
    try:
        from pyngrok import ngrok, conf
        if NGROK_AUTHTOKEN:
            conf.get_default().auth_token = NGROK_AUTHTOKEN

        # Create a public HTTPs URL forwarding to http://127.0.0.1:8080
        public_tunnel = ngrok.connect(addr=PORT, proto="http", bind_tls=True)
        public_url = public_tunnel.public_url
        print(f"→ Public webhook URL: {public_url}/webhook")
        print(f"→ Health check      : {public_url}/healthz")
        print("   Keep this script running while you test. Ctrl+C to stop.")
    except Exception as e:
        print("! Could not start ngrok tunnel from Python.")
        print("  Make sure pyngrok is installed and internet is available.")
        print(f"  Error: {e}")
        print(f"→ You can still POST locally: http://{HOST}:{PORT}/webhook")

    # Keep process alive
    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\nbye")

if __name__ == "__main__":
    main()
