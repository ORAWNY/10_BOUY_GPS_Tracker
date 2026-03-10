"""
utils/Email_parser/email_parser_imap.py
=======================================
IMAP backend — duck-type drop-in for email_parser_outlook.py.

Provides the same public surface that email_parser_core.py uses:

    _get_namespace()                      → IMAP4 connection (lazy singleton)
    get_sender_email(msg)                 → str
    resolve_mailbox(name)                 → IMAPMailbox
    resolve_folder_path(mb, path)         → IMAPFolder
    list_outlook_folder_paths(name, ...)  → List[List[str]]
    _resolve_child(mb, parent, name)      → IMAPFolder

Messages are wrapped in IMAPMessage objects that expose the same attributes
as Outlook COM MailItems (.Subject, .SenderName, .SenderEmailAddress,
.ReceivedTime, .Body, .EntryID) so email_parser_core.py needs no changes
during the message-iteration loop.

Configure before first use (called automatically from run_parser):
    from utils.Email_parser import email_parser_imap as _imap
    _imap.configure(host="outlook.office365.com", username="x@y.com", password="…")

Office 365 shared mailbox notes
---------------------------------
If IT has granted you full-access delegation to the shared mailbox, you can
log in directly as the shared mailbox address using its password (or an app
password if MFA is on).  Alternatively, set username="delegate@domain.com"
and mailbox="shared@domain.com" — the IMAP server will accept it when the
right delegation permissions are set.

The host for O365 is:  outlook.office365.com  port 993  SSL=True
"""
from __future__ import annotations

import email as _email_lib
import email.header
import email.utils
import imaplib
import re
import ssl
from datetime import datetime, timezone
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Module-level config (populated by configure())
# ---------------------------------------------------------------------------
_cfg: dict = {}


def configure(
    host: str,
    port: int = 993,
    use_ssl: bool = True,
    username: str = "",
    password: str = "",
) -> None:
    """Set IMAP credentials before resolving mailboxes.  Safe to call multiple times."""
    _cfg.update(host=host, port=port, use_ssl=use_ssl,
                username=username, password=password)
    # Force a fresh connection on next use
    global _conn
    _conn = None


# ---------------------------------------------------------------------------
# Connection management — one persistent connection, reconnected on drop
# ---------------------------------------------------------------------------
_conn: Optional[imaplib.IMAP4] = None


def _get_namespace() -> imaplib.IMAP4:
    """Return (and lazily create) the authenticated IMAP connection."""
    global _conn
    if _conn is not None:
        try:
            _conn.noop()
            return _conn
        except Exception:
            _conn = None

    host = _cfg.get("host", "")
    port = int(_cfg.get("port", 993))
    use_ssl = bool(_cfg.get("use_ssl", True))
    username = _cfg.get("username", "")
    password = _cfg.get("password", "")

    if not host:
        raise RuntimeError(
            "IMAP host not configured. Call email_parser_imap.configure() first."
        )

    if use_ssl:
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(host, port)
        try:
            conn.starttls()
        except Exception:
            pass  # server may not support STARTTLS

    conn.login(username, password)
    _conn = conn
    return _conn


# ---------------------------------------------------------------------------
# Folder separator detection
# ---------------------------------------------------------------------------
def _get_folder_separator(conn: imaplib.IMAP4) -> str:
    """Detect the IMAP folder hierarchy separator character (usually '/')."""
    try:
        typ, data = conn.list('""', '""')
        if typ == "OK" and data and data[0]:
            raw = data[0] if isinstance(data[0], str) else data[0].decode("ascii", errors="replace")
            m = re.search(r'"([^"]+)"\s+"?"?\s*$', raw)
            if not m:
                m = re.search(r'\(([^)]*)\)\s+"([^"]+)"', raw)
                if m:
                    return m.group(2)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "/"


# ---------------------------------------------------------------------------
# IMAPMessage — duck-types an Outlook MailItem
# ---------------------------------------------------------------------------
def _decode_header(raw: str) -> str:
    """Decode an RFC2047-encoded email header value."""
    try:
        parts = email.header.decode_header(raw or "")
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(part))
        return "".join(out)
    except Exception:
        return raw or ""


def _extract_plain_body(msg: _email_lib.message.Message) -> str:
    """Walk a parsed email message and return the first text/plain part."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset("utf-8") or "utf-8"
                if payload:
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset("utf-8") or "utf-8"
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


class _ReceivedTimeProxy:
    """Wraps a datetime so that .strftime() works identically to Outlook's ReceivedTime."""
    __slots__ = ("_dt",)

    def __init__(self, dt: datetime):
        self._dt = dt

    def strftime(self, fmt: str) -> str:
        return self._dt.strftime(fmt)

    def __str__(self) -> str:
        return self._dt.strftime("%Y-%m-%d %H:%M:%S")

    def __lt__(self, other) -> bool:
        if isinstance(other, _ReceivedTimeProxy):
            return self._dt < other._dt
        return NotImplemented

    def __gt__(self, other) -> bool:
        if isinstance(other, _ReceivedTimeProxy):
            return self._dt > other._dt
        return NotImplemented


