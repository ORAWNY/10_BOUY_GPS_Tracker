#!/usr/bin/env python3
"""
Ground Control (RockBLOCK) MO webhook — CSV output with acceptance controls.

Features:
- IMEI allow-list          (RB_ALLOW_IMEIS env, comma-separated)
- Minimum transmit time    (RB_MIN_TX_ISO, e.g. 2025-09-01T00:00:00Z)
- Minimum MOMSN floor      (RB_MIN_MOMSN)
- Dedup by (imei, momsn)   (SQLite state file, RB_STATE_DB)
- Optional JWT verification (RB_REQUIRE_JWT=1, default on)

Run:
    python rockbloc_webhook.py   # http://127.0.0.1:8080/webhook

Key env vars:
    RB_CSV_PATH     ./rockblock_messages.csv
    RB_HOST         127.0.0.1
    RB_PORT         8080
    RB_REQUIRE_JWT  1
    RB_ALLOW_IMEIS  300234010753370,300234010999999
    RB_MIN_TX_ISO   2025-09-01T00:00:00Z
    RB_MIN_MOMSN    100
    RB_STATE_DB     ./rockblock_state.sqlite
"""

import csv
import os
import sys
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any

from flask import Flask, jsonify, make_response
import jwt  # PyJWT

from utils.Web_hook_API.rb_webhook_base import (
    decode_hex, extract_params, health_response,
    parse_gc_transmit_time, iso_to_utc_dt, dt_to_iso_z, ensure_dir,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CSV_PATH    = os.getenv("RB_CSV_PATH", "./rockblock_messages.csv")
HOST        = os.getenv("RB_HOST", "127.0.0.1")
PORT        = int(os.getenv("RB_PORT", "8080"))
REQUIRE_JWT = os.getenv("RB_REQUIRE_JWT", "1") != "0"
ALLOW_IMEIS = [x.strip() for x in os.getenv("RB_ALLOW_IMEIS", "").split(",") if x.strip()]
MIN_TX_ISO  = os.getenv("RB_MIN_TX_ISO", "").strip()
MIN_MOMSN   = int(os.getenv("RB_MIN_MOMSN", "0") or 0)
STATE_DB    = os.getenv("RB_STATE_DB", "./rockblock_state.sqlite")

# Ground Control RS256 public key
GC_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlaWAVJfNWC4XfnRx96p9cztBcdQV6l8aKmzAlZdpEcQR6MSPzlgvihaUHNJgKm8t5ShR3jcDXIOI7er30cIN4/9aVFMe0LWZClUGgCSLc3rrMD4FzgOJ4ibD8scVyER/sirRzf5/dswJedEiMte1ElMQy2M6IWBACry9u12kIqG0HrhaQOzc6Tr8pHUWTKft3xwGpxCkV+K1N+9HCKFccbwb8okRP6FFAMm5sBbw4yAu39IVvcSL43Tucaa79FzOmfGs5mMvQfvO1ua7cOLKfAwkhxEjirC0/RYX7Wio5yL6jmykAHJqFG2HT0uyjjrQWMtoGgwv9cIcI7xbsDX6owIDAQAB
-----END PUBLIC KEY-----"""

CSV_FIELDS = [
    "received_utc", "jwt_valid", "imei", "serial", "momsn",
    "transmit_time_utc", "iridium_latitude", "iridium_longitude",
    "iridium_cep", "data_hex", "data_text", "raw_payload_source", "stored_reason",
]

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _append_csv(row: Dict[str, Any]) -> None:
    need_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    ensure_dir(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if need_header:
            w.writeheader()
        w.writerow(row)

# ---------------------------------------------------------------------------
# SQLite dedup state
# ---------------------------------------------------------------------------

def _state_conn() -> sqlite3.Connection:
    ensure_dir(STATE_DB)
    conn = sqlite3.connect(STATE_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS processed (
        imei TEXT NOT NULL, momsn INTEGER NOT NULL, PRIMARY KEY (imei, momsn))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS min_momsn (
        imei TEXT PRIMARY KEY, min_momsn INTEGER NOT NULL)""")
    conn.commit()
    return conn


