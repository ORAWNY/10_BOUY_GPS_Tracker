# utils/Email_parser/email_parser_timeshifter.py
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # graceful fallback if zoneinfo isn't available


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers: parsing + shifting
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
        hh = int(m.group(2)); mm = int(m.group(3))
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

    # Generic: rx/tx_(first|last)\d+(min)? (underscores optional)
    g = re.match(r"^(rx|tx)_(first|last)_(\d+)(?:_?min)?$", norm)
    if g:
        side, which, n = g.group(1), g.group(2), int(g.group(3))
        return (f"{side}_{which}{n}", offset_min)
    g2 = re.match(r"^(rx|tx)(first|last)(\d+)(?:min)?$", norm)
    if g2:
        side, which, n = g2.group(1), g2.group(2), int(g2.group(3))
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
            hh = int(m.group(2)); mm = int(m.group(3))
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


# ── NEW: DST-aware UTC → local conversions ───────────────────────────────────

def _tz_convert_ts12_from_utc(ts12: str, tz_name: str) -> str:
    """
    Interpret YYMMDDHHMMSS as UTC and convert to tz_name (DST-aware),
    returning YYMMDDHHMMSS in the local wall-clock of tz_name.
    """
    if not ts12 or not tz_name or ZoneInfo is None:
        return ts12 or ""
    dt = ts12_to_dt(ts12)
    if dt is None:
        return ts12 or ""
    try:
        local = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
        return local.strftime("%y%m%d%H%M%S")
    except Exception:
        return ts12 or ""


def _tz_convert_iso_from_utc(iso: str, tz_name: str) -> str:
    """
    Interpret ISO string as UTC (Z/+00:00) and convert to tz_name (DST-aware),
    returning an ISO string with the local offset (e.g., +01:00 / -05:00).
    """
    if not iso or not tz_name or ZoneInfo is None:
        return iso or ""
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.replace(microsecond=0).isoformat()
    except Exception:
        return iso or ""


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
# Lookup helpers (moved from core to centralize behavior)
# ─────────────────────────────────────────────────────────────────────────────

def lookup_timestamp_field(lookup: Dict[str, Any]) -> str:
    """Return the configured timestamp field from lookup (S.emit_d or root)."""
    if not isinstance(lookup, dict):
        return ""
    s_emit = (lookup.get("S") or {}).get("emit_d") or {}
    val = str(s_emit.get("timestamp_field", "") or "")
    if not val:
        val = str(lookup.get("timestamp_field", "") or "")
    return val


def lookup_uses_transmit_time(lookup: Dict[str, Any]) -> bool:
    """
    True if we should use raw transmit time from the email body.
    Accepted (case/spacing doesn't matter): transmit_time, transmit_ts12, transit_time,
    transit_ts12, tx, tx_ts12, with optional leading 'input '.
    """
    raw = lookup_timestamp_field(lookup)
    key = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
    accepted = {
        "transmittime", "transmitts12",
        "transittime",  "transitts12",
        "tx", "txts12",
        "inputtransmittime", "inputtransmitts12",
        "inputtransittime",  "inputtransitts12",
        "inputtx", "inputtxts12",
    }
    return key in accepted


def lookup_uses_transmit_last10(lookup: Dict[str, Any]) -> bool:
    """
    True if we should use the transmit time floored to previous 10-min mark.
    """
    raw = lookup_timestamp_field(lookup)
    key = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
    accepted = {
        "transmitlast10min", "transmitprev10", "transmitprevious10min",
        "transitlast10min",  "txlast10min",
        "inputtransmitlast10min", "inputtransmitprev10", "inputtransmitprevious10min",
        "inputtransitlast10min",  "inputtxlast10min",
    }
    return key in accepted


def lookup_uses_received_last10(lookup: Dict[str, Any]) -> bool:
    """
    True if the lookup asks to use received time rounded to a 10-minute mark.
    Accepts any of these (case/spacing doesn’t matter):
      recieved_last10min, received_last10min, use_nearest_10_min, use_nearest_10min
      and with optional leading "input ".
    """
    raw = lookup_timestamp_field(lookup)
    key = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
    return key in {
        "recievedlast10min",   # legacy misspelling
        "receivedlast10min",
        "usenearest10min", "usenearest10min",
        "inputusenearest10min",
        "inputreceivedlast10min",
        "inputrecievedlast10min",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Transmit time extraction (moved from core)
# ─────────────────────────────────────────────────────────────────────────────

TRANSMIT_TIME_RE = re.compile(
    r"(?im)^\s*(?:Transmit|Transit)\s+Time\s*:\s*"
    r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)"
    r"(?:\s*UTC)?\s*$"
)

def _iso_to_ts12_no_tz(iso: str) -> str:
    """
    Turn 'YYYY-MM-DDTHH:MM:SS[Z|±HH:MM]' into YYMMDDHHMMSS (string formatting only).
    No timezone math is performed.
    """
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", (iso or "").strip())
    if not m:
        return ""
    y, M, d, h, m_, s = m.groups()
    return f"{y[2:]}{M}{d}{h}{m_}{s}"

