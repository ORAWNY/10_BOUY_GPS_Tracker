"""
utils/Web_hook_API/rb_webhook_base.py
======================================
Shared utilities for all RockBLOCK MO webhook receivers.

Previously duplicated across rockbloc_webhook.py, rockbloc_webhook_txt.py
and rockblock_webhook_public.py.  Import from here instead.
"""
from __future__ import annotations

import binascii
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from flask import request, jsonify


# ---------------------------------------------------------------------------
# File-system helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: str) -> None:
    """Create all parent directories for *path* if they don't already exist."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)


# ---------------------------------------------------------------------------
# Payload decoding
# ---------------------------------------------------------------------------

def decode_hex(hex_str: str) -> str:
    """Best-effort decode a hex string to UTF-8 text; returns '' on failure."""
    h = (hex_str or "").replace(" ", "").replace("\n", "")
    if not h:
        return ""
    try:
        return binascii.unhexlify(h).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Flask request helpers
# ---------------------------------------------------------------------------

def extract_params() -> Dict[str, str]:
    """Parse a RockBLOCK form-POST or JSON body into a flat string dict."""
    if request.is_json:
        j = request.get_json(silent=True) or {}
        return {k: (v if isinstance(v, str) else str(v)) for k, v in j.items()}
    form = request.form or {}
    return {k: form.get(k, "") for k in form.keys()}


def health_response():
    """Standard /healthz JSON response."""
    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# Time helpers  (used by the full CSV/state webhook)
# ---------------------------------------------------------------------------

def parse_gc_transmit_time(s: str) -> Optional[datetime]:
    """
    Parse Ground Control transmit_time string to an aware UTC datetime.
    GC example: '21-10-31 10:41:50'
    """
    s = (s or "").strip()
    for fmt in ("%y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def iso_to_utc_dt(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 string (with optional trailing Z) to aware UTC datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def dt_to_iso_z(dt: Optional[datetime]) -> str:
    """Format an aware datetime as 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# TXT output  (shared by txt and public webhook variants)
# ---------------------------------------------------------------------------

def write_txt_block(txt_path: str, params: Dict[str, str]) -> None:
    """Append a human-readable message block to *txt_path*."""
    ensure_dir(txt_path)
    data_hex  = (params.get("data") or "").strip()
    data_text = decode_hex(data_hex)
    now       = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    block = "\n".join([
        "----------------------------------------",
        f"received_utc:       {now}",
        f"imei:               {params.get('imei', '')}",
        f"serial:             {params.get('serial', '')}",
        f"momsn:              {params.get('momsn', '')}",
        f"transmit_time (raw):{params.get('transmit_time', '')}",
        f"iridium_latitude:   {params.get('iridium_latitude', '')}",
        f"iridium_longitude:  {params.get('iridium_longitude', '')}",
        f"iridium_cep:        {params.get('iridium_cep', '')}",
        f"data_hex:           {data_hex}",
        "data_text:",
        data_text,
        "",
    ])
    with open(txt_path, "a", encoding="utf-8") as f:
        f.write(block)