def _already_processed(imei: str, momsn: int) -> bool:
    with _state_conn() as c:
        return c.execute(
            "SELECT 1 FROM processed WHERE imei=? AND momsn=? LIMIT 1", (imei, momsn)
        ).fetchone() is not None


def _mark_processed(imei: str, momsn: int) -> None:
    with _state_conn() as c:
        c.execute("INSERT OR IGNORE INTO processed(imei, momsn) VALUES(?,?)", (imei, momsn))
        c.commit()

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def _verify_jwt(token: str) -> bool:
    if not token:
        return False
    try:
        jwt.decode(token, GC_PUBLIC_KEY_PEM, algorithms=["RS256"],
                   options={"verify_signature": True, "verify_exp": False,
                            "verify_iat": False, "verify_aud": False, "verify_iss": False})
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Webhook route
# ---------------------------------------------------------------------------

@app.post("/webhook")
def webhook():
    from flask import request
    params, source = extract_params(), "form"
    if request.is_json:
        source = "json"

    token  = (params.get("jwt") or params.get("JWT") or
              request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    jwt_ok = _verify_jwt(token)
    if REQUIRE_JWT and not jwt_ok:
        return make_response(jsonify({"ok": False, "error": "invalid_jwt"}), 401)

    imei      = (params.get("imei") or "").strip()
    serial    = (params.get("serial") or "").strip()
    momsn_raw = (params.get("momsn") or "").strip()
    momsn     = int(momsn_raw) if momsn_raw.lstrip("-").isdigit() else -1

    tx_dt  = parse_gc_transmit_time(params.get("transmit_time", ""))
    tx_iso = dt_to_iso_z(tx_dt)

    data_hex  = decode_hex(params.get("data", ""))
    data_text = decode_hex(params.get("data", ""))   # re-decoded as text below
    # decode_hex returns the text; store hex separately
    raw_hex   = "".join((params.get("data") or "").split()).lower()

    # --- Acceptance controls ---
    if ALLOW_IMEIS and imei not in ALLOW_IMEIS:
        return jsonify({"ok": True, "stored": False, "reason": "imei_not_allowed"})

    min_dt = iso_to_utc_dt(MIN_TX_ISO)
    if min_dt and tx_dt and tx_dt < min_dt:
        return jsonify({"ok": True, "stored": False, "reason": "older_than_min_tx"})

    if momsn >= 0 and momsn < MIN_MOMSN:
        return jsonify({"ok": True, "stored": False, "reason": "momsn_below_floor", "floor": MIN_MOMSN})

    if momsn >= 0 and imei and _already_processed(imei, momsn):
        return jsonify({"ok": True, "stored": False, "reason": "duplicate_momsn"})

    # --- Persist ---
    row = {
        "received_utc":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jwt_valid":          "true" if jwt_ok else "false",
        "imei":               imei,
        "serial":             serial,
        "momsn":              str(momsn) if momsn >= 0 else "",
        "transmit_time_utc":  tx_iso,
        "iridium_latitude":   (params.get("iridium_latitude") or "").strip(),
        "iridium_longitude":  (params.get("iridium_longitude") or "").strip(),
        "iridium_cep":        (params.get("iridium_cep") or "").strip(),
        "data_hex":           raw_hex,
        "data_text":          decode_hex(params.get("data", "")),
        "raw_payload_source": source,
        "stored_reason":      "accepted",
    }
    try:
        _append_csv(row)
        if momsn >= 0 and imei:
            _mark_processed(imei, momsn)
    except Exception as e:
        return jsonify({"ok": True, "stored": False, "reason": f"write_failed:{e}"})

    return jsonify({"ok": True, "stored": True})


@app.get("/healthz")
def health():
    return health_response()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print(f"→ CSV output:   {os.path.abspath(CSV_PATH)}")
    print(f"→ State DB:     {os.path.abspath(STATE_DB)}")
    print(f"→ Listening:    http://{HOST}:{PORT}/webhook")
    print(f"→ JWT required: {REQUIRE_JWT}")
    if ALLOW_IMEIS:
        print(f"→ Allow IMEIs:  {', '.join(ALLOW_IMEIS)}")
    if MIN_TX_ISO:
        print(f"→ Min TX time:  {MIN_TX_ISO}")
    if MIN_MOMSN:
        print(f"→ Min MOMSN:    {MIN_MOMSN}")
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
