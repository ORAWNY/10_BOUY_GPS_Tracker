"""
utils/Email_parser/email_parser_payload.py
==========================================
Compressed / encoded payload detection and decoding helpers.

These are pure functions with no GUI or database dependencies — easy to test
in isolation.  Previously inlined at the top of email_parser_core.py.
"""
from __future__ import annotations

import base64
import re
import zlib
from typing import List, Optional

# Matches the canonical payload line formats produced by the logger firmware:
#   [A1]#S,12475,L73,DataLogger,2509041445,...
#   [A1]#D,12475,##,L73,DataLogger,...
PAYLOAD_RE = re.compile(r"^(?:\[[^\]]+\])?#([SD]),(.*)$")

_DATA_LINE_RE = re.compile(r"(?im)^\s*Data\s*:\s*(.+?)\s*$")


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s


def find_encoded_payload_line(body: str) -> Optional[str]:
    """
    Prefer a 'Data: <blob>' line if present; else fall back to the last non-empty line.
    Returns the raw candidate string (not yet decoded).
    """
    if not body:
        return None
    m = _DATA_LINE_RE.search(body)
    if m:
        return _strip_quotes(m.group(1))
    for line in reversed(body.splitlines()):
        line = _strip_quotes(line.strip())
        if line:
            return line
    return None


def find_encoded_payload_candidates(body: str) -> List[str]:
    """
    Return candidate encoded payload strings in order of preference.
    Prefers lines *after* the 'Data:' header, then the inline content,
    then the last non-empty line of the body.
    """
    if not body:
        return []
    lines = body.splitlines()
    candidates: List[str] = []

    for idx, raw in enumerate(lines):
        m = _DATA_LINE_RE.match(raw)
        if not m:
            continue
        inline = _strip_quotes(m.group(1)).strip()
        j = idx + 1
        while j < len(lines):
            nxt = _strip_quotes(lines[j].strip())
            if not nxt:
                j += 1
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9 _-]*:\s", nxt):
                break
            candidates.append(nxt)
            j += 1
        if inline:
            candidates.append(inline)
        break  # only the first Data: block

    if not candidates:
        for line in reversed(lines):
            s = _strip_quotes(line.strip())
            if s:
                candidates.append(s)
                break

    seen: set = set()
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
    if re.fullmatch(r"[0-9A-Fa-f]+", s) is not None:
        return False  # pure hex — handled via hex paths
    return re.fullmatch(r"[A-Za-z0-9+/=]+", s) is not None and (len(s) % 4 == 0)


def _decode_python_escaped_bytes(s: str) -> Optional[bytes]:
    """Turn a string like 'x\\x9c%\\xc8;...' into the actual byte sequence."""
    try:
        unescaped = bytes(s, "utf-8").decode("unicode_escape")
        return unescaped.encode("latin1", errors="ignore")
    except Exception:
        return None


def maybe_decode_compressed_payload(s: str) -> Optional[str]:
    """
    Try to recover a plaintext '#D,...' or '#S,...' payload from an encoded blob.

    Strategies attempted in order:
      0) HEX → ASCII that looks like BASE64 → zlib
      1) BASE64 → zlib
      2) Python-escaped bytes ('x\\x9c..') → zlib
      3) HEX of the escaped representation → unescape → zlib
    """
    if not s:
        return None
    candidate = s.strip()

    # Strategy 0: HEX → (ASCII) BASE64 → zlib
    try:
        if re.fullmatch(r"[0-9A-Fa-f]+", candidate):
            ascii_text = bytes.fromhex(candidate).decode("latin1", errors="ignore").strip()
            if _looks_base64(ascii_text):
                out = zlib.decompress(base64.b64decode(ascii_text, validate=False)).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    # Strategy 1: BASE64 → zlib
    try:
        if _looks_base64(candidate):
            out = zlib.decompress(base64.b64decode(candidate, validate=False)).decode("utf-8", errors="replace")
            if PAYLOAD_RE.search(out):
                return out.strip()
    except Exception:
        pass

    # Strategy 2: Python-escaped \x.. bytes → zlib
    try:
        if r"\x" in candidate or "\\x" in candidate:
            raw = _decode_python_escaped_bytes(candidate)
            if raw:
                out = zlib.decompress(raw).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    # Strategy 3: HEX of escaped repr → unescape → zlib
    try:
        if re.fullmatch(r"[0-9A-Fa-f]+", candidate):
            stage1_text = bytes.fromhex(candidate).decode("latin1", errors="ignore")
            stage2 = _decode_python_escaped_bytes(stage1_text)
            if stage2:
                out = zlib.decompress(stage2).decode("utf-8", errors="replace")
                if PAYLOAD_RE.search(out):
                    return out.strip()
    except Exception:
        pass

    return None