class IMAPMessage:
    """Wraps an RFC822-parsed message to look like an Outlook COM MailItem.

    Attributes exposed (same names as Outlook COM):
        Subject           str
        SenderName        str   (display name from From: header)
        SenderEmailAddress str  (SMTP address)
        Sender            None  (not needed; SenderEmailAddress is already SMTP)
        ReceivedTime      _ReceivedTimeProxy  (supports .strftime())
        Body              str   (plain-text part)
        EntryID           str   (stable "<uid>@<folder>" key used for dedup)
    """

    __slots__ = (
        "Subject", "SenderName", "SenderEmailAddress",
        "Sender", "ReceivedTime", "Body", "EntryID",
    )

    def __init__(
        self,
        uid: str,
        folder_name: str,
        parsed: _email_lib.message.Message,
        internal_date: datetime,
    ) -> None:
        self.EntryID = f"{uid}@{folder_name}"
        self.Subject = _decode_header(parsed.get("Subject", ""))

        from_raw = parsed.get("From", "")
        display_name, addr = email.utils.parseaddr(from_raw)
        self.SenderName = _decode_header(display_name) if display_name else addr
        self.SenderEmailAddress = addr
        self.Sender = None

        # Store as UTC; strftime gives "YYYY-MM-DD HH:MM:SS" matching Outlook's format
        self.ReceivedTime = _ReceivedTimeProxy(internal_date.replace(tzinfo=None))
        self.Body = _extract_plain_body(parsed)


