# utils/alerts/store.py
from __future__ import annotations

import csv
import datetime as _dt
import glob
import json
import os
import sqlite3
from typing import Iterable, Optional, Tuple

# ---------------- paths ----------------

def _alerts_dir_for(main_db_path: str) -> str:
    """
    Prefer the project alerts directory exposed by the GUI via BUOY_ALERTS_DIR.
    Fallback: a local 'alerts' folder beside the DB.
    """
    d = os.environ.get("BUOY_ALERTS_DIR", "").strip()
    if not d:
        d = os.path.join(os.path.dirname(os.path.abspath(main_db_path)), "alerts")
    os.makedirs(d, exist_ok=True)
    return d

def get_state_db_path_for(main_db_path: str) -> str:
    """Sidecar state DB lives in alerts/<alerts_state.sqlite>."""
    return os.path.join(_alerts_dir_for(main_db_path), "alerts_state.sqlite")


# ---------------- sqlite helpers ----------------

def _connect(main_db_path: str) -> sqlite3.Connection:
    path = get_state_db_path_for(main_db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return conn

def _utcnow_str() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- schema ----------------

def ensure_alerts_tables(main_db_path: str) -> None:
    conn = _connect(main_db_path)
    cur = conn.cursor()

    # audit trail for settings edits
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_settings_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            table_name TEXT NOT NULL,
            event TEXT NOT NULL,         -- created/updated/deleted/renamed/etc.
            payload_json TEXT            -- snapshot of alert spec or meta
        )
    """)

    # last status per alert
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_last_status (
            table_name TEXT NOT NULL,
            alert_id   TEXT NOT NULL,
            status     TEXT NOT NULL,    -- OFF/GREEN/AMBER/RED
            observed   REAL,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (table_name, alert_id)
        )
    """)

    # flags per alert (for UI badge)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_flags (
            table_name TEXT NOT NULL,
            alert_id   TEXT NOT NULL,
            flagged    INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (table_name, alert_id)
        )
    """)

    # last email per alert (cooldown)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_last_email (
            table_name TEXT NOT NULL,
            alert_id   TEXT NOT NULL,
            status     TEXT NOT NULL,    -- status that triggered the mail
            ts_utc     TEXT NOT NULL,
            PRIMARY KEY (table_name, alert_id)
        )
    """)

    # alerts history log (append-only)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,
            table_name TEXT NOT NULL,
            condition TEXT NOT NULL,     -- e.g. transition:G->A, email:sent, flag:raise, baseline:first...
            threshold REAL,
            observed REAL,
            last_lat REAL,
            last_lon REAL,
            last_time TEXT,              -- UTC timestamp string for 'last observation'
            recipients TEXT,
            map_path TEXT,
            notes TEXT
        )
    """)

    # lightweight current settings blob per table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_current_settings (
            table_name TEXT PRIMARY KEY,
            payload    TEXT,
            updated_utc TEXT
        )
    """)

    # ---- tiny migration: add (alert_id, name, kind) to alerts_log if missing ----
    cols = {r[1] for r in cur.execute("PRAGMA table_info(alerts_log)").fetchall()}
    for col, ddl in [("alert_id", "TEXT"), ("name", "TEXT"), ("kind", "TEXT")]:
        if col not in cols:
            cur.execute(f"ALTER TABLE alerts_log ADD COLUMN {col} {ddl}")

    # helpful indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_log_table_time ON alerts_log(table_name, created_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_last_email_table ON alerts_last_email(table_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_flags_table ON alerts_flags(table_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_last_status_table ON alerts_last_status(table_name)")

    conn.commit()
    conn.close()


# ---------------- audit ----------------

def write_settings_audit(main_db_path: str, table_name: str, event: str, payload_json: str) -> None:
    conn = _connect(main_db_path)
    conn.execute(
        "INSERT INTO alerts_settings_audit (ts_utc, table_name, event, payload_json) VALUES (?, ?, ?, ?)",
        (_utcnow_str(), table_name, event, payload_json),
    )
    conn.commit()
    conn.close()


# ---------------- last status ----------------

