#!/usr/bin/env python3
"""
Local Fake RockBLOCK:
- POST /webhook        ← accepts RockBLOCK-style posts (form or JSON)
- GET  /feed           ← returns recent messages as JSON for your parser
- GET  /healthz        ← simple health check

Storage: ./mock_rb.sqlite (SQLite)
Auth for /feed: optional "Authorization: Bearer LOCALTEST" header (set FEED_BEARER in env)
"""

import os
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort
import binascii

DB_PATH = os.getenv("RB_MOCK_DB", "./mock_rb.sqlite")
FEED_BEARER = os.getenv("FEED_BEARER", "").strip()   # e.g. "LOCALTEST"

app = Flask(__name__)

def _utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_utc TEXT NOT NULL,
            imei TEXT,
            serial TEXT,
            momsn TEXT,
            transmit_time_utc TEXT,
            iridium_latitude TEXT,
            iridium_longitude TEXT,
            iridium_cep TEXT,
            data_hex TEXT,
            data_text TEXT
        )
    """)
    conn.commit()
    return conn

def _hex_to_text(hx: str) -> str:
    if not hx:
        return ""
    try:
        b = binascii.unhexlify("".join(hx.split()))
        return b.decode("utf-8", errors="replace")
    except Exception:
        return ""

@app.post("/webhook")
def webhook():
    # accept either form-encoded or JSON
    if request.is_json:
        p = request.get_json(silent=True) or {}
        get = lambda k: str(p.get(k, "") or "")
    else:
        f = request.form
        get = lambda k: f.get(k, "") or ""

    transmit_time = (get("transmit_time") or "").strip()
    # Normalize transmit_time to ISO if provided like "12-10-10 10:41:50" or "2025-09-29 14:24:50"
    tx_iso = ""
    for fmt in ("%y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            tx_dt = datetime.strptime(transmit_time, fmt).replace(tzinfo=timezone.utc)
            tx_iso = tx_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            break
        except Exception:
            pass

    data_hex = get("data").lower()
    data_text = _hex_to_text(data_hex)

    row = {
        "received_utc": _utcnow_iso(),
        "imei": get("imei"),
        "serial": get("serial"),
        "momsn": get("momsn"),
        "transmit_time_utc": tx_iso,
        "iridium_latitude": get("iridium_latitude"),
        "iridium_longitude": get("iridium_longitude"),
        "iridium_cep": get("iridium_cep"),
        "data_hex": data_hex,
        "data_text": data_text,
    }

    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages(received_utc, imei, serial, momsn, transmit_time_utc,
                             iridium_latitude, iridium_longitude, iridium_cep,
                             data_hex, data_text)
        VALUES(:received_utc,:imei,:serial,:momsn,:transmit_time_utc,
               :iridium_latitude,:iridium_longitude,:iridium_cep,
               :data_hex,:data_text)
    """, row)
    conn.commit()
    conn.close()
    # Respond 200 to simulate “successfully handled”
    return jsonify({"ok": True})

@app.get("/feed")
def feed():
    # Optional bearer check (mirrors how you'd secure a public feed)
    if FEED_BEARER:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.split(" ", 1)[1].strip() != FEED_BEARER:
            abort(401)

    since = (request.args.get("since") or "").strip()   # expect ISO "YYYY-MM-DDTHH:MM:SSZ"
    limit = int(request.args.get("limit") or 200)
    limit = max(1, min(limit, 1000))

    conn = _connect()
    cur = conn.cursor()

    if since:
        # Return items strictly newer than "since"
        cur.execute("""
            SELECT * FROM messages
            WHERE received_utc > ?
            ORDER BY received_utc DESC
            LIMIT ?
        """, (since, limit))
    else:
        cur.execute("""
            SELECT * FROM messages
            ORDER BY received_utc DESC
            LIMIT ?
        """, (limit,))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Shape to what your parser expects (list of dicts)
    # (keys already match what the parser’s _webhook_iter_messages looks for)
    return jsonify(rows)

@app.get("/healthz")
def health():
    return jsonify({"ok": True, "db": os.path.abspath(DB_PATH)})

if __name__ == "__main__":
    print(f"→ Mock DB: {os.path.abspath(DB_PATH)}")
    print("→ POST  to: http://127.0.0.1:5000/webhook")
    print("→ GET feed: http://127.0.0.1:5000/feed?limit=50")
    app.run(host="127.0.0.1", port=5000, debug=False)
