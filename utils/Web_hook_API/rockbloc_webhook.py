#!/usr/bin/env python3
"""
Ground Control (RockBLOCK) MO Webhook receiver WITH ACCEPTANCE CONTROLS.

What it adds:
- IMEI allow-list via RB_ALLOW_IMEIS env (comma-separated)
- Filter messages older than RB_MIN_TX_ISO (UTC ISO, e.g. 2025-09-01T00:00:00Z)
- Filter messages with MOMSN lower than RB_MIN_MOMSN (per IMEI)
- Dedupe by (imei, momsn) using a tiny SQLite state file (RB_STATE_DB)

Run:
    python rockblock_webhook.py  # http://127.0.0.1:8080/webhook

Env (examples on Windows `set`, on Linux/macOS `export`):
    RB_CSV_PATH=./rockblock_messages.csv
    RB_HOST=0.0.0.0
    RB_PORT=8080
    RB_REQUIRE_JWT=1
    RB_ALLOW_IMEIS=300234010753370,300234010999999
    RB_MIN_TX_ISO=2025-09-01T00:00:00Z
    RB_MIN_MOMSN=100
    RB_STATE_DB=./rockblock_state.sqlite
"""

import csv
import os
import sys
import binascii
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

from flask import Flask, request, jsonify, make_response
import jwt  # PyJWT