def extract_transmit_time_from_body(body: str) -> tuple[str, str]:
    """
    Returns (transmit_iso, transmit_ts12). If not found, both are ''.
    Example body line: 'Transmit Time: 2025-10-01T15:23:58Z UTC'
    """
    if not body:
        return "", ""
    m = TRANSMIT_TIME_RE.search(body)
    if not m:
        return "", ""
    iso = m.group("iso").strip()
    ts12 = _iso_to_ts12_no_tz(iso)
    return iso, ts12


# ─────────────────────────────────────────────────────────────────────────────
# Filename token composer + helpers (existing, centralized here)
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
    apply_tz: bool = False,
    tz_name: str = "",
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

    # Base datetime derived from received_time (assumed UTC), then optional TZ convert
    try:
        base_dt_utc = datetime.strptime(received_time or "", "%Y-%m-%d %H:%M:%S")
    except Exception:
        base_dt_utc = datetime.utcnow()

    base_dt = base_dt_utc
    if apply_tz and tz_name and ZoneInfo is not None:
        try:
            base_dt = base_dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass

    # Apply a numeric global shift after TZ conversion
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

    # payload ts12 (UNSHIFTED here; per-token/global handled later)
    pdt_val = re.sub(r"\D+", "", (payload_date_time_ts12 or ""))
    if not pdt_val:
        pdt_val = base_dt.strftime("%y%m%d%H%M") + "00"

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
        if not val:
            return val

        # First: apply DST-aware UTC→TZ conversion if requested
        v = val
        if apply_tz and tz_name:
            s12 = re.sub(r"\D+", "", v or "")
            if len(s12) == 12:
                v = _tz_convert_ts12_from_utc(s12, tz_name)
            elif "T" in v and ":" in v:
                v = _tz_convert_iso_from_utc(v, tz_name)

        # Then: per-token/global numeric minutes, if any
        if minutes:
            s = re.sub(r"\D+", "", v or "")
            if len(s) == 12:
                return shift_ts12_minutes(v, minutes)
            if "T" in v and ":" in v:
                return shift_iso_minutes(v, minutes)
        return v

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
            # Apply TZ first (so "first/last N minutes" are interpreted in UTC, then displayed in TZ)
            if apply_tz and tz_name:
                out = _tz_convert_ts12_from_utc(out, tz_name)
            if total_min:
                out = shift_ts12_minutes(out, total_min)
            has_timeish = True
            return out

        # 2) Classic time-ish tokens → mark as timeish
        if CLASSIC_TIMEISH_RE.match(key):
            has_timeish = True

        # 3) Standard replacements (base + extra)
        if key in base_ci:
            base_val = base_ci[key]
            return _maybe_shift_value(key, base_val, total_min)

        base_val = extra_ci.get(key, "")
        return _maybe_shift_value(key, base_val, total_min)

    replaced = TOKEN_RE.sub(_repl, pattern_str or "")

    # If 'email' granularity and no explicit time-like token, append _HHMMSS.
    # base_dt already includes TZ conversion + global shift above.
    if g == "email" and not has_timeish:
        replaced = f"{replaced}_{base_dt.strftime('%H%M%S')}"

    # Sanitize filename
    replaced = replaced.replace(" ", "_")
    replaced = re.sub(r"[^A-Za-z0-9_.-]+", "_", replaced)
    replaced = re.sub(r"_+", "_", replaced).strip("._")

    base = replaced or (date_key or "file")
    return f"{base}{ext}"


# Expose a small utility to construct extra filename tokens used by core
def received_prev10_ts12_from_received_time(received_time: str) -> str:
    try:
        dt = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.utcnow()
    floored = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
    return floored.strftime("%y%m%d%H%M") + "00"