# ---------------------------------------------------------------------------
# UID / INTERNALDATE fetch helpers
# ---------------------------------------------------------------------------
_UID_RE = re.compile(rb"UID\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)


def _parse_uid_dates(data: list) -> List[Tuple[bytes, datetime]]:
    """Extract (uid_bytes, datetime_utc) from a FETCH INTERNALDATE response."""
    pairs: List[Tuple[bytes, datetime]] = []
    for item in data:
        if not item:
            continue
        raw = item[0] if isinstance(item, tuple) else item
        if not isinstance(raw, bytes):
            continue
        m_uid = _UID_RE.search(raw)
        m_date = _DATE_RE.search(raw)
        if not m_uid or not m_date:
            continue
        uid = m_uid.group(1)
        try:
            tup = imaplib.Internaldate2tuple(raw)
            dt = datetime(*tup[:6], tzinfo=timezone.utc) if tup else datetime.now(timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        pairs.append((uid, dt))
    return pairs


def _parse_rfc822_fetch(data: list) -> List[Tuple[bytes, bytes]]:
    """Extract (uid_bytes, raw_rfc822_bytes) from a UID FETCH RFC822 response.

    imaplib returns a flat list of alternating tuples and boundary strings:
        [(b'<seq> (UID <n> RFC822 {size}', b'<raw msg bytes>'), b')', ...]
    """
    pairs: List[Tuple[bytes, bytes]] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        header_part, body = item[0], item[1]
        if not isinstance(body, bytes) or not body:
            continue
        m = _UID_RE.search(header_part if isinstance(header_part, bytes) else b"")
        uid = m.group(1) if m else b"0"
        pairs.append((uid, body))
    return pairs


# ---------------------------------------------------------------------------
# IMAPItems — duck-types Outlook's Items collection
# ---------------------------------------------------------------------------
_FETCH_BATCH = 25  # messages per RFC822 fetch request


class IMAPItems:
    """Lazy, sorted sequence of IMAPMessage objects.

    .Count  — total number of messages in the folder
    .Sort() — no-op (already sorted newest-first at construction)
    Iterating yields IMAPMessage objects, newest first.
    """

    def __init__(
        self,
        conn: imaplib.IMAP4,
        folder_name: str,
        uid_date_pairs: List[Tuple[bytes, datetime]],
    ) -> None:
        self._conn = conn
        self._folder_name = folder_name
        self._pairs = uid_date_pairs  # sorted newest-first

    @property
    def Count(self) -> int:
        return len(self._pairs)

    def Sort(self, field: str, descending: bool = True) -> None:
        """No-op — sorted during construction."""

    def __iter__(self):
        date_map = {uid: dt for uid, dt in self._pairs}
        # Iterate in batches to avoid huge single requests
        for i in range(0, len(self._pairs), _FETCH_BATCH):
            batch_pairs = self._pairs[i : i + _FETCH_BATCH]
            uid_str = b",".join(uid for uid, _ in batch_pairs)
            try:
                typ, data = self._conn.uid("FETCH", uid_str, "(RFC822)")
                if typ != "OK" or not data:
                    continue
            except Exception:
                continue

            for uid_b, raw in _parse_rfc822_fetch(data):
                dt = date_map.get(uid_b, datetime.now(timezone.utc))
                try:
                    parsed = _email_lib.message_from_bytes(raw)
                except Exception:
                    continue
                yield IMAPMessage(
                    uid=uid_b.decode("ascii", errors="replace"),
                    folder_name=self._folder_name,
                    parsed=parsed,
                    internal_date=dt,
                )


# ---------------------------------------------------------------------------
# IMAPFolder — duck-types an Outlook Folder object
# ---------------------------------------------------------------------------
class IMAPFolder:
    """Wraps an IMAP folder; .Items selects it and fetches all UIDs."""

    def __init__(self, conn: imaplib.IMAP4, folder_name: str) -> None:
        self._conn = conn
        self._folder_name = folder_name

    @property
    def Items(self) -> IMAPItems:
        conn = self._conn
        # SELECT the folder (read-only to avoid marking messages as seen)
        typ, _ = conn.select(f'"{self._folder_name}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot select IMAP folder '{self._folder_name}'")

        # Lightweight fetch: UIDs + dates only (no bodies)
        typ, data = conn.uid("FETCH", "1:*", "(UID INTERNALDATE)")
        if typ != "OK" or not data or data == [None]:
            return IMAPItems(conn, self._folder_name, [])

        pairs = _parse_uid_dates(data)
        pairs.sort(key=lambda x: x[1], reverse=True)  # newest first
        return IMAPItems(conn, self._folder_name, pairs)


# ---------------------------------------------------------------------------
# IMAPMailbox — top-level handle returned by resolve_mailbox()
# ---------------------------------------------------------------------------
class IMAPMailbox:
    def __init__(self, conn: imaplib.IMAP4, name: str) -> None:
        self._conn = conn
        self.name = name


# ---------------------------------------------------------------------------
# Folder discovery helpers
# ---------------------------------------------------------------------------
def _list_all_folders(conn: imaplib.IMAP4) -> List[Tuple[str, str]]:
    """Return [(separator, full_folder_name), ...] for all subscribed folders."""
    _LIST_RE = re.compile(r'\(([^)]*)\)\s+"([^"]+)"\s+"?([^"]+)"?\s*$')
    results: List[Tuple[str, str]] = []
    try:
        typ, data = conn.list('""', "*")
        if typ != "OK":
            return []
        for item in data:
            if not item:
                continue
            text = item.decode("utf-7", errors="replace") if isinstance(item, bytes) else str(item)
            m = _LIST_RE.search(text)
            if m:
                sep = m.group(2)
                name = m.group(3).strip().strip('"')
                results.append((sep, name))
    except Exception:
        pass
    return results


def list_outlook_folder_paths(
    mailbox_name: str,
    max_depth: int = 6,
    max_count: int = 2000,
) -> List[List[str]]:
    """Return all IMAP folder paths as lists of path segments.

    Mirrors the Outlook equivalent so the folder-picker UI works unchanged.
    """
    conn = _get_namespace()
    paths: List[List[str]] = []
    for sep, full_name in _list_all_folders(conn):
        parts = full_name.split(sep) if sep else [full_name]
        if len(parts) <= max_depth:
            paths.append(parts)
        if len(paths) >= max_count:
            break
    return paths


# ---------------------------------------------------------------------------
# Public API — mirrors email_parser_outlook.py exactly
# ---------------------------------------------------------------------------
def get_sender_email(msg) -> str:
    """Return the SMTP sender address from an IMAPMessage."""
    try:
        addr = getattr(msg, "SenderEmailAddress", "") or ""
        if addr:
            return addr.strip()
    except Exception:
        pass
    try:
        return (getattr(msg, "SenderName", "") or "").strip()
    except Exception:
        return ""


def resolve_mailbox(mailbox_name: str) -> IMAPMailbox:
    """Validate the IMAP connection and return a mailbox handle."""
    conn = _get_namespace()
    return IMAPMailbox(conn, mailbox_name)


def _find_folder_icase(conn: imaplib.IMAP4, target_lower: str, sep: str) -> str:
    """Case-insensitive folder name lookup across all folders."""
    for fs, full_name in _list_all_folders(conn):
        if full_name.lower() == target_lower:
            return full_name
    return target_lower  # fall back to what was asked for


def resolve_folder_path(mailbox: IMAPMailbox, path: List[str]) -> IMAPFolder:
    """Resolve a list of path segments to an IMAPFolder.

    Handles:
    - Case-insensitive 'Inbox' → 'INBOX' mapping
    - Server-specific folder separators (/, .)
    - Case-insensitive fallback search if exact match fails
    """
    conn = mailbox._conn
    sep = _get_folder_separator(conn)

    segments = list(path)
    if segments and segments[0].lower() == "inbox":
        segments[0] = "INBOX"

    folder_name = sep.join(segments) if segments else "INBOX"

    # Verify existence; if not found try case-insensitive search
    typ, data = conn.list('""', f'"{folder_name}"')
    if typ != "OK" or not any(data):
        folder_name = _find_folder_icase(conn, folder_name.lower(), sep)

    return IMAPFolder(conn, folder_name)


def _resolve_child(mailbox: IMAPMailbox, parent, name: str) -> IMAPFolder:
    """Interface-compatibility shim (mirrors email_parser_outlook._resolve_child)."""
    conn = mailbox._conn
    sep = _get_folder_separator(conn)
    if isinstance(parent, IMAPFolder):
        return IMAPFolder(conn, parent._folder_name + sep + name)
    return IMAPFolder(conn, name)
