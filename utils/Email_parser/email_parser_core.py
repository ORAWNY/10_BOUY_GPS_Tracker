# utils/Email_parser/email_parser_core.py
from __future__ import annotations
from datetime import datetime as _dt
from datetime import timedelta as _td

import os
import re
import csv
import json
import sqlite3
import hashlib
import time
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Callable, Optional, Dict, Any, Sequence, Tuple

import requests
import tempfile
import shutil
import base64
import zlib
import binascii


try:
    import win32com.client  # type: ignore
except Exception:  # pragma: no cover
    win32com = None  # graceful fallback if pywin32 is missing

from utils.Email_parser.email_parser_ftp import FTPSession
from utils.Email_parser.email_parser_timeshifter import (
    # generic shifting/parsing
    parse_shift_to_minutes,
    shift_ts12_minutes,
    shift_iso_minutes,
    apply_payload_shift_if_enabled,

    # filename tokens
    compose_filename_tokens,
    make_filename_extra_tokens,

    # lookup decisions
    lookup_timestamp_field,
    lookup_uses_transmit_time,
    lookup_uses_transmit_last10,
    lookup_uses_received_last10,

    # transmit time extraction
    extract_transmit_time_from_body,

    # cfg normalization
    normalize_time_shifts_inplace,

    # canonical payload timestamp decision + helpers
    compute_effective_payload_ts12,
    find_ts_idx_in_D_tokens,
    normalize_ts_len12,
    yymmddhhmm_from_received,
    received_prev10_ts12_from_received_time,
)


DEFAULT_MAILBOX = "metocean configuration"

# Payload lines may look like:
#   [A1]#S,12475,L73,DataLogger,2509041445,11.92,27.39,27.4,26.69,0,3.56,**
#   [A1]#D,12475,##,L73,DataLogger,K1,K1,F5,F5,2509041445,Battery,11.92,Tempat5m,27,DO,4,**
PAYLOAD_RE = re.compile(r"^(?:\[[^\]]+\])?#([SD]),(.*)$")

# Reserved columns always present in DB tables
RESERVED_COLS = ("id", "subject", "sender", "received_time")

# ------------ Compressed / encoded payload helpers ------------
_DATA_LINE_RE = re.compile(r"(?im)^\s*Data\s*:\s*(.+?)\s*$")

def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s

def _find_encoded_payload_line(body: str) -> Optional[str]:
    """
    Prefer a 'Data: <blob>' line if present; else fall back to the last non-empty line.
    Returns the raw candidate string (not yet decoded).
    """
    if not body:
        return None
    m = _DATA_LINE_RE.search(body)
    if m:
        return _strip_quotes(m.group(1))
    # fallback: last non-empty line
    for line in reversed((body or "").splitlines()):
        line = _strip_quotes(line.strip())
        if line:
            return line
    return None

def _find_encoded_payload_candidates(body: str) -> List[str]:
    """
    Return candidate encoded payload strings in order of preference.
    Prefers lines *after* the 'Data:' header (the next non-empty, non-header lines),
    then the inline content on the 'Data:' line, then finally the last non-empty line.
    """
    if not body:
        return []
    lines = body.splitlines()
    candidates: List[str] = []

    # locate "Data:" header line
    for idx, raw in enumerate(lines):
        m = _DATA_LINE_RE.match(raw)
        if not m:
            continue
        inline = _strip_quotes(m.group(1)).strip()
        # collect subsequent non-empty lines until the next header-like line (e.g., "IMEI:", "MOMSN:", etc.)
        j = idx + 1
        while j < len(lines):
            nxt = _strip_quotes(lines[j].strip())
            if not nxt:
                j += 1
                continue
            # another header? stop
            if re.match(r"^[A-Za-z][A-Za-z0-9 _-]*:\s", nxt):
                break
            candidates.append(nxt)   # e.g., the 'eJw...' or 'x\x9c...' line
            j += 1
        # consider inline value too (lower priority than the following lines)
        if inline:
            candidates.append(inline)
        break  # only first Data: block

    # fallback: last non-empty line in the whole body
    if not candidates:
        for line in reversed(lines):
            s = _strip_quotes(line.strip())
            if s:
                candidates.append(s)
                break

    # Dedup but keep order
    seen = set()
    uniq: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _looks_base64(s: str) -> bool:
    s = re.sub(r"\s+", "", s or "")
    if not s:
        return False
    # If it's pure hex, treat it as NOT base64 (we'll try hex paths instead)
    if re.fullmatch(r"[0-9A-Fa-f]+", s) is not None:
        return False
    # base64 alphabet and length multiple of 4
    return re.fullmatch(r"[A-Za-z0-9+/=]+", s) is not None and (len(s) % 4 == 0)


def _decode_python_escaped_bytes(s: str) -> Optional[bytes]:
    """
    Turn a string like:  x\\x9c%\\xc8;...  into the actual bytes b'x\\x9c%\\xc8;...'
    We go through 'unicode_escape' → bytes.
    """
    try:
        # first, turn backslash escapes into actual chars
        unescaped = bytes(s, "utf-8").decode("unicode_escape")
        # then map 1:1 codepoints to bytes
        return unescaped.encode("latin1", errors="ignore")
    except Exception:
        return None

def _maybe_decode_compressed_payload(s: str) -> Optional[str]:
    """
    Try to get plaintext '#D,...' or '#S,...' from the blob:
      0) HEX that decodes to ASCII that looks like BASE64 -> base64 -> zlib
      1) BASE64 -> zlib
      2) Python-escaped bytes text ('x\\x9c..') -> zlib
      3) HEX of the escaped representation -> unescape -> zlib
    """
    if not s:
        return None
    candidate = s.strip()

    # Strategy 0: HEX -> (ASCII) that looks like BASE64 -> zlib
    try:
        if re.fullmatch(r"[0-9A-Fa-f]+", candidate):
            b = bytes.fromhex(candidate)
            ascii_text = b.decode("latin1", errors="ignore").strip()
            if _looks_base64(ascii_text):
                raw = base64.b64decode(ascii_text, validate=False)
                out = zlib.decompress(raw).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    # Strategy 1: Base64 → zlib
    try:
        if _looks_base64(candidate):
            raw = base64.b64decode(candidate, validate=False)
            out = zlib.decompress(raw).decode("utf-8", errors="replace")
            if PAYLOAD_RE.search(out):
                return out.strip()
    except Exception:
        pass

    # Strategy 2: Python-escaped string with \x.. escapes
    try:
        if r"\x" in candidate or "\\x" in candidate:
            raw2 = _decode_python_escaped_bytes(candidate)
            if raw2:
                out = zlib.decompress(raw2).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    # Strategy 3: HEX of the escaped representation ("78 5c 9c ..." -> "x\\x9c...") -> zlib
    try:
        if re.fullmatch(r"[0-9A-Fa-f]+", candidate):
            stage1 = bytes.fromhex(candidate)
            stage1_text = stage1.decode("latin1", errors="ignore")
            stage2 = _decode_python_escaped_bytes(stage1_text)
            if stage2:
                out = zlib.decompress(stage2).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    return None