def make_filename_extra_tokens(received_time: str, base_extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Provides tokens usable in filename patterns:
      (received_last10min) / (received_last10_min) / (use_nearest_10_min) / (use_nearest_10min)
      → YYMMDDHHMMSS snapped to previous 10-min mark.
    """
    extra = dict(base_extra or {})
    ts12 = received_prev10_ts12_from_received_time(received_time)
    # canonical + common aliases (case-insensitive match later)
    extra.setdefault("received_last10min", ts12)
    extra.setdefault("received_last10_min", ts12)
    extra.setdefault("use_nearest_10_min", ts12)
    extra.setdefault("use_nearest_10min", ts12)
    extra.setdefault("recieved_last10min", ts12)  # legacy
    return extra


# ─────────────────────────────────────────────────────────────────────────────
# “What timestamp should this payload use?” — single source of truth
# ─────────────────────────────────────────────────────────────────────────────

def yymmddhhmm_from_received(received_time: str) -> str:
    try:
        dt = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.utcnow()
    return dt.strftime("%y%m%d%H%M")

def normalize_ts_len12(ts: str) -> str:
    s = re.sub(r"\D+", "", str(ts or ""))
    if len(s) < 12:
        s = s + ("0" * (12 - len(s)))
    if len(s) > 12:
        s = s[:12]
    return s

def find_ts_idx_in_D_tokens(tokens: List[str]) -> int:
    """
    Return the *token index* (not CSV index) where YYMMDDHHMMSS lives for a device #D line.
    Strategy: prefer slot at '##' + 7, else first 6+ digit token after '##', else first 6+ digit token anywhere.
    """
    try:
        i_hash = tokens.index("##")
    except ValueError:
        i_hash = -1

    def is_ts12_like(s: str) -> bool:
        return bool(re.fullmatch(r"\d{6,}", (s or "").strip()))

    # Expected slot after ##: +7
    if i_hash >= 0 and (i_hash + 7) < len(tokens):
        cand = tokens[i_hash + 7].strip()
        if is_ts12_like(cand):
            return i_hash + 7

    # Else scan after ##
    if i_hash >= 0:
        for j in range(i_hash + 1, len(tokens)):
            if is_ts12_like(tokens[j]):
                return j

    # Fallback: anywhere
    for j, t in enumerate(tokens):
        if is_ts12_like(t):
            return j
    return -1

def _rx_round(received_time: str, which: str, minutes: int) -> str:
    """which: 'first' (ceil) | 'last' (floor) on received_time, returns YYMMDDHHMMSS with SS=00."""
    try:
        dt = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.utcnow()
    dt = dt.replace(second=0, microsecond=0)
    step = max(1, int(minutes))
    if which == "first":
        if dt.minute % step != 0:
            q = (dt.minute // step + 1) * step
            dt = dt.replace(minute=0) + timedelta(hours=1) if q >= 60 else dt.replace(minute=q)
    else:
        dt = dt.replace(minute=(dt.minute // step) * step)
    return dt.strftime("%y%m%d%H%M") + "00"

def compute_effective_payload_ts12(
    *,
    tag: str,                       # 'S' or 'D' (only for semantics; the logic is uniform)
    received_time: str,             # 'YYYY-MM-DD HH:MM:SS'
    transmit_ts12: str,             # '' if none
    lookup: Dict[str, Any],
    cfg,                            # EmailParserConfig (for global payload shift toggles)
    current_ts12: str = "",         # the embedded ts12 we’d otherwise keep
) -> str:
    """
    Decide the final YYMMDDHHMMSS for this payload, uniformly for #D and S→D.

    Precedence (same for both):
      1) lookup timestamp_field (tx/tx_firstN/tx_lastN/rx_firstN/rx_lastN) + optional inline +/-HH:MM
      2) else keep current_ts12
    Finally, apply cfg.shift_payload_time (global payload shift) if enabled.
    """
    base_key, inline_min = parse_timeish_expr(lookup_timestamp_field(lookup))
    choose = (base_key or "").lower()

    out = normalize_ts_len12(current_ts12) if current_ts12 else ""

    # TX-based overrides
    m_tx = re.match(r"^tx_(first|last)(\d+)$", choose)
    if choose == "tx" and transmit_ts12:
        out = normalize_ts_len12(transmit_ts12)
    elif m_tx and transmit_ts12:
        which = m_tx.group(1); minutes = int(m_tx.group(2))
        out = _ceil_to_n_minutes(transmit_ts12, minutes) if which == "first" else _floor_to_n_minutes(transmit_ts12, minutes)

    # RX-based overrides
    m_rx = re.match(r"^rx_(first|last)(\d+)$", choose)
    if m_rx:
        which = m_rx.group(1); minutes = int(m_rx.group(2))
        out = _rx_round(received_time, which, minutes)

    # inline +/- applies to the computed (or fallback) ts12
    if inline_min:
        base_for_inline = out or transmit_ts12 or yymmddhhmm_from_received(received_time) + "00"
        out = shift_ts12_minutes(base_for_inline, inline_min)

    # DST-aware timezone conversion (applies only if payload shift checkbox is on)
    if getattr(cfg, "shift_payload_time", False) and getattr(cfg, "use_timezone_shift", False) and getattr(cfg, "timezone_name", ""):
        base_for_tz = out or (transmit_ts12 or yymmddhhmm_from_received(received_time) + "00")
        out = _tz_convert_ts12_from_utc(base_for_tz, getattr(cfg, "timezone_name", ""))

    # Then apply the legacy numeric minutes (still controlled by the same checkbox)
    out = apply_payload_shift_if_enabled(cfg, out or current_ts12)

    return out or ""


# ─────────────────────────────────────────────────────────────────────────────
# Config normalization (moved from core)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_time_shifts_inplace(cfg) -> None:
    """Parse the human text like '+01:00' / '-30' into minutes every run."""
    try:
        cfg.payload_time_shift_minutes = parse_shift_to_minutes(getattr(cfg, "payload_time_shift", "+00:00"))
    except Exception:
        cfg.payload_time_shift_minutes = 0
    try:
        cfg.filename_time_shift_minutes = parse_shift_to_minutes(getattr(cfg, "filename_time_shift", "+00:00"))
    except Exception:
        cfg.filename_time_shift_minutes = 0
