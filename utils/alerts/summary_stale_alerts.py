# utils/summary_stale_alerts.py
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple, Callable

import pandas as pd

from PyQt6.QtWidgets import QDialog, QMessageBox

from utils.time_settings import local_zone, parse_series_to_local_naive
from utils.alerts import REGISTRY, AlertSpec


# -------------------------
# Minimal host object (what StaleHandler expects)
# -------------------------

@dataclass
class LiteHost:
    db_path: str
    table_name: str
    df: pd.DataFrame
    datetime_col: str


# -------------------------
# Defaults / payload helpers
# -------------------------

def default_payload() -> Dict[str, Any]:
    """
    Single source of truth for Summary page stale settings.
    Safe to include extra keys; the Stale handler ignores unknown keys.
    """
    return {
        "amber_min": 30,
        "red_min": 60,
        "scope_all": False,  # False → since last; True → max historical gap
        "interval_min": 15,
        "email_cooldown_min": 240,

        # end-user controls
        "email_on_amber": True,
        "email_on_red": True,

        # existing controls
        "email_on_escalation": True,
        "email_on_recovery": False,

        "recipients": [],
        "threshold_min": 30,  # legacy
        "also_tables": [],
    }


def normalize_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = default_payload()
    if isinstance(payload, dict):
        base.update(payload)

    # keep legacy key consistent
    try:
        base["amber_min"] = int(base.get("amber_min") or base.get("threshold_min") or 30)
    except Exception:
        base["amber_min"] = 30

    try:
        base["red_min"] = int(base.get("red_min") or max(int(base["amber_min"]) * 2, 60))
    except Exception:
        base["red_min"] = max(int(base["amber_min"]) * 2, 60)

    if base["red_min"] < base["amber_min"]:
        base["red_min"] = base["amber_min"]

    base["threshold_min"] = int(base["amber_min"])
    return base


def build_spec_for_table(table: str, payload: Dict[str, Any]) -> AlertSpec:
    payload = normalize_payload(payload)
    return AlertSpec(
        id=table,
        kind="Stale",
        name=f"Since last — {table}",
        enabled=True,
        recipients=list(payload.get("recipients", []) or []),
        payload=payload,
    )


# -------------------------
# SQLite helpers (kept out of SummaryPage)
# -------------------------

def list_user_tables(db_path: str) -> List[str]:
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def choose_dt_col(conn: sqlite3.Connection, table: str) -> Optional[str]:
    cols = _table_columns(conn, table)
    pref = ["timestamp", "received_time", "datetime", "time", "date"]
    lower = {c.lower(): c for c in cols}
    for name in pref:
        if name in lower:
            return lower[name]
    return cols[0] if cols else None


def build_host(db_path: str, table: str, *, sample_limit: int, for_viewer: bool) -> Optional[LiteHost]:
    """
    Build what the Stale handler needs.
    for_viewer=True -> larger sample for charting
    for_viewer=False -> smaller sample for fast evaluation
    """
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            dt_col = choose_dt_col(conn, table)
            if not dt_col:
                return None

            lim = int(sample_limit or 20000) if for_viewer else 5000
            lim = max(2000, min(lim, 200000))

            q = f'SELECT "{dt_col}" FROM "{table}" ORDER BY "{dt_col}" DESC LIMIT {lim}'
            df = pd.read_sql_query(q, conn)

            if df.empty:
                return LiteHost(db_path, table, df, dt_col)

            # chronological
            df = df.iloc[::-1].reset_index(drop=True)
            df[dt_col] = parse_series_to_local_naive(df[dt_col])
            return LiteHost(db_path, table, df, dt_col)
    except Exception:
        return None


# -------------------------
# Status mapping (for Summary colors)
# -------------------------

def status_to_level(st: Any) -> str:
    if st is None:
        return "unknown"
    if hasattr(st, "value"):
        st = st.value
    s = str(st).strip().upper()
    if s in {"GREEN", "AMBER", "RED"}:
        return s.lower()
    if s == "OFF":
        return "off"
    return "unknown"


def fallback_level_from_last_dt(last_dt: Optional[pd.Timestamp], payload: Dict[str, Any]) -> str:
    if last_dt is None or pd.isna(last_dt):
        return "unknown"

    payload = normalize_payload(payload)
    now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)
    since_s = float((now_local - last_dt).total_seconds())

    amb = int(payload.get("amber_min", 30))
    red = int(payload.get("red_min", max(amb * 2, 60)))
    if red < amb:
        red = amb

    if since_s >= float(red * 60):
        return "red"
    if since_s >= float(amb * 60):
        return "amber"
    return "green"


# -------------------------
# Handler access + evaluation
# -------------------------

def get_handler():
    return REGISTRY.get("Stale")