# --------------------- Config ---------------------
@dataclass
class EmailParserConfig:
    """Single parser definition (mailbox + multiple folder paths)."""
    mailbox: str = DEFAULT_MAILBOX

    # --- ORIGINAL FIELDS (kept for back-compat) ---
    db_path: str = ""                    # used when output_format == "db"
    folder_paths: List[List[str]] = None
    auto_run: bool = False

    # --- FILE OUTPUTS ---
    output_format: str = "db"            # "db" | "csv" | "txt"
    output_dir: str = ""                 # for csv/txt outputs
    file_granularity: str = "day"        # "email" | "day" | "week" | "month"
    lookup_path: str = ""                # sender-based lookups: file or folder

    # filename controls (BRACKETED TOKENS: e.g., "(payload_datetime)_(Log_no)")
    filename_pattern: str = "(payload_date_time)"  # dialog usually sets "(payload_datetime)"
    filename_code: str = ""                       # unused now (kept for back-compat)

    # fill used when a value is missing/blank ('' = write blank; e.g., '-9999', 'N/A')
    missing_value: str = ""

    # --- per-parser state (project folder) ---
    state_dir: str = ""     # project folder path (where state DB is stored)
    parser_name: str = ""   # used to name the state DB (unique per parser)
    refresh_tabs: bool = True  # UI flag; core does not act on it

    # --- lookback window from the last checkpoint (in hours) ---
    lookback_hours: int = 2

    # --- Webhook (poll a public URL for newly arrived RockBLOCK posts) ---
    webhook_enabled: bool = False
    webhook_url: str = ""  # e.g., https://your-host.example/feed
    webhook_auth_header: str = ""  # e.g., 'Authorization: Bearer XYZ'
    webhook_since_param: str = "since"  # query param name used for checkpoint
    webhook_limit_param: str = "limit"  # optional page size param name
    webhook_limit: int = 200

    # --- Manual range / checkpoint control ---
    manual_from: str = ""  # "YYYY-MM-DD HH:MM:SS" (local)
    manual_to: str = ""  # "YYYY-MM-DD HH:MM:SS" (local); empty = up to now
    respect_checkpoint: bool = True  # False = ignore checkpoint (use manual_from)
    update_checkpoint: bool = True  # False = don’t advance checkpoints after this run
    reset_state_before_run: bool = False  # True = drop this parser's state before run

    # TXT timestamp override + quiet logging (per parser) ---
    txt_timestamp_mode: str = "payload"  # "payload" | "received_prev10"
    quiet: bool = False  # True = minimal logging

    # time shifts (persisted) ---
    # Payload line timestamps (#D lines and S→D) will be shifted if enabled
    shift_payload_time: bool = False
    payload_time_shift: str = "+00:00"  # UI text, e.g. "+01:00" or "-30"
    payload_time_shift_minutes: int = 0  # parsed minutes

    # Filename time tokens (e.g., (payload_datetime), (rx_last10), etc.)
    shift_filename_time: bool = False
    filename_time_shift: str = "+00:00"  # UI text
    filename_time_shift_minutes: int = 0  # parsed minutes

    # DST-aware timezone conversion (assume inputs are UTC)
    use_timezone_shift: bool = False  # enable UTC→TZ conversion
    timezone_name: str = ""  # e.g. "Europe/London" / "America/New_York"

    # OUTPUT DESTINATIONS ---
    # Local vs FTP destinations: either or both may be enabled.
    use_local_output: bool = True
    use_ftp_output: bool = False

    # FTP/FTPS settings (used only if use_ftp_output is True)
    ftp_host: str = ""                 # hostname or IP
    ftp_port: int = 21                 # 21 for FTP/FTPS
    ftp_username: str = ""
    ftp_password: str = ""
    ftp_remote_dir: str = ""           # remote directory to CWD to (created if needed)
    ftp_use_tls: bool = False          # True → ftplib.FTP_TLS (explicit FTPS)
    ftp_passive: bool = True           # passive mode recommended (firewall friendly)
    ftp_timeout: int = 20              # seconds
    ftp_check_on_start: bool = True    # test connect/cwd before run
    ftp_delete_local_after_upload: bool = False  # if True, delete local files after upload
    ftp_make_vrf_files: bool = False   # NEW: if True (and txt + FTP), create .vrf alongside each .txt

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "EmailParserConfig":
        # Back-compat: per_email_file -> file_granularity
        per_email = d.get("per_email_file", None)
        gran = d.get("file_granularity", None)
        if gran is None and isinstance(per_email, bool):
            gran = "email" if per_email else "day"

        # ---- Normalize time-shift fields (parse "+HH:MM" → minutes) ----
        payload_time_shift = d.get("payload_time_shift", "+00:00")
        payload_minutes = int(d.get("payload_time_shift_minutes", 0) or 0)
        if not payload_minutes and payload_time_shift:
            try:
                payload_minutes = parse_shift_to_minutes(payload_time_shift)
            except Exception:
                payload_minutes = 0

        filename_time_shift = d.get("filename_time_shift", "+00:00")
        filename_minutes = int(d.get("filename_time_shift_minutes", 0) or 0)
        if not filename_minutes and filename_time_shift:
            try:
                filename_minutes = parse_shift_to_minutes(filename_time_shift)
            except Exception:
                filename_minutes = 0
        # ----------------------------------------------------------------

        return EmailParserConfig(
            mailbox=d.get("mailbox", DEFAULT_MAILBOX),
            db_path=d.get("db_path", ""),
            folder_paths=list(d.get("folder_paths", [])),
            auto_run=bool(d.get("auto_run", False)),

            # --- FILE OUTPUTS ---
            output_format=d.get("output_format", d.get("format", "db")),
            output_dir=d.get("output_dir", ""),
            file_granularity=(gran or "day"),
            lookup_path=d.get("lookup_path", d.get("lookup_file", "")),

            filename_pattern=d.get("filename_pattern", "payload_date_time"),
            filename_code=d.get("filename_code", ""),
            missing_value=d.get("missing_value", ""),

            # --- per-parser state (project folder) ---
            state_dir=d.get("state_dir", ""),
            parser_name=d.get("parser_name", d.get("name", "")),
            refresh_tabs=bool(d.get("refresh_tabs", True)),

            # --- lookback window ---
            lookback_hours=int(d.get("lookback_hours", 2)),

            # --- Webhook ---
            webhook_enabled=bool(d.get("webhook_enabled", False)),
            webhook_url=d.get("webhook_url", ""),
            webhook_auth_header=d.get("webhook_auth_header", ""),
            webhook_since_param=d.get("webhook_since_param", "since"),
            webhook_limit_param=d.get("webhook_limit_param", "limit"),
            webhook_limit=int(d.get("webhook_limit", 200)),

            # --- Manual range / checkpoint control ---
            manual_from=d.get("manual_from", ""),
            manual_to=d.get("manual_to", ""),
            respect_checkpoint=bool(d.get("respect_checkpoint", True)),
            update_checkpoint=bool(d.get("update_checkpoint", True)),
            reset_state_before_run=bool(d.get("reset_state_before_run", False)),

            # TXT timestamp override + quiet logging
            txt_timestamp_mode=d.get("txt_timestamp_mode", "payload"),
            quiet=bool(d.get("quiet", True)),

            # --- time shifts (persisted) ---
            shift_payload_time=bool(d.get("shift_payload_time", False)),
            payload_time_shift=payload_time_shift,
            payload_time_shift_minutes=payload_minutes,

            shift_filename_time=bool(d.get("shift_filename_time", False)),
            filename_time_shift=filename_time_shift,
            filename_time_shift_minutes=filename_minutes,

            use_timezone_shift=bool(d.get("use_timezone_shift", False)),
            timezone_name=d.get("timezone_name", ""),

            # --- OUTPUT DESTINATIONS ---
            use_local_output=bool(d.get("use_local_output", True)),
            use_ftp_output=bool(d.get("use_ftp_output", False)),

            # --- FTP/FTPS settings ---
            ftp_host=d.get("ftp_host", ""),
            ftp_port=int(d.get("ftp_port", 21)),
            ftp_username=d.get("ftp_username", ""),
            ftp_password=d.get("ftp_password", ""),
            ftp_remote_dir=d.get("ftp_remote_dir", ""),
            ftp_use_tls=bool(d.get("ftp_use_tls", False)),
            ftp_passive=bool(d.get("ftp_passive", True)),
            ftp_timeout=int(d.get("ftp_timeout", 20)),
            ftp_check_on_start=bool(d.get("ftp_check_on_start", True)),
            ftp_delete_local_after_upload=bool(d.get("ftp_delete_local_after_upload", False)),
            ftp_make_vrf_files=bool(d.get("ftp_make_vrf_files", False)),
        )


def _parse_local_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def reset_state(state_dir: str, parser_name: str, folder_tags: Optional[List[str]] = None):
    """
    Clears the per-parser *state* DB. This DB has tables:
      - checkpoints
      - exports
      - processed_messages
    NOTE: _dedupe_index lives in the OUTPUT .db (DB mode), not in this state DB.
    """
    conn = _open_state_db(state_dir, parser_name)
    try:
        cur = conn.cursor()
        if folder_tags:
            for tag in folder_tags:
                cur.execute("DELETE FROM checkpoints WHERE folder_tag=?", (tag,))
                cur.execute("DELETE FROM exports WHERE folder_tag=?", (tag,))
                cur.execute("DELETE FROM processed_messages WHERE folder_tag=?", (tag,))
        else:
            cur.execute("DELETE FROM checkpoints")
            cur.execute("DELETE FROM exports")
            cur.execute("DELETE FROM processed_messages")
        conn.commit()
    finally:
        conn.close()


