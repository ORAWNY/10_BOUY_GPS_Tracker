# utils/Email_parser/email_parser_timeshifter.py
from __future__ import annotations
import re
from typing import Any, Dict, Optional
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_timeish_expr(spec: str) -> tuple[str, int]:
    """
    Parse a timestamp spec used in lookup S.emit_d.timestamp_field.

    Returns:
      (base_key, inline_offset_minutes)

    base_key can be:
      - 'tx'                        (raw transmit)
      - 'tx_lastN'  / 'tx_firstN'  (e.g., 'tx_first10', 'tx_last5')
      - 'rx_lastN'  / 'rx_firstN'  (e.g., 'rx_first15')
      - legacy shorthands:
          'tx_last10', 'rx_last10',
          'rx_recieved_last10', 'rx_received_last10', 'rx_use_nearest_10_min'
    """
    s = (spec or "").strip()
    if not s:
        return ("", 0)

    # split out inline +HH:MM / -HH:MM
    m = re.search(r"([+\-])\s*(\d{1,2}):(\d{2})\s*$", s)
    offset_min = 0
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hh = int(m.group(2))
        mm = int(m.group(3))
        offset_min = sign * (hh * 60 + mm)
        s = s[:m.start()].strip()

    norm = re.sub(r"[^a-z0-9]+", "_", s.lower())

    # Map sources
    def src_tx(x: str) -> str:
        return x.replace("transmit", "tx").replace("transit", "tx")

    def src_rx(x: str) -> str:
        return x.replace("received", "rx")

    norm = src_tx(src_rx(norm))

    # Legacy tokens → explicit
    if norm in {"rx_last10", "rx_recieved_last10", "rx_received_last10", "rx_use_nearest_10_min"}:
        return ("rx_last10", offset_min)
    if norm in {"tx_last10", "tx_last10min"}:
        return ("tx_last10", offset_min)
    if norm in {"tx", "tx_time", "tx_ts12"}:
        return ("tx", offset_min)

    # Generic: rx/tx_(first|last)\d+(min)? with or without underscores
    g = re.match(r"^(rx|tx)_(first|last)_(\d+)(?:_?min)?$", norm)
    if g:
        side = g.group(1)         # rx | tx
        which = g.group(2)        # first | last
        n = int(g.group(3))
        return (f"{side}_{which}{n}", offset_min)

    g2 = re.match(r"^(rx|tx)(first|last)(\d+)(?:min)?$", norm)
    if g2:
        side = g2.group(1)
        which = g2.group(2)
        n = int(g2.group(3))
        return (f"{side}_{which}{n}", offset_min)

    # Fallback: plain column name etc.
    return (norm, offset_min)


def parse_shift_to_minutes(s: Any, fallback_minutes: int = 0) -> int:
    """
    Accepts "+HH:MM", "-H:MM", "HH:MM", "MM", or integer-like string.
    Returns signed minutes; falls back to fallback_minutes on failure.
    """
    try:
        if s is None:
            return int(fallback_minutes)
        if isinstance(s, int):
            return int(s)
        txt = str(s).strip()
        if not txt:
            return int(fallback_minutes)

        # pure minutes like "90" or "-15"
        if re.fullmatch(r"[+-]?\d+", txt):
            return int(txt)

        m = re.match(r"^\s*([+-])?\s*(\d{1,2})\s*:\s*([0-5]?\d)\s*$", txt)
        if m:
            sign = -1 if (m.group(1) == "-") else 1
            hh = int(m.group(2))
            mm = int(m.group(3))
            return sign * (hh * 60 + mm)
        return int(fallback_minutes)
    except Exception:
        return int(fallback_minutes)


def ts12_to_dt(ts12: str) -> Optional[datetime]:
    """Parse YYMMDDHHMMSS into naive datetime (year assumed 2000-2099)."""
    s = re.sub(r"\D+", "", ts12 or "")
    if len(s) != 12:
        return None
    try:
        yy = int(s[0:2]); year = 2000 + yy
        month = int(s[2:4]); day = int(s[4:6])
        hour = int(s[6:8]); minute = int(s[8:10]); second = int(s[10:12])
        return datetime(year, month, day, hour, minute, second)
    except Exception:
        return None