def evaluate_table(
    *,
    db_path: str,
    table: str,
    payload: Dict[str, Any],
    sample_limit: int,
) -> Tuple[str, str]:
    """
    Returns (level, tooltip_summary).
    """
    payload = normalize_payload(payload)
    handler = get_handler()
    if not handler:
        return "unknown", "Stale handler not registered. Import utils.alerts.stale_alert."

    host = build_host(db_path, table, sample_limit=sample_limit, for_viewer=False)
    if host is None:
        return "unknown", "Could not load host data."

    spec = build_spec_for_table(table, payload)
    try:
        res = handler.evaluate(spec, host)
        level = status_to_level(res.get("status"))
        tip = str(res.get("summary") or "") or "Click to view chart and edit thresholds."
        return level, tip
    except Exception:
        # safe fallback
        return fallback_level_from_last_dt(getattr(host, "df", pd.DataFrame()).get(getattr(host, "datetime_col", ""), pd.Series()).max() if hasattr(host, "df") else None, payload), \
               "Click to view chart and edit thresholds."


# -------------------------
# Viewer/editor launching (Summary calls this; stays skinny)
# -------------------------

def open_viewer_dialog(
    *,
    parent,
    db_path: str,
    table: str,
    payload: Dict[str, Any],
    sample_limit: int,
    configure_spec_callback: Optional[Callable[[AlertSpec], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Opens the Stale viewer dialog for this table.
    Returns updated payload if user edited settings, else None.

    configure_spec_callback: if provided, StaleViewDialog calls parent.configure_spec(spec);
    we can provide a callback by temporarily attaching it to the parent.
    """
    handler = get_handler()
    if not handler:
        QMessageBox.information(parent, "Since last", "Stale handler not registered.")
        return None

    host = build_host(db_path, table, sample_limit=sample_limit, for_viewer=True)
    if host is None:
        QMessageBox.information(parent, "Since last", f"Could not load data for {table}.")
        return None

    spec = build_spec_for_table(table, payload)

    # Optional bridge: StaleViewDialog tries parent.configure_spec(spec)
    restore = None
    if configure_spec_callback is not None:
        restore = getattr(parent, "configure_spec", None)
        setattr(parent, "configure_spec", configure_spec_callback)

    try:
        dlg = handler.create_viewer(spec, host, parent)
        if isinstance(dlg, QDialog):
            dlg.exec()
    finally:
        if configure_spec_callback is not None:
            if restore is None:
                try:
                    delattr(parent, "configure_spec")
                except Exception:
                    pass
            else:
                setattr(parent, "configure_spec", restore)

    return dict(spec.payload or {})


# -------------------------
# Notification logic (pluggable)
# -------------------------

@dataclass
class NotifyDecision:
    should_send: bool
    reason: str
    new_state: Dict[str, Any]


def decide_notification(
    *,
    table: str,
    old_state: Dict[str, Any],
    new_level: str,
    payload: Dict[str, Any],
    now_utc: Optional[pd.Timestamp] = None,
) -> NotifyDecision:
    """
    Pure decision function. No email sent here.

    old_state is per-table persisted state, e.g.
      {"last_level": "green", "last_email_utc": "..."}
    """
    payload = normalize_payload(payload)
    lvl = (new_level or "unknown").lower()

    prev = str(old_state.get("last_level") or "unknown").lower()
    cooldown_min = int(payload.get("email_cooldown_min", 240) or 0)

    # toggles
    email_on_amber = bool(payload.get("email_on_amber", True))
    email_on_red = bool(payload.get("email_on_red", True))
    email_on_escalation = bool(payload.get("email_on_escalation", True))
    email_on_recovery = bool(payload.get("email_on_recovery", False))

    if now_utc is None:
        now_utc = pd.Timestamp.utcnow()

    last_email_utc = old_state.get("last_email_utc")
    if last_email_utc:
        try:
            last_email_utc = pd.Timestamp(last_email_utc)
        except Exception:
            last_email_utc = None

    # cooldown gate
    if cooldown_min > 0 and last_email_utc is not None:
        mins = (now_utc - last_email_utc).total_seconds() / 60.0
        if mins < cooldown_min:
            return NotifyDecision(
                should_send=False,
                reason=f"cooldown active ({mins:.1f} < {cooldown_min} min)",
                new_state={"last_level": lvl, "last_email_utc": old_state.get("last_email_utc")},
            )

    # transition detection
    def rank(x: str) -> int:
        return {"green": 0, "amber": 1, "red": 2}.get(x, -1)

    prev_r = rank(prev)
    new_r = rank(lvl)

    # unknown/off: don’t spam by default
    if new_r < 0:
        return NotifyDecision(False, "non-actionable status", {"last_level": lvl, "last_email_utc": old_state.get("last_email_utc")})

    should = False
    why = "no trigger"

    if prev_r < 0:
        # first meaningful status: treat like "entering"
        if lvl == "amber" and email_on_amber:
            should, why = True, "enter amber (first status)"
        elif lvl == "red" and email_on_red:
            should, why = True, "enter red (first status)"
    else:
        if prev == "green" and lvl == "amber" and email_on_amber:
            should, why = True, "enter amber"
        elif prev == "green" and lvl == "red" and email_on_red:
            should, why = True, "enter red"
        elif prev == "amber" and lvl == "red" and email_on_escalation:
            should, why = True, "escalation amber→red"
        elif prev in {"amber", "red"} and lvl == "green" and email_on_recovery:
            should, why = True, "recovery → green"

    new_state = {"last_level": lvl, "last_email_utc": old_state.get("last_email_utc")}
    if should:
        new_state["last_email_utc"] = now_utc.isoformat()

    return NotifyDecision(should_send=should, reason=why, new_state=new_state)