def _http_get_json(url: str, headers: Dict[str, str], params: Dict[str, str]) -> Any:
    resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _parse_iso_to_local_str(s: str) -> str:
    """
    Accepts 'YYYY-MM-DDTHH:MM:SSZ' or with offset; returns local
    '%Y-%m-%d %H:%M:%S' (naive). Falls back to local now if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        local = dt.astimezone()  # system local tz
        return local.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _webhook_iter_messages(cfg: EmailParserConfig, folder_tag: str, dt_cutoff: Optional[datetime], logger: Optional[Callable[[str], None]]) -> List["_MailRecord"]:
    """
    Poll a JSON feed endpoint and return newest→oldest messages.

    Expected JSON (array of objects), newest first (preferred) or any order:
    [
      {
        "received_utc": "2025-09-29T14:25:12Z",
        "imei": "300234010753370",
        "serial": "12345",
        "momsn": 345,
        "transmit_time_utc": "2025-09-29T14:24:50Z",
        "data_hex": "48656c6c6f2c...",
        "data_text": "#D,100,##,L73,DataLogger,...,**"
      },
      ...
    ]
    """

    def log(msg: str):
        if logger:
            logger(msg)

    url = (cfg.webhook_url or "").strip()
    if not url:
        raise RuntimeError("Webhook URL is empty. Set 'webhook_url' in the parser settings.")

    # Build headers from 'webhook_auth_header' line, e.g. "Authorization: Bearer XYZ"
    hdr_line = (cfg.webhook_auth_header or "").strip()
    headers: Dict[str, str] = {}
    if hdr_line:
        if ":" in hdr_line:
            k, v = hdr_line.split(":", 1)
            headers[k.strip()] = v.strip()
        else:
            # If user pasted only a token, assume Bearer
            headers["Authorization"] = f"Bearer {hdr_line}"

    params: Dict[str, str] = {}
    if dt_cutoff is not None:
        since_iso = (dt_cutoff).strftime("%Y-%m-%dT%H:%M:%SZ")
        params[cfg.webhook_since_param or "since"] = since_iso

    if cfg.webhook_limit and cfg.webhook_limit_param:
        params[cfg.webhook_limit_param] = str(int(cfg.webhook_limit))

    data = _http_get_json(url, headers, params)

    # Accept array or wrapped object
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        log("Webhook response shape not recognized; expecting list or {'items': [...]} — got fallback empty.")
        items = []

    @dataclass
    class _MailRecord:
        subject: str
        sender: str
        received_time: str  # "%Y-%m-%d %H:%M:%S"
        body: str
        entry_id: str

    recs: List[_MailRecord] = []

    def pick(d: Dict[str, Any], *keys, default="") -> str:
        for k in keys:
            v = d.get(k)
            if v is not None:
                return str(v)
        return default

    for it in items:
        if not isinstance(it, dict):
            continue

        received_iso = pick(it, "received_utc", "received_at", "created_at")
        received_time = _parse_iso_to_local_str(received_iso) if received_iso else datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S")

        # Prefer IMEI and synthesize a lookup key email-like
        imei = pick(it, "imei")
        if imei:
            sender = f"{imei}@rockblock.rock7.com"
        else:
            sender = pick(it, "sender", "serial", default="RockBLOCK")

        momsn_str = pick(it, "momsn")
        subject = f"RB momsn {momsn_str}".strip() if momsn_str else "RB message"

        body = pick(it, "data_text")
        if not body:
            hx = pick(it, "data_hex").replace(" ", "")
            try:
                body = bytes.fromhex(hx).decode("utf-8", errors="replace")
            except Exception:
                body = hx  # preserve hex if undecodable

        entry_id = pick(it, "entry_id")
        if not entry_id:
            if momsn_str:
                entry_id = f"MOMSN:{momsn_str}"
            elif imei:
                entry_id = f"RB:{imei}:{received_time}"
            else:
                entry_id = f"RB:{sender}:{received_time}"

        recs.append(_MailRecord(
            subject=subject,
            sender=sender,
            received_time=received_time,
            body=body,
            entry_id=entry_id,
        ))

    recs.sort(key=lambda r: r.received_time, reverse=True)

    if dt_cutoff is not None:
        out: List[_MailRecord] = []
        for r in recs:
            try:
                if datetime.strptime(r.received_time, "%Y-%m-%d %H:%M:%S") < dt_cutoff:
                    break
            except Exception:
                pass
            out.append(r)
        return out

    return recs


def _format_outlook_restrict_time(dt: datetime) -> str:
    """Outlook Restrict expects 'MM/DD/YYYY HH:MM AM/PM' (US 12h)."""
    return dt.strftime("%m/%d/%Y %I:%M %p")


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9_.-]+", "_", s)
    return re.sub(r"_+", "_", s).strip("._") or "parser"


def _open_state_db(state_dir: str, parser_name: str) -> sqlite3.Connection:
    """
    Per-parser state lives in a persistent directory. Priority:
      1) explicit `state_dir` argument (used as-is),
      2) BUOY_STATE_DIR environment variable,
      3) BUOY_PROJECT_DIR/state,
      4) fallback: <cwd>/state
    """
    # 1) explicit
    base_dir = (state_dir or "").strip()

    # 2) env override
    if not base_dir:
        base_dir = (os.environ.get("BUOY_STATE_DIR", "") or "").strip()

    # 3) project/state
    if not base_dir:
        proj = (os.environ.get("BUOY_PROJECT_DIR", "") or "").strip()
        if proj:
            base_dir = os.path.join(proj, "state")

    # 4) final fallback to cwd/state
    if not base_dir:
        base_dir = os.path.join(os.getcwd(), "state")

    os.makedirs(base_dir, exist_ok=True)

    path = os.path.join(base_dir, f".email_parser_state_{_slug(parser_name)}.db")
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS exports (
            folder_tag TEXT NOT NULL,
            key        TEXT NOT NULL,
            PRIMARY KEY(folder_tag, key)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            folder_tag    TEXT PRIMARY KEY,
            last_received TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            folder_tag TEXT NOT NULL,
            entry_id   TEXT NOT NULL,
            PRIMARY KEY(folder_tag, entry_id)
        )
    """)
    conn.commit()
    return conn


def _get_checkpoint(conn: sqlite3.Connection, folder_tag: str) -> str:
    cur = conn.cursor()
    cur.execute("SELECT last_received FROM checkpoints WHERE folder_tag=? LIMIT 1", (folder_tag,))
    row = cur.fetchone()
    return row[0] if row and row[0] else ""