def shift_ts12_minutes(ts12: str, minutes: int) -> str:
    """Shift a YYMMDDHHMMSS string by +/- minutes. Returns original if parse fails."""
    if not minutes:
        return ts12 or ""
    dt = ts12_to_dt(ts12)
    if dt is None:
        return ts12 or ""
    dt2 = dt + timedelta(minutes=int(minutes))
    return dt2.strftime("%y%m%d%H%M%S")


def shift_iso_minutes(iso: str, minutes: int) -> str:
    """
    Shift an ISO-like string 'YYYY-MM-DDTHH:MM:SSZ' or '...±HH:MM' by +/- minutes.
    We preserve 'Z' suffix if it was present; no timezone math beyond adding minutes.
    """
    if not minutes or not iso:
        return iso or ""
    try:
        raw = iso.strip()
        had_z = raw.endswith("Z")
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        dt2 = dt + timedelta(minutes=int(minutes))
        out = dt2.replace(microsecond=0).isoformat()
        return out.replace("+00:00", "Z") if had_z else out
    except Exception:
        return iso or ""


# ─────────────────────────────────────────────────────────────────────────────
# Payload (TXT) timestamp shifting
# ─────────────────────────────────────────────────────────────────────────────

def apply_payload_shift_if_enabled(cfg, ts12: str) -> str:
    """
    Applies cfg.payload_time_shift (HH:MM or minutes) to a YYMMDDHHMMSS string if
    cfg.shift_payload_time is True. Returns the original if disabled.
    """
    if not getattr(cfg, "shift_payload_time", False):
        return ts12 or ""
    total_min = parse_shift_to_minutes(
        getattr(cfg, "payload_time_shift", ""),
        getattr(cfg, "payload_time_shift_minutes", 0),
    )
    return shift_ts12_minutes(ts12, total_min)


# ─────────────────────────────────────────────────────────────────────────────
# Filename token composer (supports tx/rx first/last + N minutes)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_ts12(ts: str) -> str:
    s = re.sub(r"\D+", "", str(ts or ""))
    if len(s) < 12:
        s = s + ("0" * (12 - len(s)))
    return s[:12]

def _rx_ts12_from_received(received_time: str) -> str:
    try:
        dt = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.utcnow()
    return dt.strftime("%y%m%d%H%M%S")

def _ts12_to_dt(ts12: str) -> datetime:
    s = _normalize_ts12(ts12)
    yy = int(s[0:2]); year = 2000 + yy
    month = int(s[2:4]); day = int(s[4:6])
    hour = int(s[6:8]); minute = int(s[8:10]); sec = int(s[10:12])
    try:
        return datetime(year, month, day, hour, minute, sec)
    except Exception:
        return datetime.utcnow().replace(second=0, microsecond=0)

def _dt_to_ts12(dt: datetime) -> str:
    return dt.strftime("%y%m%d%H%M") + "00"