# --- Public key from Ground Control (RS256) ---
GC_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlaWAVJfNWC4XfnRx96p9cztBcdQV6l8aKmzAlZdpEcQR6MSPzlgvihaUHNJgKm8t5ShR3jcDXIOI7er30cIN4/9aVFMe0LWZClUGgCSLc3rrMD4FzgOJ4ibD8scVyER/sirRzf5/dswJedEiMte1ElMQy2M6IWBACry9u12kIqG0HrhaQOzc6Tr8pHUWTKft3xwGpxCkV+K1N+9HCKFccbwb8okRP6FFAMm5sBbw4yAu39IVvcSL43Tucaa79FzOmfGs5mMvQfvO1ua7cOLKfAwkhxEjirC0/RYX7Wio5yL6jmykAHJqFG2HT0uyjjrQWMtoGgwv9cIcI7xbsDX6owIDAQAB
-----END PUBLIC KEY-----"""

# --- Config via env ---
CSV_PATH     = os.getenv("RB_CSV_PATH", "./rockblock_messages.csv")
HOST         = os.getenv("RB_HOST", "127.0.0.1")
PORT         = int(os.getenv("RB_PORT", "8080"))
REQUIRE_JWT  = os.getenv("RB_REQUIRE_JWT", "1") != "0"
ALLOW_IMEIS  = [x.strip() for x in os.getenv("RB_ALLOW_IMEIS", "").split(",") if x.strip()]
MIN_TX_ISO   = os.getenv("RB_MIN_TX_ISO", "").strip()  # e.g. 2025-09-01T00:00:00Z
MIN_MOMSN    = int(os.getenv("RB_MIN_MOMSN", "0") or 0)
STATE_DB     = os.getenv("RB_STATE_DB", "./rockblock_state.sqlite")

app = Flask(__name__)

# CSV header (stable order)
CSV_FIELDS = [
    "received_utc",
    "jwt_valid",
    "imei",
    "serial",
    "momsn",
    "transmit_time_utc",
    "iridium_latitude",
    "iridium_longitude",
    "iridium_cep",
    "data_hex",
    "data_text",
    "raw_payload_source",
    "stored_reason"
]

# ---------------- CSV helpers ----------------
def ensure_csv_header(path: str):
    need_header = not os.path.exists(path) or os.path.getsize(path) == 0
    if need_header:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()

def append_csv_row(path: str, row: Dict[str, Any]):
    ensure_csv_header(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writerow(row)

# -------------- State (SQLite) ----------------
def _state_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(STATE_DB)) or ".", exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            imei TEXT NOT NULL,
            momsn INTEGER NOT NULL,
            PRIMARY KEY (imei, momsn)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS min_momsn (
            imei TEXT PRIMARY KEY,
            min_momsn INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

def already_processed(imei: str, momsn: int) -> bool:
    with _state_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM processed WHERE imei=? AND momsn=? LIMIT 1", (imei, momsn))
        return cur.fetchone() is not None

def mark_processed(imei: str, momsn: int):
    with _state_conn() as c:
        cur = c.cursor()
        cur.execute("INSERT OR IGNORE INTO processed(imei, momsn) VALUES(?,?)", (imei, momsn))
        c.commit()

def min_momsn_for(imei: str) -> int:
    # Environment global floor is MIN_MOMSN; table can override per IMEI later if you wish
    return MIN_MOMSN

# -------------- Time helpers ------------------
def parse_gc_transmit_time(s: str) -> Optional[datetime]:
    """
    GC example: '21-10-31 10:41:50' (UTC).
    Returns aware UTC datetime or None.
    """
    s = (s or "").strip()
    for fmt in ("%y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None

def iso_to_utc_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None

def dt_to_iso_z(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# -------------- Payload helpers ---------------
def decode_hex_to_text(hex_str: str) -> Tuple[str, str]:
    if not hex_str:
        return "", ""
    h = "".join(hex_str.split())
    try:
        raw = binascii.unhexlify(h)
        txt = raw.decode("utf-8", errors="replace")
        return h.lower(), txt
    except Exception:
        return h.lower(), ""

def verify_jwt(token: str) -> bool:
    if not token:
        return False
    try:
        jwt.decode(
            token,
            GC_PUBLIC_KEY_PEM,
            algorithms=["RS256"],
            options={"verify_signature": True, "verify_exp": False, "verify_iat": False,
                     "verify_aud": False, "verify_iss": False},
        )
        return True
    except Exception:
        return False

def extract_params() -> Tuple[Dict[str, Any], str]:
    if request.is_json:
        j = request.get_json(silent=True) or {}
        return {k: (v if isinstance(v, str) else str(v)) for k, v in j.items()}, "json"
    form = request.form or {}
    return {k: form.get(k, "") for k in form.keys()}, "form"

# ------------------- Webhook -------------------
@app.post("/webhook")
def webhook():
    params, source = extract_params()

    token = params.get("jwt") or params.get("JWT") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    jwt_ok = verify_jwt(token)
    if REQUIRE_JWT and not jwt_ok:
        # Fast 401 so GC retries (backoff)
        return make_response(jsonify({"ok": False, "error": "invalid_jwt"}), 401)

    imei   = (params.get("imei") or "").strip()
    serial = (params.get("serial") or "").strip()
    momsn_raw = (params.get("momsn") or "").strip()
    try:
        momsn = int(momsn_raw) if momsn_raw else -1
    except Exception:
        momsn = -1

    tx_dt = parse_gc_transmit_time(params.get("transmit_time", ""))
    tx_iso = dt_to_iso_z(tx_dt)

    lat = (params.get("iridium_latitude") or "").strip()
    lon = (params.get("iridium_longitude") or "").strip()
    cep = (params.get("iridium_cep") or "").strip()
    data_hex, data_text = decode_hex_to_text(params.get("data", ""))

    # -------- Acceptance controls (all fast, in-memory/SQLite) --------
    # 1) IMEI allow-list
    if ALLOW_IMEIS and imei not in ALLOW_IMEIS:
        # Still reply 200 to avoid endless retries, but don't store
        return jsonify({"ok": True, "stored": False, "reason": "imei_not_allowed"})

    # 2) Minimum transmit time
    min_dt = iso_to_utc_dt(MIN_TX_ISO)
    if min_dt and tx_dt and tx_dt < min_dt:
        return jsonify({"ok": True, "stored": False, "reason": "older_than_min_tx"})

    # 3) Minimum MOMSN (per IMEI)
    if momsn >= 0:
        floor = min_momsn_for(imei)
        if momsn < floor:
            return jsonify({"ok": True, "stored": False, "reason": "momsn_below_floor", "floor": floor})

    # 4) Dedupe by (IMEI, MOMSN)
    if momsn >= 0 and imei:
        if already_processed(imei, momsn):
            return jsonify({"ok": True, "stored": False, "reason": "duplicate_momsn"})

    # -------- Build CSV row --------
    row = {
        "received_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jwt_valid": "true" if jwt_ok else "false",
        "imei": imei,
        "serial": serial,
        "momsn": str(momsn if momsn >= 0 else ""),
        "transmit_time_utc": tx_iso,
        "iridium_latitude": lat,
        "iridium_longitude": lon,
        "iridium_cep": cep,
        "data_hex": data_hex,
        "data_text": data_text,
        "raw_payload_source": source,
        "stored_reason": "accepted"
    }

    # -------- Persist (append CSV + mark processed) --------
    try:
        append_csv_row(CSV_PATH, row)
        if momsn >= 0 and imei:
            mark_processed(imei, momsn)
    except Exception as e:
        # You can switch to 500 to force GC retry, but CSV errors are usually transient/local
        return jsonify({"ok": True, "stored": False, "reason": f"csv_write_failed:{e}"})

    return jsonify({"ok": True, "stored": True})

@app.get("/healthz")
def health():
    return jsonify({"ok": True})

def main():
    print(f"→ Writing rows to: {os.path.abspath(CSV_PATH)}")
    print(f"→ State DB:       {os.path.abspath(STATE_DB)}")
    print(f"→ Listening on:   http://{HOST}:{PORT}/webhook")
    print(f"→ JWT required:   {REQUIRE_JWT}")
    if ALLOW_IMEIS:
        print(f"→ Allow IMEIs:    {', '.join(ALLOW_IMEIS)}")
    if MIN_TX_ISO:
        print(f"→ Min TX time:    {MIN_TX_ISO}")
    if MIN_MOMSN:
        print(f"→ Min MOMSN:      {MIN_MOMSN} (per IMEI)")
    app.run(host=HOST, port=PORT)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