def _set_checkpoint(conn: sqlite3.Connection, folder_tag: str, received_time_str: str):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO checkpoints(folder_tag, last_received)
        VALUES(?, ?)
        ON CONFLICT(folder_tag) DO UPDATE SET last_received=excluded.last_received
    """, (folder_tag, received_time_str))
    conn.commit()


def _load_export_keys(conn: sqlite3.Connection, folder_tag: str) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT key FROM exports WHERE folder_tag=?", (folder_tag,))
    return {r[0] for r in cur.fetchall()}


def _index_mark(conn: sqlite3.Connection, folder_tag: str, key: str) -> None:
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO exports(folder_tag, key) VALUES(?,?)", (folder_tag, key))


def _load_processed_ids(conn: sqlite3.Connection, folder_tag: str) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT entry_id FROM processed_messages WHERE folder_tag=?", (folder_tag,))
    return {r[0] for r in cur.fetchall()}


def _mark_processed_id(conn: sqlite3.Connection, folder_tag: str, entry_id: str) -> None:
    if not entry_id:
        return
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO processed_messages(folder_tag, entry_id) VALUES(?,?)",
        (folder_tag, entry_id),
    )



def _get_sender_email(msg) -> str:
    """Try hard to get the SMTP email. Falls back to SenderName if needed."""
    try:
        addr = getattr(msg, "SenderEmailAddress", "") or ""
        if addr:
            return addr.strip()
    except Exception:
        pass
    try:
        sender = getattr(msg, "Sender", None)
        if sender:
            ex_user = sender.GetExchangeUser()
            if ex_user:
                smtp = ex_user.PrimarySmtpAddress
                if smtp:
                    return smtp.strip()
    except Exception:
        pass
    try:
        nm = getattr(msg, "SenderName", "") or ""
        return nm.strip()
    except Exception:
        return ""


def _get_namespace():
    if win32com is None:
        raise RuntimeError("pywin32 is not available. Install 'pywin32' to use the Outlook parser.")
    return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")


def resolve_mailbox(mailbox_name: str):
    ns = _get_namespace()
    last_err = None
    for attempt in range(6):  # ~3 seconds total
        try:
            return ns.Folders.Item(mailbox_name)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"Mailbox '{mailbox_name}' not found or busy:\n{last_err}")


def _resolve_child(mailbox, parent, name: str):
    """Case-insensitive and Inbox-safe resolution."""
    target = (name or "").strip()
    if not target:
        return parent
    try:
        if target.lower() == "inbox":
            return mailbox.GetDefaultFolder(6)
    except Exception:
        pass
    try:
        return parent.Folders[target]
    except Exception:
        try:
            for i in range(1, parent.Folders.Count + 1):
                f = parent.Folders.Item(i)
                if f.Name.strip().lower() == target.lower():
                    return f
        except Exception:
            pass
        raise


def resolve_folder_path(mailbox, path: List[str]):
    f = mailbox
    for seg in path:
        f = _resolve_child(mailbox, f, seg)
    return f


def list_outlook_folder_paths(mailbox_name: str, max_depth: int = 6, max_count: int = 2000) -> List[List[str]]:
    m = resolve_mailbox(mailbox_name)
    paths: List[List[str]] = []

    def walk(folder, prefix: List[str], depth: int):
        if depth > max_depth:
            return
        try:
            count = folder.Folders.Count
        except Exception:
            return
        for i in range(1, count + 1):
            child = folder.Folders.Item(i)
            name = (child.Name or "").strip()
            cur = prefix + [name]
            paths.append(cur)
            if len(paths) >= max_count:
                return
            walk(child, cur, depth + 1)

    walk(m, [], 0)
    return paths


# --------------------- Lookup helpers --------------------
def _load_lookup_bundle(lookup_path: str, sender_email: str, logger: Optional[Callable[[str], None]]) -> Dict[str, Any]:
    """
    Returns a bundle per sender with format-specific config:

    {
      "S": { "columns": [...], "prefix": "F5", "emit_d": {...} },
      "D": { "label_map": {"Battery":"Volt", ...}, "prefix":"F5", "columns":[...](optional) }
    }
    """

    def log(msg: str):
        if logger:
            logger(msg)

    defaults = {
        "S": {"columns": [], "prefix": "F5"},   # empty means "name positionally and pad with col*"
        "D": {"columns": [], "label_map": {}, "prefix": "F5"},
    }
    if not lookup_path:
        return defaults

    try:
        # Folder-of-files mode: <sender>.json
        if os.path.isdir(lookup_path):
            candidate = os.path.join(lookup_path, f"{sender_email}.json")
            if os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return _normalize_lookup_payload(data, defaults)
            # optional info; keep quiet by default
            return defaults

        # Single consolidated file
        with open(lookup_path, "r", encoding="utf-8") as f:
            blob = json.load(f)

        # If the file has a "senders" mapping, try exact & case-insensitive key match
        if isinstance(blob, dict) and "senders" in blob:
            senders = blob.get("senders") or {}
            entry = senders.get(sender_email)
            if entry is None:
                entry = {(k or "").lower(): v for k, v in senders.items()}.get((sender_email or "").lower())
            if entry is None:
                return defaults
            return _normalize_lookup_payload(entry, defaults)

        # Plain {"columns":[...]} treated as global default override for S and preferred order for D
        if isinstance(blob, dict) and isinstance(blob.get("columns"), list):
            cols = [str(x) for x in blob["columns"]]
            return {
                "S": {"columns": cols, "prefix": "F5"},
                "D": {"columns": cols, "label_map": {}, "prefix": "F5"}
            }

        return defaults
    except Exception:
        return defaults


def _normalize_lookup_payload(entry: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    out = {"S": dict(defaults["S"]), "D": dict(defaults["D"])}
    if not isinstance(entry, dict):
        return out

    # If entry just has columns/prefix/label_map, apply to both
    if ("columns" in entry) or ("prefix" in entry) or ("label_map" in entry):
        if isinstance(entry.get("columns"), list):
            out["S"]["columns"] = [str(x) for x in entry["columns"]]
            out["D"]["columns"] = [str(x) for x in entry["columns"]]
        if isinstance(entry.get("label_map"), dict):
            out["D"]["label_map"] = {str(k): str(v) for k, v in entry["label_map"].items()}
        if "prefix" in entry:
            out["S"]["prefix"] = str(entry["prefix"])
            out["D"]["prefix"] = str(entry["prefix"])

    # Formats sub-object
    fmts = entry.get("formats")
    if isinstance(fmts, dict):
        s = fmts.get("S")
        if isinstance(s, dict):
            if isinstance(s.get("columns"), list):
                out["S"]["columns"] = [str(x) for x in s["columns"]]
            if "prefix" in s:
                out["S"]["prefix"] = str(s["prefix"])
            if isinstance(s.get("emit_d"), dict):
                out["S"]["emit_d"] = s["emit_d"]
        d = fmts.get("D")
        if isinstance(d, dict):
            if isinstance(d.get("columns"), list):
                out["D"]["columns"] = [str(x) for x in d["columns"]]
            if isinstance(d.get("label_map"), dict):
                out["D"]["label_map"] = {str(k): str(v) for k, v in d["label_map"].items()}
            if "prefix" in d:
                out["D"]["prefix"] = str(d["prefix"])
    return out


# --------------------- DB helpers (dynamic schema) ---------------------
def _pragma_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    return [r[1] for r in cur.fetchall()]


def _sanitize_col_name(name: str) -> str:
    name = (name or "").strip() or "col"
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if name.lower() in RESERVED_COLS:
        name = f"{name}_1"
    return name


def _uniquify(names: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for n in names:
        base = _sanitize_col_name(n)
        if base in seen:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 1
            out.append(base)
    return out


def _ensure_table_with_columns(cursor: sqlite3.Cursor, table_name: str, cols_needed: List[str]):
    """Create table if missing; else add any missing data columns."""
    cols_needed = _uniquify([c for c in cols_needed if c and c.lower() not in RESERVED_COLS])

    existing_cols: List[str] = []
    try:
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        rows = cursor.fetchall()
        existing_cols = [r[1] for r in rows]
    except Exception:
        existing_cols = []

    if not existing_cols:
        data_sql = ",\n".join([f'"{c}" TEXT' for c in cols_needed]) if cols_needed else ""
        extra = ("," if data_sql else "")
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                id INTEGER PRIMARY KEY,
                subject TEXT,
                sender TEXT,
                received_time TEXT{extra}
                {data_sql}
            )
        """)
        return

    to_add = [c for c in cols_needed if c not in existing_cols]
    for c in to_add:
        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{c}" TEXT')


def _table_name_from_path(path: List[str]) -> str:
    s = "_".join([seg.replace(" ", "_") for seg in path])
    return re.sub(r"\W+", "_", s) or "inbox"