def _floor_to_n_minutes(ts12: str, n: int) -> str:
    dt = _ts12_to_dt(ts12)
    block = max(1, n)
    m = (dt.minute // block) * block
    floored = dt.replace(minute=m, second=0, microsecond=0)
    return _dt_to_ts12(floored)

def _ceil_to_n_minutes(ts12: str, n: int) -> str:
    dt = _ts12_to_dt(ts12)
    block = max(1, n)
    needs_bump = (dt.minute % block != 0) or (dt.second != 0)
    if needs_bump:
        m = ((dt.minute // block) + 1) * block
        base = dt.replace(second=0, microsecond=0)
        while m >= 60:
            base = base.replace(minute=0) + timedelta(hours=1)
            m -= 60
        ceiled = base.replace(minute=m)
    else:
        ceiled = dt.replace(second=0, microsecond=0)
    return _dt_to_ts12(ceiled)

def _parse_period_token(key: str):
    """
    Return (source, mode, minutes) for keys like:
      tx_first10, transmit_last5, received_first30min, rx_last15, transit_last5min
    where:
      source ∈ {'tx','rx'}
      mode   ∈ {'first','last'}  (first = ceil, last = floor)
      minutes = int
    Else return None.
    """
    k = (key or "").strip().lower()
    k = re.sub(r"\s+", "", k)
    k = re.sub(r"min$", "", k)  # allow '...10min'
    m = re.fullmatch(r"(tx|transmit|transit|rx|received)_(first|last)(\d+)", k)
    if not m:
        return None
    raw_src, mode, mins = m.group(1), m.group(2), int(m.group(3))
    src = "tx" if raw_src in ("tx", "transmit", "transit") else "rx"
    return (src, mode, max(1, mins))


def compose_filename_tokens(
    pattern_str: str,
    *,
    granularity: str,
    received_time: str,
    sender_slug: str,
    folder_slug: str,
    payload_date_time_ts12: str,
    ext: str,
    extra_tokens: Optional[Dict[str, Any]] = None,
    apply_shift: bool = False,
    shift_hhmm: str = "",
    shift_minutes: int = 0,
) -> str:
    """
    Expands (token) placeholders in pattern_str.

    Supports dynamic period tokens:
      (transmit_last5), (tx_first10), (received_first30), (rx_last15min)
    and classic tokens:
      (payload_datetime), (date), (time), (datetime), (sender), (folder),
      (transmit_time)/(transmit_ts12)/(transmit_iso),
      (received_last10min)/(recieved_last10min)/(use_nearest_10_min)

    Also supports per-token offsets like (transmit_ts12+01:10) and (rx_last10-30),
    and an optional global shift via apply_shift/shift_hhmm/shift_minutes.
    """
    # Global shift minutes
    total_shift_min_global = parse_shift_to_minutes(shift_hhmm, shift_minutes) if apply_shift else 0

    # Base datetime for date/time/datetime tokens (after global shift, if any)
    try:
        base_dt = datetime.strptime(received_time or "", "%Y-%m-%d %H:%M:%S")
    except Exception:
        base_dt = datetime.utcnow()
    if total_shift_min_global:
        base_dt = base_dt + timedelta(minutes=total_shift_min_global)

    g = (granularity or "day").lower()
    if g not in {"email", "day", "week", "month"}:
        g = "day"

    if g in {"email", "day"}:
        date_key = base_dt.strftime("%Y%m%d")
    elif g == "week":
        y, w, _ = base_dt.isocalendar()
        date_key = f"{y}_W{int(w):02d}"
    else:  # month
        date_key = f"{base_dt.year}{base_dt.month:02d}"

    time_key = base_dt.strftime("%H%M%S")
    datetime_key = date_key if g != "email" else base_dt.strftime("%Y%m%d%H%M%S")

    # payload ts12 (optionally shifted by global)
    pdt_val = re.sub(r"\D+", "", (payload_date_time_ts12 or ""))
    if not pdt_val:
        pdt_val = base_dt.strftime("%y%m%d%H%M") + "00"
    elif total_shift_min_global:
        pdt_val = shift_ts12_minutes(pdt_val, total_shift_min_global)

    base_ci: Dict[str, str] = {
        "payload_datetime": pdt_val,
        "payload_date_time": pdt_val,
        "date": date_key,
        "time": (date_key if g != "email" else time_key),
        "datetime": (date_key if g != "email" else datetime_key),
        "sender": sender_slug,
        "folder": folder_slug,
    }

    extra_ci: Dict[str, str] = {}
    if extra_tokens:
        for k, v in extra_tokens.items():
            if k is None:
                continue
            key_str = str(k)
            val_str = "" if v is None else str(v)
            extra_ci[key_str.lower()] = val_str
            alias = re.sub(r"[^A-Za-z0-9_.-]", "_", key_str).lower()
            extra_ci[alias] = val_str

    # helpers to get tx/rx base timestamps
    def _tx_base_ts12():
        # prefer explicit tx ts12; fall back to rx if missing
        v = extra_ci.get("transmit_ts12") or extra_ci.get("transit_ts12") \
            or extra_ci.get("transmit_time") or ""
        v = _normalize_ts12(v)
        return v if v.strip("0") else _rx_ts12_from_received(received_time)

    def _rx_base_ts12():
        return _rx_ts12_from_received(received_time)

    # Whether we included an explicit time-like token in the pattern
    has_timeish = False

    # token pattern with optional per-token shift e.g. (transmit_ts12+01:10) or (rx_last10-30)
    TOKEN_RE = re.compile(r"\(\s*([^) +]+)\s*(?:([+-])\s*(\d{1,2}:\d{2}|\d+))?\s*\)")

    # "classic" time-ish token keys that imply explicit time in filename
    CLASSIC_TIMEISH_RE = re.compile(
        r"^\s*(?:payload_?date_?time|time|datetime|received_?last10min|use_?nearest_?10_?min|recieved_?last10min|"
        r"transmit_?time|transmit_?ts12|transmit_?iso|transit_?time|transit_?ts12|transit_?iso)\s*$",
        re.IGNORECASE
    )

    def _maybe_shift_value(key_lc: str, val: str, minutes: int) -> str:
        if not minutes or not val:
            return val
        # If the key is time-like OR the value looks like ts12 / ISO, try to shift it.
        if CLASSIC_TIMEISH_RE.match(key_lc) or re.fullmatch(r"\d{12}", re.sub(r"\D+", "", val or "")):
            s = re.sub(r"\D+", "", val or "")
            if len(s) == 12:
                return shift_ts12_minutes(s, minutes)
            if "T" in val and ":" in val:
                return shift_iso_minutes(val, minutes)
        return val

    def _repl(m: re.Match) -> str:
        nonlocal has_timeish

        raw_key = (m.group(1) or "").strip()
        sign = (m.group(2) or "")
        off_txt = (m.group(3) or "")

        per_token_min = parse_shift_to_minutes((sign + off_txt) if off_txt else 0)
        total_min = per_token_min + (total_shift_min_global if apply_shift else 0)

        key = raw_key.lower()

        # 1) Dynamic period tokens: tx/rx_firstN|lastN (accepts aliases like transmit/transit/received)
        got = _parse_period_token(key)
        if got:
            src, mode, mins = got
            base = _tx_base_ts12() if src == "tx" else _rx_base_ts12()
            out = _ceil_to_n_minutes(base, mins) if mode == "first" else _floor_to_n_minutes(base, mins)
            if total_min:
                out = shift_ts12_minutes(out, total_min)
            has_timeish = True
            return out

        # 2) Classic time-ish tokens → mark as timeish and fetch their value
        if CLASSIC_TIMEISH_RE.match(key):
            has_timeish = True

        # 3) Standard replacements (base + extra)
        if key in base_ci:
            base_val = base_ci[key]
            return _maybe_shift_value(key, base_val, total_min)

        base_val = extra_ci.get(key, "")
        return _maybe_shift_value(key, base_val, total_min)

    replaced = TOKEN_RE.sub(_repl, pattern_str or "")

    # If 'email' granularity and no explicit time-like token, append _HHMMSS from (possibly shifted) base_dt
    if g == "email" and not has_timeish:
        replaced = f"{replaced}_{base_dt.strftime('%H%M%S')}"

    # Sanitize filename
    replaced = replaced.replace(" ", "_")
    replaced = re.sub(r"[^A-Za-z0-9_.-]+", "_", replaced)
    replaced = re.sub(r"_+", "_", replaced).strip("._")

    base = replaced or (date_key or "file")
    return f"{base}{ext}"