def read_last_status(main_db_path: str, table_name: str, alert_id: str) -> Optional[str]:
    conn = _connect(main_db_path)
    row = conn.execute(
        "SELECT status FROM alerts_last_status WHERE table_name=? AND alert_id=?",
        (table_name, alert_id)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def read_last_status_meta(main_db_path: str, table_name: str, alert_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (status, updated_utc) from alerts_last_status.
    updated_utc is a naive UTC string 'YYYY-mm-dd HH:MM:SS' or None.
    """
    conn = _connect(main_db_path)
    row = conn.execute(
        "SELECT status, updated_utc FROM alerts_last_status WHERE table_name=? AND alert_id=?",
        (table_name, alert_id)
    ).fetchone()
    conn.close()
    if not row:
        return (None, None)
    return (row[0], row[1])

def write_last_status(main_db_path: str, table_name: str, alert_id: str, status: str, observed: Optional[float]) -> None:
    conn = _connect(main_db_path)
    conn.execute("""
        INSERT INTO alerts_last_status (table_name, alert_id, status, observed, updated_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(table_name, alert_id) DO UPDATE SET
            status=excluded.status,
            observed=excluded.observed,
            updated_utc=excluded.updated_utc
    """, (table_name, alert_id, status, observed, _utcnow_str()))
    conn.commit()
    conn.close()


# ---------------- flags ----------------

def read_flag(main_db_path: str, table_name: str, alert_id: str) -> Tuple[bool, Optional[str], Optional[int]]:
    conn = _connect(main_db_path)
    row = conn.execute(
        "SELECT flagged, updated_utc FROM alerts_flags WHERE table_name=? AND alert_id=?",
        (table_name, alert_id)
    ).fetchone()
    conn.close()
    if not row:
        return (False, None, None)
    return (bool(row[0]), row[1], row[0])

def set_flag(main_db_path: str, table_name: str, alert_id: str, flagged: bool) -> None:
    conn = _connect(main_db_path)
    conn.execute("""
        INSERT INTO alerts_flags (table_name, alert_id, flagged, updated_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(table_name, alert_id) DO UPDATE SET
            flagged=excluded.flagged,
            updated_utc=excluded.updated_utc
    """, (table_name, alert_id, 1 if flagged else 0, _utcnow_str()))
    conn.commit()
    conn.close()

def count_flagged(main_db_path: str, table_name: str) -> int:
    conn = _connect(main_db_path)
    row = conn.execute(
        "SELECT COUNT(*) FROM alerts_flags WHERE TRIM(table_name)=TRIM(?) AND flagged=1 COLLATE NOCASE",
        (table_name,)
    ).fetchone()
    conn.close()
    return int(row[0] if row else 0)


def clear_all_flags(main_db_path: str, table_name: str) -> None:
    conn = _connect(main_db_path)
    conn.execute(
        "DELETE FROM alerts_flags WHERE TRIM(table_name)=TRIM(?) COLLATE NOCASE",
        (table_name,)
    )
    conn.commit()
    conn.close()



# ---------------- last email ----------------

def read_last_email(main_db_path: str, table_name: str, alert_id: str) -> Tuple[Optional[str], Optional[str]]:
    conn = _connect(main_db_path)
    row = conn.execute(
        "SELECT status, ts_utc FROM alerts_last_email WHERE table_name=? AND alert_id=?",
        (table_name, alert_id)
    ).fetchone()
    conn.close()
    if not row:
        return (None, None)
    return (row[0], row[1])

def write_last_email(main_db_path: str, table_name: str, alert_id: str, status: str) -> None:
    conn = _connect(main_db_path)
    conn.execute("""
        INSERT INTO alerts_last_email (table_name, alert_id, status, ts_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(table_name, alert_id) DO UPDATE SET
            status=excluded.status,
            ts_utc=excluded.ts_utc
    """, (table_name, alert_id, status, _utcnow_str()))
    conn.commit()
    conn.close()


# ---------------- prune history ----------------

def prune_alerts_log(
    main_db_path: str,
    table_name: str,
    keep_days: int = 30,
    max_rows_per_table: int = 50_000
) -> None:
    conn = _connect(main_db_path)
    cur = conn.cursor()

    # time-based prune
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(keep_days))).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("DELETE FROM alerts_log WHERE table_name=? AND created_utc<?", (table_name, cutoff))

    # row-cap prune
    row = cur.execute("SELECT COUNT(*) FROM alerts_log WHERE table_name=?", (table_name,)).fetchone()
    n = int(row[0] if row else 0)
    if n > max_rows_per_table:
        excess = n - max_rows_per_table
        # delete oldest 'excess' rows
        cur.execute("""
            DELETE FROM alerts_log
            WHERE id IN (
                SELECT id FROM alerts_log
                WHERE table_name=?
                ORDER BY id ASC
                LIMIT ?
            )
        """, (table_name, excess))

    conn.commit()
    conn.close()


# ---------------- current settings blob ----------------

def write_current_settings(main_db_path: str, table_name: str, payload_json: str) -> None:
    conn = _connect(main_db_path)
    conn.execute("""
        INSERT INTO alerts_current_settings (table_name, payload, updated_utc)
        VALUES (?, ?, ?)
        ON CONFLICT(table_name) DO UPDATE SET
            payload=excluded.payload,
            updated_utc=excluded.updated_utc
    """, (table_name, payload_json, _utcnow_str()))
    conn.commit()
    conn.close()

def read_current_settings(main_db_path: str, table_name: str):
    conn = _connect(main_db_path)
    row = conn.execute("SELECT payload FROM alerts_current_settings WHERE table_name=?", (table_name,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def purge_orphaned_alert_state(main_db_path: str, table_name: str, valid_ids: Iterable[str]) -> None:
    valid = set(valid_ids or [])
    conn = _connect(main_db_path)
    cur = conn.cursor()

    if not valid:
        cur.execute("DELETE FROM alerts_last_status WHERE table_name=?", (table_name,))
        cur.execute("DELETE FROM alerts_flags WHERE table_name=?", (table_name,))
        cur.execute("DELETE FROM alerts_last_email WHERE table_name=?", (table_name,))
    else:
        # delete where NOT IN valid ids
        qmarks = ",".join("?" for _ in valid)
        params = (table_name, *valid)
        cur.execute(f"DELETE FROM alerts_last_status WHERE table_name=? AND alert_id NOT IN ({qmarks})", params)
        cur.execute(f"DELETE FROM alerts_flags WHERE table_name=? AND alert_id NOT IN ({qmarks})", params)
        cur.execute(f"DELETE FROM alerts_last_email WHERE table_name=? AND alert_id NOT IN ({qmarks})", params)

    conn.commit()
    conn.close()


# ---------------- CSV mirroring ----------------

def _csv_log_dir(main_db_path: str) -> str:
    d = os.path.join(_alerts_dir_for(main_db_path), "log")
    os.makedirs(d, exist_ok=True)
    return d

def append_alert_csv(main_db_path: str, row: dict) -> None:
    """
    Append a single history row to alerts/log/alerts_log-YYYY-MM-DD.csv
    (creates file with header if missing).
    """
    log_dir = _csv_log_dir(main_db_path)
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    path = os.path.join(log_dir, f"alerts_log-{today}.csv")

    field_order = [
        "created_utc", "table_name", "alert_id", "name", "kind",
        "condition", "threshold", "observed", "last_lat", "last_lon", "last_time",
        "recipients", "notes"
    ]
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=field_order, extrasaction="ignore")
        if not exists:
            w.writeheader()
        # coerce None -> ""
        safe = {k: ("" if v is None else v) for k, v in row.items()}
        w.writerow(safe)

def rotate_daily_alert_csvs(main_db_path: str, keep_days: int = 120) -> None:
    """
    Keep the last N days of per-day CSV logs; older files are deleted.
    """
    log_dir = _csv_log_dir(main_db_path)
    pattern = os.path.join(log_dir, "alerts_log-*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        return

    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=int(keep_days))
    for p in files:
        try:
            name = os.path.basename(p)
            # name like alerts_log-YYYY-MM-DD.csv
            date_part = name.split("-", 2)[-1].replace(".csv", "")
            dt = _dt.datetime.strptime(date_part, "%Y-%m-%d")
            if dt < cutoff:
                os.remove(p)
        except Exception:
            pass


__all__ = [
    "ensure_alerts_tables",
    "write_settings_audit",
    "read_last_status", "read_last_status_meta", "write_last_status",
    "read_flag", "set_flag", "count_flagged", "clear_all_flags",
    "read_last_email", "write_last_email",
    "get_state_db_path_for", "prune_alerts_log",
    "write_current_settings", "read_current_settings", "purge_orphaned_alert_state",
    "append_alert_csv", "rotate_daily_alert_csvs",
]