# --------------------- Minimized token helpers kept in core ---------------------
def _sanitize_slug(s: str) -> str:
    s = (s or "unknown").strip().replace("@", "_at_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s) or "unknown"

def _extract_payload_datetime_token(tokens: List[str]) -> str:
    """
    Finds the first ts-like token (12+ digits or with _counter suffix) in an S/D payload list.
    Used only as a hint for filename tokens; final payload timestamp comes from timeshifter.
    """
    def head_before_underscore(s: str) -> str:
        s = s.strip()
        return s.split("_", 1)[0]

    try:
        start = tokens.index("DataLogger") + 1
    except ValueError:
        start = 0

    for j in range(start, len(tokens)):
        t = tokens[j].strip()
        if re.fullmatch(r"\d{6,}(?:_\d+)?", t):
            return head_before_underscore(t)

    for t in tokens:
        tt = t.strip()
        if re.fullmatch(r"\d{6,}(?:_\d+)?", tt):
            return head_before_underscore(tt)

    return ""


# --------------------- Dedupe keys (DB and file modes) ---------------------
def _get_entry_id(msg) -> str:
    try:
        eid = getattr(msg, "EntryID", "") or ""
        return str(eid)
    except Exception:
        return ""


def _hash_values(subject: str, sender: str, received_time: str, values: Sequence[str]) -> str:
    h = hashlib.sha256()
    h.update((subject or "").encode("utf-8", "ignore"))
    h.update((sender or "").encode("utf-8", "ignore"))
    h.update((received_time or "").encode("utf-8", "ignore"))
    for v in values:
        h.update((v or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def _make_dedupe_key(entry_id: str, subject: str, sender: str, received_time: str, values: Sequence[str]) -> str:
    if entry_id:
        return "ID:" + entry_id
    return "H:" + _hash_values(subject, sender, received_time, values)


# DB-side dedupe (in the same .db)
def _ensure_db_dedupe_table(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _dedupe_index (
            folder_tag TEXT NOT NULL,
            key TEXT NOT NULL,
            PRIMARY KEY (folder_tag, key)
        )
    """)
    conn.commit()


def _db_index_has(conn: sqlite3.Connection, folder_tag: str, key: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM _dedupe_index WHERE folder_tag=? AND key=? LIMIT 1", (folder_tag, key))
    return cur.fetchone() is not None


def _db_index_mark(conn: sqlite3.Connection, folder_tag: str, key: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO _dedupe_index(folder_tag, key) VALUES(?,?)",
        (folder_tag, key),
    )


# --------------------- Payload extraction ---------------------
def _first_payload_line(body: str) -> Optional[Tuple[str, List[str]]]:
    for raw in (body or "").splitlines():
        line = raw.strip()
        line = line.strip('"').strip("'")
        m = PAYLOAD_RE.match(line)
        if not m:
            continue
        tag = m.group(1)
        rest = m.group(2)
        toks = [t.strip() for t in rest.split(",")]
        while toks and toks[-1] in ("**", "##", ""):
            toks.pop()
        return tag, toks
    return None


def _iter_payload_lines(body: str) -> List[Tuple[str, List[str]]]:
    out: List[Tuple[str, List[str]]] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        line = line.strip('"').strip("'")
        m = PAYLOAD_RE.match(line)
        if not m:
            continue
        tag = m.group(1)
        rest = m.group(2)
        toks = [t.strip() for t in rest.split(",")]
        while toks and toks[-1] in ("**", "##", ""):
            toks.pop()
        out.append((tag, toks))
    return out


def _is_digits(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4,}", s or ""))


def _extract_logger_info_S(tokens: List[str], lookup_S: Dict[str, Any]) -> Tuple[str, str, int]:
    prefix = str(lookup_S.get("prefix", "F5"))
    serial = ""
    idx = 0
    try:
        dl = tokens.index("DataLogger")
        for j in range(dl + 1, min(dl + 5, len(tokens))):
            if _is_digits(tokens[j]):
                serial = tokens[j]
                idx = j + 1
                break
    except ValueError:
        for j, t in enumerate(tokens):
            if _is_digits(t):
                serial = t
                idx = j + 1
                break
    return prefix, serial, idx


def _extract_logger_info_D(tokens: List[str], lookup_D: Dict[str, Any]) -> Tuple[str, str, int]:
    prefix = str(lookup_D.get("prefix", "F5"))
    serial = ""
    idx_after_serial = 0

    try:
        dl = tokens.index("DataLogger")
    except ValueError:
        dl = -1

    start = dl + 1 if dl >= 0 else 0
    serial_pos = -1
    for j in range(start, len(tokens)):
        if _is_digits(tokens[j]):
            serial = tokens[j]
            serial_pos = j
            break
    if serial_pos > 0:
        cand = tokens[serial_pos - 1]
        if re.fullmatch(r"[A-Za-z0-9]+", cand):
            prefix = cand
        idx_after_serial = serial_pos + 1
    return prefix, serial, idx_after_serial


def _parse_D_pairs(tokens: List[str], start_idx: int, missing: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    i = start_idx
    while i + 1 < len(tokens):
        k = (tokens[i] or "").strip()
        v = (tokens[i + 1] or "")
        if k in ("**", "##") or k == "":
            i += 2
            continue
        out[k] = v if v != "" else missing
        i += 2
    return out


# --------------------- Header + row building ---------------------
def _build_row_for_S(
    tokens: List[str],
    lookup: Dict[str, Any],
    missing: str,
) -> Tuple[List[str], Dict[str, str], str, str]:
    lookup_S = lookup.get("S", {})
    prefix, serial, _ = _extract_logger_info_S(tokens, lookup_S)

    values = tokens[:]
    cols = list(lookup_S.get("columns", []))
    if len(cols) < len(values):
        cols += [f"col{i+1}" for i in range(len(cols), len(values))]
    headers = _uniquify(cols)

    data_map: Dict[str, str] = {}
    for i, name in enumerate(headers):
        if i < len(values):
            v = values[i]
            data_map[name] = missing if (v is None or v == "") else v
        else:
            data_map[name] = missing

    payload_dt = _extract_payload_datetime_token(tokens)
    return headers, data_map, (f"{prefix}{serial}" if serial else prefix), payload_dt


def _build_row_for_D(
    tokens: List[str],
    lookup: Dict[str, Any],
    missing: str,
) -> Tuple[List[str], Dict[str, str], str, str]:
    lookup_D = lookup.get("D", {})
    label_map = {str(k): str(v) for k, v in dict(lookup_D.get("label_map", {})).items()}
    preferred: List[str] = [str(x) for x in (lookup_D.get("columns") or [])]

    prefix = str(lookup_D.get("prefix", "F5"))
    serial = ""

    try:
        i_hash = tokens.index("##")
    except ValueError:
        i_hash = -1

    xyz_val = ""
    tag_val = ""
    if i_hash >= 0:
        if i_hash + 1 < len(tokens):
            xyz_val = tokens[i_hash + 1].strip()
        if i_hash + 2 < len(tokens):
            tag_val = tokens[i_hash + 2].strip()

    # Capture K1/M2 tokens for use in CSV rows and filename tokens
    k1_val = ""
    m2_val = ""
    if i_hash >= 0:
        # post-## offsets: +1 XYZ, +2 TAG, +3 K1, +4 K1, +5 M2, +6 M2, +7 TS
        if i_hash + 3 < len(tokens):
            k1_val = tokens[i_hash + 3].strip()
        if i_hash + 5 < len(tokens):
            m2_val = tokens[i_hash + 5].strip()

    ts_token = ""
    if i_hash >= 0 and (i_hash + 7) < len(tokens):
        cand = tokens[i_hash + 7].strip()
        if re.fullmatch(r"\d{6,}", cand):
            ts_token = cand
    if not ts_token:
        for t in tokens[(i_hash + 1 if i_hash >= 0 else 0):]:
            if re.fullmatch(r"\d{6,}", t.strip()):
                ts_token = t.strip()
                break
    payload_dt = normalize_ts_len12(ts_token)

    kv_start = (i_hash + 8) if (i_hash >= 0 and (i_hash + 7) < len(tokens)) else len(tokens)

    kv_raw = _parse_D_pairs(tokens, kv_start, missing)
    present: Dict[str, str] = {}
    for raw_k, v in kv_raw.items():
        k = label_map.get(raw_k, raw_k)
        present[str(k)] = str(v) if v != "" else missing

    if xyz_val:
        present.setdefault("XYZ", xyz_val)
    if tag_val:
        present.setdefault("TAG", tag_val)
    if k1_val:
        present.setdefault("K1", k1_val)
    if m2_val:
        present.setdefault("M2", m2_val)

    extras = [k for k in present.keys() if k not in preferred]
    headers = _uniquify(preferred + extras)

    data_map: Dict[str, str] = {h: present.get(h, missing) for h in headers}

    logger_display = prefix
    return headers, data_map, logger_display, payload_dt


# --------------------- TXT helpers ---------------------
def _default_emit_config_S_to_D(lookup_S: Dict[str, Any]) -> Dict[str, Any]:
    cols = [str(c) for c in (lookup_S.get("columns") or [])]
    meta = {"Email_No", "Log_no", "C_S", "C_L", "Source", "date"}
    params = [c for c in cols if c not in meta]
    return {
        "xyz_from": "C_S",
        "tag_from": "Source",
        "timestamp_field": "date",
        "k1": "K1",
        "m2": "M2",
        "battery_label": "BATTERY",
        "battery_value": "12",
        "param_order": params,
        "param_labels": {"Bat1": "bat1", "Bat2": "bat2", "Bat3": "bat3"},
    }

def _emit_get(emit: Dict[str, Any], base_key: str, s_map: Dict[str, Any], default: Optional[str] = None) -> str:
    col_key = f"{base_key}_from"
    if col_key in emit and str(emit[col_key]):
        return str(s_map.get(str(emit[col_key]), "") or "")
    if base_key in emit and str(emit[base_key]):
        return str(emit[base_key])
    return "" if default is None else str(default)


def _compose_d_from_s_line(tokens: List[str], lookup: Dict[str, Any], missing: str) -> str:
    _, s_map, _logger_display, payload_dt = _build_row_for_S(tokens, lookup, missing)
    lookup_S = lookup.get("S", {})
    emit = dict(lookup_S.get("emit_d") or _default_emit_config_S_to_D(lookup_S))

    xyz = _emit_get(emit, "xyz", s_map, default=None)
    if not xyz:
        xf = str(emit.get("xyz_from", "") or "")
        xyz = s_map.get(xf, "") if xf else ""

    tag = _emit_get(emit, "tag", s_map, default=None)
    if not tag:
        tf = str(emit.get("tag_from", "") or "")
        tag = s_map.get(tf, "") if tf else ""

    ts_src = str(emit.get("timestamp_field", "") or "")
    ts_raw = s_map.get(ts_src, "") if ts_src else ""
    if not ts_raw:
        ts_raw = payload_dt
    ts12 = normalize_ts_len12(ts_raw)

    k1 = _emit_get(emit, "k1", s_map, default="K1")
    m2 = _emit_get(emit, "m2", s_map, default="M2")
    bat_lbl = _emit_get(emit, "battery_label", s_map, default="BATTERY")
    bat_val = _emit_get(emit, "battery_value", s_map, default="12")

    order: List[str] = [str(x) for x in (emit.get("param_order") or [])]
    labels: Dict[str, str] = {str(k): str(v) for k, v in (emit.get("param_labels") or {}).items()}

    parts: List[str] = [
        "#D", "100", "##",
        str(xyz or ""), str(tag or ""),
        k1, k1, m2, m2,
        ts12,
        bat_lbl, bat_val,
    ]
    for name in order:
        lbl = labels.get(name, name)
        val = s_map.get(name, missing)
        parts.append(lbl)
        parts.append(val)

    return ",".join(parts + ["**"])


def _compose_txt_payload_line(
    tag: str,
    tokens: List[str],
    lookup: Dict[str, Any],
    missing: str,
    *,
    cfg: EmailParserConfig,
    received_time: str,
    transmit_ts12: str = "",
) -> str:
    """
    Build one export line. Timestamp override is delegated to timeshifter.compute_effective_payload_ts12
    so both inbound #D and S→D behave identically.
    """

    if tag == "D":
        # copy as-is, then replace the timestamp column (if we can find it)
        line = "#D," + ",".join(tokens + ["**"])
        parts = line.split(",")

        t_idx = find_ts_idx_in_D_tokens(tokens)   # token index
        csv_idx = (t_idx + 1) if t_idx >= 0 else -1  # parts has '#D' prefix

        if 0 < csv_idx < len(parts):
            current = parts[csv_idx]
            new_ts = compute_effective_payload_ts12(
                tag=tag,
                received_time=received_time,
                transmit_ts12=transmit_ts12,
                lookup=lookup,
                cfg=cfg,
                current_ts12=current,
            )
            if new_ts:
                parts[csv_idx] = new_ts
        return ",".join(parts)

    # S → canonical #D
    line = _compose_d_from_s_line(tokens, lookup, missing)
    parts = line.split(",")
    if len(parts) >= 10 and parts[0] == "#D":
        current = parts[9]  # ts12 lives here in canonical D lines
        new_ts = compute_effective_payload_ts12(
            tag="S",
            received_time=received_time,
            transmit_ts12=transmit_ts12,
            lookup=lookup,
            cfg=cfg,
            current_ts12=current,
        )
        if new_ts:
            parts[9] = new_ts
        return ",".join(parts)

    return line


# --------------------- File writers ---------------------
def _write_row_csv(path: str, headers: List[str], row: Dict[str, Any], write_header_if_new: bool = True):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    file_exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["subject", "sender", "received_time"] + headers)
        if write_header_if_new and not file_exists:
            w.writeheader()
        w.writerow(row)


def _write_row_txt(path: str, headers: List[str], row: Dict[str, Any]):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("subject: " + str(row.get("subject", "")) + "\n")
        f.write("sender: " + str(row.get("sender", "")) + "\n")
        f.write("received_time: " + str(row.get("received_time", "")) + "\n")
        for h in headers:
            f.write(f"{h}: {row.get(h, '')}\n")
        f.write("-" * 40 + "\n")

def _write_payload_txt_line(path: str, line: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write((line or "") + "\n")


def _ensure_vrf_for_txt(txt_path: str) -> str:
    """
    Make an empty .vrf file next to the given .txt file and return its path.
    If it already exists, we leave it as-is.
    """
    base, _ = os.path.splitext(txt_path)
    vrf_path = base + ".vrf"
    os.makedirs(os.path.dirname(os.path.abspath(vrf_path)) or ".", exist_ok=True)
    # create empty (or touch existing)
    with open(vrf_path, "a", encoding="utf-8"):
        pass
    return vrf_path


# --------------------- Runner ---------------------
def run_parser(cfg: EmailParserConfig, logger: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    def log(msg: str):
        if getattr(cfg, "quiet", True):
            return
        if logger:
            logger(msg)

    # Normalize once per run (covers live GUI edits)
    normalize_time_shifts_inplace(cfg)

    fmt = (cfg.output_format or "db").lower()

    """
    Fast run WITHOUT Outlook.Restrict:
      • Sort newest→oldest, compute cutoff = checkpoint − lookback_hours, and STOP when older.
      • DB mode: growing schema + _dedupe_index; checkpoint still maintained.
      • File modes (csv/txt):
          - Bracketed filename tokens (now includes transmit-time tokens, if present in email body).
          - Per-folder checkpoints (state DB in project folder, named per parser).
          - Exports table prevents duplicate rows; processed EntryIDs avoid re-reading bodies.
      • TXT:
          - '#D' payloads copied as-is (optionally override timestamp with Transmit Time or received_last10).
          - '#S' payloads are converted to canonical '#D' lines as configured.
          - Optional overrides: Transmit Time (exact, no tz math) or received time floored to previous 10 minutes.
      • Optional FTP upload (FTP or FTPS). Enable local output, FTP output, or both.
    """

    if fmt == "db":
        if not cfg.db_path:
            raise ValueError("EmailParserConfig.db_path is empty.")
        os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path) or "."), exist_ok=True)
        log(f"Running email parser → DB: {cfg.db_path} | source: {('WEBHOOK' if getattr(cfg, 'webhook_enabled', False) else cfg.mailbox)} | {len(cfg.folder_paths or [])} folder(s)")
    else:
        # Destinations
        if not (cfg.use_local_output or cfg.use_ftp_output):
            raise ValueError("No output destination selected. Enable use_local_output and/or use_ftp_output.")
        if cfg.use_local_output and not cfg.output_dir:
            raise ValueError("output_dir is empty while local output is enabled.")

    # Determine destination directory for FILE outputs (csv/txt)
    dest_dir: Optional[str] = None
    temp_dir_for_run: Optional[str] = None
    if fmt in ("csv", "txt"):
        if cfg.use_local_output:
            dest_dir = cfg.output_dir
            os.makedirs(dest_dir, exist_ok=True)
        else:
            temp_dir_for_run = tempfile.mkdtemp(prefix="email_parser_tmp_")
            dest_dir = temp_dir_for_run
        log(
            f"Running email parser → {fmt.upper()} to "
            f"{'LOCAL' if cfg.use_local_output else 'TEMP'} dir: {dest_dir} | "
            f"FTP={'ON' if cfg.use_ftp_output else 'OFF'} | "
            f"source: {('WEBHOOK' if getattr(cfg, 'webhook_enabled', False) else cfg.mailbox)} | "
            f"{len(cfg.folder_paths or [])} folder(s)"
        )

    use_webhook = bool(getattr(cfg, "webhook_enabled", False))

    # Optional FTP pre-check
    ftp_session: Optional[FTPSession] = None
    if fmt in ("csv", "txt") and cfg.use_ftp_output:
        ftp_session = FTPSession(cfg, logger)
        if cfg.ftp_check_on_start:
            ok, err = ftp_session.test_connection()
            if ok:
                log("FTP connection OK.")
            else:
                log(f"FTP connection check failed: {err}")

    # Outlook mailbox handle if not webhook
    if not use_webhook:
        mailbox = resolve_mailbox(cfg.mailbox)
        _ = _get_namespace()  # ensure Outlook objects are created

    # ---------- Resolve a SAFE state base (never inside the output directory) ----------
    def _abs(p: Optional[str]) -> str:
        return os.path.abspath(p) if p else ""

    def _is_within(child: str, parent: str) -> bool:
        try:
            c = _abs(child)
            p = _abs(parent)
            if not c or not p:
                return False
            return os.path.commonpath([c, p]) == p
        except Exception:
            return False

    env_state = (os.environ.get("BUOY_STATE_DIR", "") or "").strip()
    env_proj = (os.environ.get("BUOY_PROJECT_DIR", "") or "").strip()
    proj_state = os.path.join(env_proj, "state") if env_proj else ""

    candidates = [
        (cfg.state_dir or "").strip(),
        env_state,
        proj_state,
    ]

    # Preferred candidate among cfg/env/project
    base_for_state = next((c for c in candidates if c), "")

    # If still empty, prefer parent of output folder (user requirement), else cwd/state
    if not base_for_state:
        if fmt in ("csv", "txt"):
            base_for_state = os.path.dirname(_abs(dest_dir)) if dest_dir else ""
        if not base_for_state:
            base_for_state = os.path.join(os.getcwd(), "state")

    # HARD RULE: the state directory cannot be the output folder or any of its children
    if fmt in ("csv", "txt") and dest_dir:
        if _is_within(base_for_state, dest_dir):
            base_for_state = os.path.dirname(_abs(dest_dir)) or os.path.dirname(_abs(dest_dir))
            if _is_within(base_for_state, dest_dir) or not base_for_state:
                base_for_state = os.path.join(os.getcwd(), "state")

    state_conn = _open_state_db(base_for_state, cfg.parser_name or "default")

    # Manual window (one run): parse once
    dt_manual_from = _parse_local_dt(getattr(cfg, "manual_from", ""))
    dt_manual_to = _parse_local_dt(getattr(cfg, "manual_to", ""))  # None = up to now
    respect_ckpt = bool(getattr(cfg, "respect_checkpoint", True))
    update_ckpt = bool(getattr(cfg, "update_checkpoint", True))

    # Optional state reset (whole parser)
    if getattr(cfg, "reset_state_before_run", False):
        try:
            reset_state(base_for_state, cfg.parser_name or "default")
            log("State reset: cleared checkpoints/dedupe for this parser.")
        except Exception as e:
            log(f"State reset failed: {e}")

    conn = None
    cursor = None

    # Track files touched per folder (for FTP upload later)
    touched_files_per_folder: Dict[str, set[str]] = {}

    try:
        if fmt == "db":
            conn = sqlite3.connect(cfg.db_path)
            cursor = conn.cursor()
            _ensure_db_dedupe_table(conn)

        results: Dict[str, Dict[str, int]] = {}

        for raw_path in (cfg.folder_paths or []):
            # Normalize path; also strip mailbox prefix if present
            path = list(raw_path or [])
            if path and path[0].strip().lower() == (cfg.mailbox or DEFAULT_MAILBOX).strip().lower():
                path = path[1:]

            table = _table_name_from_path(path)
            folder_tag = table

            touched_files_per_folder.setdefault(folder_tag, set())

            # Determine cutoff from checkpoint (used by both modes)
            last_checkpoint = _get_checkpoint(state_conn, folder_tag)

            # Lower bound (oldest we will read)
            dt_cutoff = None
            if dt_manual_from:
                if respect_ckpt and last_checkpoint:
                    dt_cutoff = dt_manual_from
                    try:
                        dt_chk = datetime.strptime(last_checkpoint, "%Y-%m-%d %H:%M:%S")
                        dt_look = dt_chk - _td(hours=max(0, int(cfg.lookback_hours or 0)))
                        if dt_look > dt_cutoff:
                            dt_cutoff = dt_look
                    except Exception:
                        pass
                else:
                    dt_cutoff = dt_manual_from
            else:
                if last_checkpoint:
                    try:
                        dt_chk = datetime.strptime(last_checkpoint, "%Y-%m-%d %H:%M:%S")
                        dt_cutoff = dt_chk - _td(hours=max(0, int(cfg.lookback_hours or 0)))
                        log(f"Cutoff from checkpoint {dt_chk:%Y-%m-%d %H:%M:%S} with lookback {cfg.lookback_hours}h → {dt_cutoff:%Y-%m-%d %H:%M:%S}")
                    except Exception as e:
                        log(f"Bad checkpoint '{last_checkpoint}' ({e}); scanning from newest.")

            # Upper bound (newest we will include)
            dt_upper = dt_manual_to  # None means "up to now"

            preloaded_keys: set[str] = set()
            if fmt in ("csv", "txt"):
                preloaded_keys = _load_export_keys(state_conn, folder_tag)
            processed_ids: set[str] = _load_processed_ids(state_conn, folder_tag)

            # --------- Source-specific message enumeration ----------
            messages = []
            if use_webhook:
                try:
                    messages = _webhook_iter_messages(cfg, folder_tag, dt_cutoff, logger)
                    log(f"Polling WEBHOOK {cfg.webhook_url} | {len(messages)} items (newest→oldest)")
                except Exception as e:
                    log(f"Webhook fetch failed: {e}")
                    results[' > '.join(path)] = {"inserted": 0, "skipped": 0}
                    continue
            else:
                try:
                    folder = resolve_folder_path(mailbox, path)
                except Exception as e:
                    log(f"Folder not found: {cfg.mailbox} > " + " > ".join(path) + f" ({e})")
                    results[" > ".join(path)] = {"inserted": 0, "skipped": 1}
                    continue
                try:
                    items = folder.Items
                except Exception as e:
                    log(f"Failed to read items: {e}")
                    results[" > ".join(path)] = {"inserted": 0, "skipped": 0}
                    continue
                try:
                    items.Sort("[ReceivedTime]", True)
                except Exception:
                    pass

            iterable = messages if use_webhook else items
            count = len(messages) if use_webhook else getattr(items, "Count", -1)
            source_label = "WEBHOOK" if use_webhook else cfg.mailbox
            log(f"Scanning {source_label} > " + " > ".join(path) + (f" | {count} items" if isinstance(count, int) and count >= 0 else ""))

            inserted = 0
            skipped_total = 0
            skip_counts: Dict[str, int] = {}
            skip_sample: Dict[str, str] = {}
            max_received_seen: Optional[str] = None

            ext = ".csv" if fmt == "csv" else ".txt" if fmt == "txt" else ""
            gran = (cfg.file_granularity or "day").lower()
            if gran not in {"email", "day", "week", "month"}:
                gran = "day"

            for msg in iterable:
                exported_any_for_message = False
                entry_id = ""
                try:
                    if use_webhook:
                        # msg is _MailRecord from webhook iterator
                        subject = msg.subject or ""
                        sender_name = msg.sender or ""
                        sender_email = msg.sender or ""
                        entry_id = msg.entry_id or ""
                        received_time = msg.received_time or ""
                        body = msg.body or ""
                    else:
                        subject = getattr(msg, "Subject", "") or ""
                        sender_name = getattr(msg, "SenderName", "") or ""
                        sender_email = _get_sender_email(msg)
                        entry_id = _get_entry_id(msg)
                        try:
                            received_time = msg.ReceivedTime.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            received_time = ""
                        body = getattr(msg, "Body", "") or ""

                        # Upper bound (manual_to)
                        if dt_upper is not None and received_time:
                            try:
                                dt_rec = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
                                if dt_rec > dt_upper:
                                    continue
                            except Exception:
                                pass

                    # Cutoff (older bound)
                    if dt_cutoff is not None and received_time:
                        try:
                            dt_rec = datetime.strptime(received_time, "%Y-%m-%d %H:%M:%S")
                            if dt_rec < dt_cutoff:
                                log(f"Reached cutoff {dt_cutoff:%Y-%m-%d %H:%M:%S}; stopping early.")
                                break
                        except Exception:
                            pass

                    if entry_id and entry_id in processed_ids:
                        continue

                    if received_time:
                        if (max_received_seen is None) or (received_time > max_received_seen):
                            max_received_seen = received_time

                    # Parse Transmit Time from the email body (if present)
                    tx_iso, tx_ts12 = extract_transmit_time_from_body(body)
                    tx_tokens: Dict[str, str] = {}
                    if tx_iso or tx_ts12:
                        tx_tokens = {
                            "transmit_time_iso": tx_iso,   # raw ISO
                            "transmit_iso": tx_iso,        # alias
                            "transmit_ts12": tx_ts12,      # YYMMDDHHMMSS (no tz math)
                            "transmit_time": tx_ts12,      # convenient short name
                            "transit_time_iso": tx_iso,    # typo-friendly
                            "transit_ts12": tx_ts12,
                        }

                    lookup = _load_lookup_bundle(cfg.lookup_path, sender_email, logger)

                    # Try normal plaintext payloads first
                    payloads = _iter_payload_lines(body)

                    # If none, attempt to decode a compressed/encoded blob near the "Data:" section (multi-line aware)
                    if not payloads:
                        for cand in _find_encoded_payload_candidates(body):
                            decoded = _maybe_decode_compressed_payload(cand)
                            if decoded:
                                body = (body or "").rstrip() + "\n" + decoded + "\n"
                                payloads = _iter_payload_lines(body)
                                break

                    # ---------- No payload ----------
                    if not payloads:
                        pref_cols = list(lookup.get("S", {}).get("columns") or lookup.get("D", {}).get("columns") or [])
                        headers_for_row = _uniquify(pref_cols if pref_cols else ["col1"])
                        data_map = {h: cfg.missing_value for h in headers_for_row}
                        values_for_hash = [data_map[h] for h in headers_for_row]
                        dedupe_key = _make_dedupe_key(entry_id, subject, sender_email or sender_name, received_time, values_for_hash)

                        if cursor is not None:
                            if _db_index_has(conn, folder_tag, dedupe_key):
                                reason = "duplicate"
                                skipped_total += 1
                                skip_counts[reason] = skip_counts.get(reason, 0) + 1
                                skip_sample.setdefault(reason, f"subj='{subject}' from='{sender_email or sender_name}' when='{received_time}'")
                            else:
                                _ensure_table_with_columns(cursor, table, headers_for_row)
                                insert_cols = ["subject", "sender", "received_time"] + headers_for_row
                                placeholders = ", ".join(["?"] * len(insert_cols))
                                sql = f'INSERT INTO "{table}" (' + ", ".join([f'"{c}"' for c in insert_cols]) + f") VALUES ({placeholders})"
                                params = [subject, sender_email or sender_name, received_time] + [data_map[h] for h in headers_for_row]
                                cursor.execute(sql, params)
                                _db_index_mark(conn, folder_tag, dedupe_key)
                                inserted += 1
                                exported_any_for_message = True
                        else:
                            if dedupe_key in preloaded_keys:
                                reason = "already_exported"
                                skipped_total += 1
                                skip_counts[reason] = skip_counts.get(reason, 0) + 1
                                skip_sample.setdefault(reason, f"subj='{subject}' from='{sender_email or sender_name}' when='{received_time}'")
                            else:
                                # Compose filename, merging transmit-time tokens if present
                                pdt_for_name = ""
                                if fmt == "txt" and lookup_uses_received_last10(lookup):
                                    pdt_for_name = received_prev10_ts12_from_received_time(received_time)

                                extra_for_name = make_filename_extra_tokens(received_time, tx_tokens)
                                out_name = compose_filename_tokens(
                                    cfg.filename_pattern,
                                    granularity=gran,
                                    received_time=received_time,
                                    sender_slug=_sanitize_slug(sender_email or sender_name),
                                    folder_slug=_sanitize_slug(folder_tag),
                                    payload_date_time_ts12=pdt_for_name,
                                    ext=ext,
                                    extra_tokens=extra_for_name,
                                    apply_shift=getattr(cfg, "shift_filename_time", False),
                                    shift_hhmm=getattr(cfg, "filename_time_shift", ""),
                                    shift_minutes=getattr(cfg, "filename_time_shift_minutes", 0),
                                    apply_tz=(getattr(cfg, "shift_filename_time", False) and getattr(cfg,
                                                                                                     "use_timezone_shift",
                                                                                                     False)),
                                    tz_name=getattr(cfg, "timezone_name", ""),
                                )

                                out_path = os.path.join(dest_dir, out_name)

                                if fmt == "csv":
                                    row = {"subject": subject, "sender": sender_email or sender_name, "received_time": received_time, **data_map}
                                    _write_row_csv(out_path, headers_for_row, row, write_header_if_new=not os.path.exists(out_path))
                                elif fmt == "txt":
                                    # Choose timestamp for the S→empty line:
                                    if lookup_uses_transmit_time(lookup) and tx_ts12:
                                        ts12 = tx_ts12
                                    elif lookup_uses_transmit_last10(lookup) and tx_ts12:
                                        ts12 = compute_effective_payload_ts12(
                                            tag="S", received_time=received_time, transmit_ts12=tx_ts12,
                                            lookup={"timestamp_field": "tx_last10"}, cfg=cfg,
                                            current_ts12=yymmddhhmm_from_received(received_time) + "00",
                                        )
                                    elif lookup_uses_received_last10(lookup):
                                        ts12 = received_prev10_ts12_from_received_time(received_time)
                                    else:
                                        ts12 = yymmddhhmm_from_received(received_time) + "00"

                                    _write_payload_txt_line(out_path, f"#S,{ts12},EMPTY,**")

                                    # make matching .vrf if requested (TXT + FTP only)
                                    if cfg.use_ftp_output and getattr(cfg, "ftp_make_vrf_files", False):
                                        vrf_path = _ensure_vrf_for_txt(out_path)
                                        touched_files_per_folder[folder_tag].add(vrf_path)

                                _index_mark(state_conn, folder_tag, dedupe_key)
                                preloaded_keys.add(dedupe_key)
                                touched_files_per_folder[folder_tag].add(out_path)
                                inserted += 1
                                exported_any_for_message = True
                        continue

                    # ---------- Has one or more payload lines ----------
                    use_entry_id = entry_id if len(payloads) <= 1 else ""

                    out_path: Optional[str] = None
                    file_headers_for_this_email: Optional[List[str]] = None
                    first_payload_extra_tokens: Optional[Dict[str, Any]] = None
                    first_payload_dt: str = ""

                    for (tag, toks) in payloads:
                        if tag == "S":
                            headers_for_row, data_map, _logger_display, payload_dt = _build_row_for_S(toks, lookup, cfg.missing_value)
                        else:
                            headers_for_row, data_map, _logger_display, payload_dt = _build_row_for_D(toks, lookup, cfg.missing_value)

                        if first_payload_extra_tokens is None:
                            first_payload_extra_tokens = dict(data_map)
                            first_payload_dt = payload_dt or ""

                            # NEW: expose lookup prefixes for filename patterns
                            s_prefix = (lookup.get("S") or {}).get("prefix", "")
                            d_prefix = (lookup.get("D") or {}).get("prefix", "")

                            # Generic token everyone can use: (prefix)
                            if tag == "S":
                                first_payload_extra_tokens.setdefault("prefix", s_prefix)
                            else:
                                first_payload_extra_tokens.setdefault("prefix", d_prefix)

                            # Optional: expose both for completeness
                            first_payload_extra_tokens.setdefault("s_prefix", s_prefix)
                            first_payload_extra_tokens.setdefault("d_prefix", d_prefix)

                            if tx_tokens:
                                first_payload_extra_tokens.update(tx_tokens)

                        values_for_hash = [data_map.get(h, "") for h in headers_for_row]
                        dedupe_key = _make_dedupe_key(use_entry_id, subject, sender_email or sender_name, received_time, values_for_hash)

                        if cursor is not None:
                            if _db_index_has(conn, folder_tag, dedupe_key):
                                reason = "duplicate"
                                skipped_total += 1
                                skip_counts[reason] = skip_counts.get(reason, 0) + 1
                                skip_sample.setdefault(reason, f"subj='{subject}' from='{sender_email or sender_name}' when='{received_time}'")
                                continue

                            _ensure_table_with_columns(cursor, table, headers_for_row)
                            insert_cols = ["subject", "sender", "received_time"] + headers_for_row
                            placeholders = ", ".join(["?"] * len(insert_cols))
                            sql = f'INSERT INTO "{table}" (' + ", ".join([f'"{c}"' for c in insert_cols]) + f") VALUES ({placeholders})"
                            params = [subject, sender_email or sender_name, received_time] + [data_map.get(h, "") for h in headers_for_row]
                            cursor.execute(sql, params)
                            _db_index_mark(conn, folder_tag, dedupe_key)
                            inserted += 1
                            exported_any_for_message = True
                            continue

                        if dedupe_key in preloaded_keys:
                            reason = "already_exported"
                            skipped_total += 1
                            skip_counts[reason] = skip_counts.get(reason, 0) + 1
                            skip_sample.setdefault(reason, f"subj='{subject}' from='{sender_email or sender_name}' when='{received_time}'")
                            continue

                        if out_path is None:
                            # Possibly override (payload_datetime) in filename for TXT
                            pdt_for_name = first_payload_dt
                            if fmt == "txt" and lookup_uses_received_last10(lookup):
                                pdt_for_name = received_prev10_ts12_from_received_time(received_time)

                            # Merge payload tokens with (received_last10min) + transmit tokens
                            extra_for_name = make_filename_extra_tokens(received_time, first_payload_extra_tokens or {})

                            out_name = compose_filename_tokens(
                                cfg.filename_pattern,
                                granularity=gran,
                                received_time=received_time,
                                sender_slug=_sanitize_slug(sender_email or sender_name),
                                folder_slug=_sanitize_slug(folder_tag),
                                payload_date_time_ts12=pdt_for_name,
                                ext=ext,
                                extra_tokens=extra_for_name,
                                apply_shift=getattr(cfg, "shift_filename_time", False),
                                shift_hhmm=getattr(cfg, "filename_time_shift", ""),
                                shift_minutes=getattr(cfg, "filename_time_shift_minutes", 0),
                                apply_tz=(getattr(cfg, "shift_filename_time", False) and getattr(cfg,
                                                                                                 "use_timezone_shift",
                                                                                                 False)),
                                tz_name=getattr(cfg, "timezone_name", ""),
                            )

                            out_path = os.path.join(dest_dir, out_name)
                            file_headers_for_this_email = list(headers_for_row)

                        if fmt == "txt":
                            line_txt = _compose_txt_payload_line(
                                tag, toks, lookup, cfg.missing_value,
                                cfg=cfg, received_time=received_time, transmit_ts12=tx_ts12
                            )
                            _write_payload_txt_line(out_path, line_txt)

                            _index_mark(state_conn, folder_tag, dedupe_key)
                            preloaded_keys.add(dedupe_key)
                            touched_files_per_folder[folder_tag].add(out_path)
                            inserted += 1
                            exported_any_for_message = True
                            continue

                        headers_for_write = file_headers_for_this_email or headers_for_row
                        row = {
                            "subject": subject,
                            "sender": sender_email or sender_name,
                            "received_time": received_time,
                            **{h: data_map.get(h, "") for h in headers_for_write},
                        }
                        _write_row_csv(out_path, headers_for_write, row, write_header_if_new=not os.path.exists(out_path))
                        _index_mark(state_conn, folder_tag, dedupe_key)
                        preloaded_keys.add(dedupe_key)
                        touched_files_per_folder[folder_tag].add(out_path)
                        inserted += 1
                        exported_any_for_message = True

                    # After writing all payload lines, create .vrf if requested (TXT + FTP only)
                    if fmt == "txt" and out_path and cfg.use_ftp_output and getattr(cfg, "ftp_make_vrf_files", False):
                        vrf_path = _ensure_vrf_for_txt(out_path)
                        touched_files_per_folder[folder_tag].add(vrf_path)

                except Exception as e:
                    reason = "write_error"
                    skipped_total += 1
                    skip_counts[reason] = skip_counts.get(reason, 0) + 1
                    skip_sample.setdefault(reason, str(e))
                finally:
                    if exported_any_for_message and entry_id:
                        _mark_processed_id(state_conn, folder_tag, entry_id)
                        processed_ids.add(entry_id)

            if conn:
                conn.commit()

            # Only advance checkpoint if we’re going "to now" (no manual upper bound) and allowed
            if max_received_seen and update_ckpt and (dt_manual_to is None):
                _set_checkpoint(state_conn, folder_tag, max_received_seen)

            state_conn.commit()

            summary = f"Inserted {inserted}, skipped {skipped_total}"
            if skip_counts:
                parts = [f"{k}: {v}" for k, v in skip_counts.items()]
                summary += " (" + ", ".join(parts) + ")"
            log(summary + f" → {table if cursor is not None else fmt.upper()}")

            for reason, example in (skip_sample or {}).items():
                log(f"• skipped[{reason}] example: {example}")

            results[" > ".join(path)] = {"inserted": inserted, "skipped": skipped_total}

            # ── FTP upload for this folder (if enabled) ──
            if fmt in ("csv", "txt") and cfg.use_ftp_output and ftp_session and touched_files_per_folder[folder_tag]:
                for local_path in sorted(touched_files_per_folder[folder_tag]):
                    try:
                        ftp_session.upload(local_path, os.path.basename(local_path))
                        if cfg.ftp_delete_local_after_upload or (cfg.use_ftp_output and not cfg.use_local_output):
                            try:
                                os.remove(local_path)
                            except Exception:
                                pass
                    except Exception as e:
                        log(f"FTP upload failed for '{local_path}': {e}")

        return results

    finally:
        # Close DB/state
        if conn:
            conn.close()
        if state_conn:
            state_conn.close()
        # Close FTP
        if fmt in ("csv", "txt") and ftp_session:
            ftp_session.close()
        # Cleanup temp dir if we created one and didn't keep files
        if temp_dir_for_run and os.path.isdir(temp_dir_for_run):
            try:
                shutil.rmtree(temp_dir_for_run, ignore_errors=True)
            except Exception:
                pass
