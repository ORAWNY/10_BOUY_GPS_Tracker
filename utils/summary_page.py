# utils/summary_page.py
from __future__ import annotations

import os
import csv
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime as _dt
from typing import Callable, Optional, Dict, Any, List, Tuple

import pandas as pd

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QBrush, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox, QGridLayout,
    QScrollArea, QComboBox, QMessageBox, QSizePolicy, QFrame, QToolButton, QMenu,
    QTableWidget, QTableWidgetItem, QDialog, QDialogButtonBox
)

from utils.alerts import REGISTRY, AlertSpec
from utils.time_settings import local_zone, parse_series_to_local_naive

# ---- Optional store helpers (gracefully degrade if absent) ----
try:
    from utils.alerts.store import count_flagged as _alerts_count_flagged
except Exception:  # pragma: no cover
    _alerts_count_flagged = None


# ============================= Utilities =============================

def _list_user_tables(db_path: str) -> list[str]:
    """List non-system tables in the SQLite DB."""
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


def _choose_dt_col(conn: sqlite3.Connection, table: str) -> Optional[str]:
    """Pick a likely datetime column for a table."""
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    pref = ["timestamp", "received_time", "datetime", "time", "date"]
    lower = {c.lower(): c for c in cols}
    for name in pref:
        if name in lower:
            return lower[name]
    return cols[0] if cols else None


def _parse_dt_best(s: pd.Series) -> pd.Series:
    """Parse timestamps: treat naive as local; preserve tz-aware by converting to local-naive."""
    return parse_series_to_local_naive(s)


def _norm_status(s: Optional[str]) -> str:
    t = (s or "").strip().lower()
    if t in ("ok", "clear", "good"):
        return "green"
    return t or "unknown"


def _is_greenish(s: Optional[str]) -> bool:
    return _norm_status(s) == "green"


def _is_active_status_obj(st: Any) -> bool:
    """
    Decide if a status object represents an *active* alert.

    Supported conventions:
      - dict with 'active': bool
      - dict with 'resolved': bool (active = not resolved)
      - dict with 'cleared_at' timestamp (active if missing/empty)
      - fall back to string status; non-green => active
    """
    if isinstance(st, dict):
        if "active" in st:
            return bool(st.get("active"))
        if "resolved" in st:
            return not bool(st.get("resolved"))
        if "cleared_at" in st:
            ca = st.get("cleared_at")
            return ca in (None, "", "0001-01-01", "1970-01-01")
        if "status" in st:
            return not _is_greenish(st.get("status"))
        return False
    if isinstance(st, (list, tuple)):
        s = st[0] if st else None
    else:
        s = st
    return not _is_greenish(s)


def _worst_status(statuses: List[str]) -> str:
    order = {"red": 3, "amber": 2, "yellow": 2, "orange": 2, "green": 1, "unknown": 0}
    if not statuses:
        return "unknown"
    return max((_norm_status(s) for s in statuses), key=lambda x: order.get(x, 0))


# ============================ Host shim ==============================

class _LiteHost:
    """
    Minimal Host shim to hand to alert handlers.
    Provides: table_name, df (recent rows), datetime_col and db_path.
    """
    def __init__(self, db_path: str, table: str, df: pd.DataFrame, dt_col: Optional[str]):
        self.db_path = db_path
        self.table_name = table
        self.df = df
        self.datetime_col = dt_col


# ========================= Tile configuration ========================

@dataclass
class SummaryTileConfig:
    table: str

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "SummaryTileConfig":
        return SummaryTileConfig(table=d.get("table", ""))


class SummaryTileDialog(QDialog):
    """Pick a table for a tile."""
    def __init__(self, db_path: str, parent=None, preset: Optional[SummaryTileConfig] = None):
        super().__init__(parent)
        self.setWindowTitle("Add/Edit Summary Tile")
        self.setModal(True)

        v = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Table:", self))
        self.cmb_table = QComboBox(self)
        for t in _list_user_tables(db_path):
            self.cmb_table.addItem(t)
        row.addWidget(self.cmb_table, 1)
        v.addLayout(row)

        info = QLabel(
            "<i>Each tile shows last data time, overall alerts status, and a dropdown of alerts with View/Config.</i>",
            self
        )
        info.setStyleSheet("color:#666;")
        v.addWidget(info)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        v.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        if preset:
            idx = self.cmb_table.findText(preset.table)
            if idx >= 0:
                self.cmb_table.setCurrentIndex(idx)

    def result_config(self) -> SummaryTileConfig:
        return SummaryTileConfig(table=self.cmb_table.currentText())


# ============================== Tile ================================

class SummaryTile(QGroupBox):
    """
    Compact summary tile:
      • Header: table name + flagged count badge + menu
      • Start/Last/Since timestamps
      • Overall alert status (traffic light + text)
      • Collapsible alerts table with per-row [👁 View] and [⚙] buttons
    """

    def __init__(
        self,
        db_path: str,
        cfg: SummaryTileConfig,
        alerts_provider: Optional[Any] = None,
        move_cb: Optional[Callable[[SummaryTile, str], None]] = None,
        remove_cb: Optional[Callable[[SummaryTile], None]] = None,
        edit_cb: Optional[Callable[[SummaryTile], None]] = None,
        parent=None,
    ):
        super().__init__(cfg.table, parent)
        self.db_path = db_path
        self.cfg = cfg
        self.alerts_provider = alerts_provider
        self._move_cb = move_cb
        self._remove_cb = remove_cb
        self._edit_cb = edit_cb

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        g = QGridLayout(self)
        g.setVerticalSpacing(6)
        g.setHorizontalSpacing(10)

        # ----- Header -----
        r = 0
        hdr = QHBoxLayout()
        title_lbl = QLabel(cfg.table, self)
        title_lbl.setStyleSheet("font-weight: 600;")
        hdr.addWidget(title_lbl)

        self.flag_badge = QLabel("⚑ 0", self)
        self.flag_badge.setStyleSheet(
            "padding:2px 6px; border-radius:9px; background:#adb5bd; color:white; font-weight:600;"
        )
        hdr.addSpacing(6)
        hdr.addWidget(self.flag_badge)
        hdr.addStretch(1)

        self.btn_trash = QToolButton(self)
        self.btn_trash.setText("🗑")
        self.btn_trash.setToolTip("Remove tile")
        self.btn_trash.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_trash.clicked.connect(lambda: self._remove_cb(self) if self._remove_cb else None)
        hdr.addWidget(self.btn_trash)

        self.btn_gear = QToolButton(self)
        self.btn_gear.setText("⚙")
        self.btn_gear.setToolTip("Tile menu")
        self.btn_gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gear.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        hdr.addWidget(self.btn_gear)

        g.addLayout(hdr, r, 0, 1, 2)
        r += 1

        # ----- Timestamps -----
        for label in ("Last data time:", "Start data time:", "Time since last:"):
            g.addWidget(QLabel(label, self), r, 0)
            if label.startswith("Last"):
                self.lbl_lastdt = QLabel("—", self)
                self.lbl_lastdt.setStyleSheet("font-weight:600;")
                g.addWidget(self.lbl_lastdt, r, 1)
            elif label.startswith("Start"):
                self.lbl_startdt = QLabel("—", self)
                self.lbl_startdt.setStyleSheet("font-weight:600;")
                g.addWidget(self.lbl_startdt, r, 1)
            else:
                self.lbl_since = QLabel("—", self)
                self.lbl_since.setStyleSheet("font-weight:600;")
                g.addWidget(self.lbl_since, r, 1)
            r += 1

        # ----- Alerts status (traffic light + text) -----
        g.addWidget(QLabel("Alerts status:", self), r, 0)
        row = QHBoxLayout()
        self.light = QLabel("●", self)
        self.light.setFixedWidth(18)
        self.light.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_light_color("#999")
        row.addWidget(self.light)

        self.lbl_alerts = QLabel("", self)
        row.addWidget(self.lbl_alerts, 1)
        g.addLayout(row, r, 1)
        r += 1

        # ----- Alerts table (collapsible) -----
        sep = QFrame(self); sep.setFrameShape(QFrame.Shape.HLine)
        g.addWidget(sep, r, 0, 1, 2); r += 1

        top = QHBoxLayout()
        self.btn_toggle_alerts = QToolButton(self)
        self.btn_toggle_alerts.setText("Alerts ▾")
        self.btn_toggle_alerts.setToolTip("Show/hide alerts table")
        self.btn_toggle_alerts.setCheckable(True)
        self.btn_toggle_alerts.setChecked(False)
        self.btn_toggle_alerts.toggled.connect(self._set_alerts_visible)
        top.addWidget(self.btn_toggle_alerts)
        top.addStretch(1)
        g.addLayout(top, r, 0, 1, 2)
        r += 1

        self.alerts_box = QWidget(self)
        av = QVBoxLayout(self.alerts_box); av.setContentsMargins(0, 0, 0, 0); av.setSpacing(4)
        self.alerts_table = QTableWidget(self.alerts_box)
        self.alerts_table.setColumnCount(6)
        self.alerts_table.setHorizontalHeaderLabels(["Name", "Kind", "Status", "Observed/Summary", "ID", "Actions"])
        self.alerts_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.alerts_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alerts_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.alerts_table.setAlternatingRowColors(True)
        av.addWidget(self.alerts_table)
        g.addWidget(self.alerts_box, r, 0, 1, 2)
        r += 1
        self._set_alerts_visible(False)

        # ----- Gear menu -----
        m = QMenu(self)
        act_edit = QAction("Edit tile…", self)
        act_rm = QAction("Remove tile", self)
        m.addAction(act_edit); m.addAction(act_rm); m.addSeparator()

        mv = m.addMenu("Move tile")
        aL = QAction("◀ Left", self); aR = QAction("▶ Right", self)
        aU = QAction("▲ Up", self);   aD = QAction("▼ Down", self)
        for a in (aL, aR, aU, aD): mv.addAction(a)

        self.btn_gear.setMenu(m)
        act_edit.triggered.connect(lambda: self._edit_cb(self) if self._edit_cb else None)
        act_rm.triggered.connect(lambda: self._remove_cb(self) if self._remove_cb else None)
        aL.triggered.connect(lambda: self._move_cb(self, "left") if self._move_cb else None)
        aR.triggered.connect(lambda: self._move_cb(self, "right") if self._move_cb else None)
        aU.triggered.connect(lambda: self._move_cb(self, "up") if self._move_cb else None)
        aD.triggered.connect(lambda: self._move_cb(self, "down") if self._move_cb else None)

    # ---------- Public ----------

    def refresh(self):
        self._refresh_last_timestamp()
        self._refresh_alert_status()
        self._refresh_alerts_table()

    def config(self) -> SummaryTileConfig:
        return self.cfg

    # ---------- Internals ----------

    def _ensure_time_col(self, df: pd.DataFrame, dt_col: Optional[str]) -> pd.DataFrame:
        """
        Ensure a standard time column '__dt_iso' exists so handler viewers can find it.
        """
        if df is None or df.empty:
            return df
        if "__dt_iso" in df.columns:
            return df

        # Prefer the detected dt_col; else common names (add 'received_time' too)
        candidates = [c for c in [dt_col, "timestamp", "received_time", "datetime", "time", "date", "DateTime"] if c]
        for c in candidates:
            if c in df.columns:
                try:
                    df = df.copy()
                    df["__dt_iso"] = parse_series_to_local_naive(df[c])
                except Exception:
                    pass
                break
        return df

    def _set_light_color(self, css_color: str):
        self.light.setStyleSheet(f"color:{css_color}; font-size: 16px;")

    def _apply_alert_level(self, level: str):
        level = (level or "").lower()
        color = {"green": "#1d9d1d", "amber": "#e3a21a", "yellow": "#e3a21a", "orange": "#e3a21a", "red": "#d13438"}.get(level, "#999")
        self._set_light_color(color)

    def _prov(self, *names: str):
        api = self.alerts_provider
        for nm in names:
            fn = getattr(api, nm, None)
            if callable(fn):
                return fn
        return None

    # ----- timestamps -----

    def _refresh_last_timestamp(self):
        """Compute Start, Last, and Since (now - last) via fast queries; fallback to full scan once."""
        start_dt_str = last_dt_str = since_str = "—"

        def _fmt_td(td: Optional[pd.Timedelta]) -> str:
            if td is None:
                return "—"
            total = int(max(0, td.total_seconds()))
            d, rem = divmod(total, 86400)
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            return f"{d}d {h:02}:{m:02}:{s:02}" if d > 0 else f"{h:02}:{m:02}:{s:02}"

        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                dt_col = _choose_dt_col(conn, self.cfg.table)
                if dt_col:
                    # last
                    try:
                        df_last = pd.read_sql_query(
                            f'SELECT "{dt_col}" FROM "{self.cfg.table}" ORDER BY "{dt_col}" DESC LIMIT 1', conn
                        )
                    except Exception:
                        df_last = pd.DataFrame()
                    # first
                    try:
                        df_first = pd.read_sql_query(
                            f'SELECT "{dt_col}" FROM "{self.cfg.table}" ORDER BY "{dt_col}" ASC LIMIT 1', conn
                        )
                    except Exception:
                        df_first = pd.DataFrame()

                    if not df_last.empty:
                        last_parsed = _parse_dt_best(df_last[dt_col]).dropna()
                        if not last_parsed.empty:
                            last_dt = last_parsed.max()
                            last_dt_str = last_dt.strftime("%Y-%m-%d %H:%M:%S")
                            since_str = _fmt_td(pd.Timestamp.now(tz=local_zone()).tz_localize(None) - last_dt)

                    if not df_first.empty:
                        first_parsed = _parse_dt_best(df_first[dt_col]).dropna()
                        if not first_parsed.empty:
                            start_dt = first_parsed.min()
                            start_dt_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

                    # fallback single scan
                    if start_dt_str == "—" or last_dt_str == "—":
                        df = pd.read_sql_query(f'SELECT "{dt_col}" FROM "{self.cfg.table}"', conn)
                        if not df.empty:
                            all_parsed = _parse_dt_best(df[dt_col]).dropna()
                            if not all_parsed.empty:
                                if start_dt_str == "—":
                                    start_dt = all_parsed.min()
                                    start_dt_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                                if last_dt_str == "—":
                                    last_dt = all_parsed.max()
                                    last_dt_str = last_dt.strftime("%Y-%m-%d %H:%M:%S")
                                    since_str = _fmt_td(pd.Timestamp.now(tz=local_zone()).tz_localize(None) - last_dt)
        except Exception:
            pass

        self.lbl_lastdt.setText(last_dt_str)
        self.lbl_startdt.setText(start_dt_str)
        self.lbl_since.setText(since_str)

    # ----- status & flags -----

    def _count_flags(self) -> int:
        """Return flagged count for this tile's table."""
        table = self.cfg.table
        # 1) DB-backed count (respects manual clears)
        if _alerts_count_flagged is not None:
            try:
                return int(_alerts_count_flagged(self.db_path, table) or 0)
            except Exception:
                pass
        # 2) provider fallback
        try:
            fn = self._prov("count_flagged", "count_flags", "flagged_count")
            if fn:
                return int(fn(table) or 0)
        except Exception:
            pass
        return 0

    def _overall_status_and_counts(self) -> Tuple[str, int]:
        """
        Returns (overall_status, count_non_green_active), using provider hooks if possible.
        """
        table = self.cfg.table
        statuses: List[str] = []

        try:
            lst = self._prov("list_alerts", "alerts_for_table")
            get = self._prov("alert_status", "get_alert_status", "status_for_alert", "alert_last_status")
            if lst and get:
                for it in list(lst(table) or []):
                    key = it.get("id") or it.get("name") or ""
                    if not key:
                        continue
                    st = get(table, key)
                    if not _is_active_status_obj(st):
                        continue
                    if isinstance(st, dict):
                        statuses.append(_norm_status(st.get("status")))
                    elif isinstance(st, (list, tuple)):
                        statuses.append(_norm_status(st[0] if st else None))
                    else:
                        statuses.append(_norm_status(st))
        except Exception:
            statuses = []

        if not statuses:
            return ("green", 0)
        non_green_active = sum(1 for s in statuses if not _is_greenish(s))
        return _worst_status(statuses), non_green_active

    def _refresh_alert_status(self):
        level, non_green_active = self._overall_status_and_counts()
        self._apply_alert_level(level)

        flags = self._count_flags()
        self.flag_badge.setText(f"⚑ {int(flags)}")
        self.flag_badge.setStyleSheet(
            "padding:2px 6px; border-radius:9px; "
            f"background:{'#f03e3e' if flags else '#adb5bd'}; color:white; font-weight:600;"
        )
        self.flag_badge.setToolTip(f"{self.cfg.table}: {flags} flagged")

        self.lbl_alerts.setText("OK" if (level == "green" and non_green_active == 0)
                                else f"{level.upper()} — {non_green_active} active alert(s)")

    # ----- alerts table -----

    def _fetch_alert_rows(self) -> List[Dict[str, Any]]:
        """
        Build table rows. Prefer a provider's direct table; otherwise compose from definitions + status calls,
        and fall back to evaluating handlers when needed.
        """
        table = self.cfg.table
        rows: List[Dict[str, Any]] = []

        # 1) Direct provider table
        for fn_name in ("get_alerts_table", "read_alerts_table", "alerts_table", "alerts_dataframe"):
            fn = self._prov(fn_name)
            if fn:
                try:
                    data = fn(table)
                    if isinstance(data, pd.DataFrame):
                        for _, r in data.iterrows():
                            st = r.get("status")
                            rows.append({
                                "name": r.get("name") or r.get("id") or "",
                                "kind": r.get("kind") or "",
                                "status": _norm_status(st),
                                "obs": r.get("summary") or r.get("observed") or "",
                                "id": r.get("id") or r.get("name") or "",
                            })
                    elif isinstance(data, list):
                        for it in data:
                            st = it.get("status")
                            rows.append({
                                "name": it.get("name") or it.get("id") or "",
                                "kind": it.get("kind") or "",
                                "status": _norm_status(st),
                                "obs": it.get("summary") or it.get("observed") or "",
                                "id": it.get("id") or it.get("name") or "",
                            })
                    if rows:
                        return rows
                except Exception:
                    pass

        # 2) Compose from list_alerts + status (fallback to handler.evaluate)
        lst = self._prov("list_alerts", "alerts_for_table")
        get = self._prov("alert_status", "get_alert_status", "status_for_alert", "alert_last_status")
        try:
            items = list(lst(table) or []) if lst else []
        except Exception:
            items = []

        for it in items:
            key = it.get("id") or it.get("name") or ""
            nm = it.get("name") or key or "Alert"
            kd = it.get("kind") or ""
            status = "unknown"; obs = ""

            st_full: Any = None
            if get and key:
                try:
                    st_full = get(table, key)
                except Exception:
                    st_full = None

            if st_full is None:
                # Evaluate handler as a last resort
                try:
                    spec = AlertSpec.from_dict(it)
                    status, obs = self._eval_fallback(spec)
                except Exception:
                    pass
            else:
                if isinstance(st_full, dict):
                    status = _norm_status(st_full.get("status"))
                    obs = st_full.get("summary") or st_full.get("observed") or ""
                elif isinstance(st_full, tuple):
                    status = _norm_status(st_full[0] if st_full else None)
                    if len(st_full) > 1:
                        obs = st_full[1] if st_full[1] is not None else ""
                else:
                    status = _norm_status(st_full)

            rows.append({"name": nm, "kind": kd, "status": status, "obs": obs, "id": key})

        return rows

    def _refresh_alerts_table(self):
        rows = self._fetch_alert_rows()
        self.alerts_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            name = QTableWidgetItem(str(r.get("name", "")))
            kind = QTableWidgetItem(str(r.get("kind", "")))
            status = QTableWidgetItem(str(r.get("status", "")))
            obs = QTableWidgetItem(str(r.get("obs", "")))
            rid = QTableWidgetItem(str(r.get("id", "")))
            rid.setData(Qt.ItemDataRole.UserRole, r.get("id", ""))

            # traffic light cell
            color_map = {"green": "#37b24d", "amber": "#f59f00", "yellow": "#f59f00", "orange": "#f59f00", "red": "#f03e3e"}
            css = color_map.get(str(r.get("status", "")).lower())
            if css:
                status.setForeground(QBrush(QColor("#ffffff")))
                status.setBackground(QBrush(QColor(css)))

            for it in (name, kind, status, obs, rid):
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.alerts_table.setItem(i, 0, name)
            self.alerts_table.setItem(i, 1, kind)
            self.alerts_table.setItem(i, 2, status)
            self.alerts_table.setItem(i, 3, obs)
            self.alerts_table.setItem(i, 4, rid)

            # Actions
            actions = QWidget(self.alerts_table)
            lay = QHBoxLayout(actions); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)

            btn_view = QToolButton(actions); btn_view.setText("👁 View"); btn_view.setToolTip("View this alert")
            btn_view.clicked.connect(lambda _=None, aid=r.get("id", ""): self._on_view_alert(aid))

            btn_cfg = QToolButton(actions); btn_cfg.setText("⚙"); btn_cfg.setToolTip("Configure this alert")
            btn_cfg.clicked.connect(lambda _=None, aid=r.get("id", ""): self._on_config_alert(aid))

            lay.addWidget(btn_view); lay.addWidget(btn_cfg); lay.addStretch(1)
            self.alerts_table.setCellWidget(i, 5, actions)

        self.alerts_table.resizeColumnsToContents()
        self.alerts_table.horizontalHeader().setStretchLastSection(True)

    def _set_alerts_visible(self, visible: bool):
        self.alerts_box.setVisible(visible)
        self.btn_toggle_alerts.setText("Alerts ▴" if visible else "Alerts ▾")

    # ----- per-row actions via handlers -----

    def _provider_host(self) -> Optional[object]:
        """Ask provider for a Host instance if it has one."""
        for name in ("host_for_table", "get_host_for_table", "get_host", "host"):
            fn = self._prov(name)
            if fn:
                try:
                    return fn(self.cfg.table)
                except Exception:
                    pass
        return None

    def _read_recent_df(self, limit_rows: int = 50_000) -> tuple[pd.DataFrame, Optional[str]]:
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                dt_col = _choose_dt_col(conn, self.cfg.table)
                if dt_col:
                    try:
                        q = f'SELECT * FROM "{self.cfg.table}" ORDER BY "{dt_col}" DESC LIMIT {int(limit_rows)}'
                        df = pd.read_sql_query(q, conn)
                        if not df.empty:
                            df = df.iloc[::-1].reset_index(drop=True)  # chronological
                    except Exception:
                        df = pd.read_sql_query(f'SELECT * FROM "{self.cfg.table}"', conn)
                else:
                    df = pd.read_sql_query(f'SELECT * FROM "{self.cfg.table}"', conn)

                # NEW: normalize time column for viewers
                df = self._ensure_time_col(df, dt_col)
                return df, dt_col
        except Exception:
            return pd.DataFrame(), None

    def _load_spec(self, alert_id: str) -> Optional[AlertSpec]:
        """Get an AlertSpec by id via provider; fallback to scanning list_alerts."""
        for name in ("get_alert_spec", "read_alert_spec", "get_alert", "read_alert"):
            fn = self._prov(name)
            if fn:
                try:
                    try:
                        obj = fn(self.cfg.table, alert_id)
                    except TypeError:
                        obj = fn(alert_id)
                    if isinstance(obj, AlertSpec):
                        return obj
                    if isinstance(obj, dict):
                        return AlertSpec.from_dict(obj)
                except Exception:
                    pass

        lst = self._prov("list_alerts", "alerts_for_table")
        if lst:
            try:
                for d in list(lst(self.cfg.table) or []):
                    if d.get("id") == alert_id or d.get("name") == alert_id:
                        return AlertSpec.from_dict(d)
            except Exception:
                pass
        return None

    def _open_via_handler(self, spec: AlertSpec, mode: str):
        if not spec or not spec.kind:
            QMessageBox.information(self, "Alert", "Alert definition not found.")
            return

        handler = REGISTRY.get(spec.kind)
        if not handler:
            QMessageBox.information(self, "Alert", f"No handler registered for kind: {spec.kind}")
            return

        host = self._provider_host()
        df_obj = getattr(host, "df", None) if host else None

        # If provider host has no data, load a recent slice ourselves
        if (df_obj is None) or (getattr(df_obj, "empty", True)):
            df, dt_col = self._read_recent_df()
            host = _LiteHost(self.db_path, self.cfg.table, df, dt_col)
        else:
            # Provider host has a df; ensure it exposes a recognizable time column
            try:
                if "__dt_iso" not in host.df.columns:
                    dt_hint = getattr(host, "datetime_col", None)
                    host.df = self._ensure_time_col(host.df, dt_hint)
            except Exception:
                pass

        try:
            if mode == "view":
                if hasattr(handler, "create_viewer"):
                    dlg = handler.create_viewer(spec, host, self)
                    if isinstance(dlg, QDialog):
                        dlg.exec()
                else:
                    QMessageBox.information(self, "View", f"{spec.kind} has no viewer.")
            else:
                if hasattr(handler, "create_editor"):
                    dlg = handler.create_editor(spec, host, self)
                    if isinstance(dlg, QDialog) and dlg.exec():
                        # persist via provider if available
                        for name in ("save_alert_spec", "update_alert_spec", "update_alert", "save_alert"):
                            fn = self._prov(name)
                            if fn:
                                try:
                                    try:
                                        fn(self.cfg.table, spec.to_dict())
                                    except Exception:
                                        fn(self.cfg.table, spec)
                                except Exception as e:
                                    QMessageBox.warning(self, "Save alert", f"Provider error while saving:\n{e}")
                                break
                else:
                    QMessageBox.information(self, "Configure", f"{spec.kind} has no editor.")
        except Exception as e:
            QMessageBox.critical(self, "Alert", str(e))

    def _on_view_alert(self, alert_id: str):
        spec = self._load_spec(alert_id)
        self._open_via_handler(spec, "view")

    def _on_config_alert(self, alert_id: str):
        spec = self._load_spec(alert_id)
        self._open_via_handler(spec, "edit")

    def _eval_fallback(self, spec: AlertSpec) -> tuple[str, str]:
        """Evaluate via handler when provider doesn't expose a status; returns (status, obs/summary)."""
        try:
            handler = REGISTRY.get(spec.kind)
            if not handler:
                return "unknown", ""
            host = self._provider_host()
            if host is None:
                df, dt_col = self._read_recent_df()
                host = _LiteHost(self.db_path, self.cfg.table, df, dt_col)
            res = handler.evaluate(spec, host)
            st = res.get("status")
            st = getattr(st, "value", st)  # Status enum -> str
            return _norm_status(st), str(res.get("summary") or res.get("observed") or "")
        except Exception:
            return "unknown", ""


# ============================= Board ================================

class SummaryBoard(QWidget):
    """Grid of tiles with simple add/edit/move/remove and refresh hooks."""
    def __init__(
        self,
        db_path: str,
        alerts_provider: Optional[Any] = None,
        parent=None,
        changed_cb: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.alerts_provider = alerts_provider
        self._changed_cb = changed_cb

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # Controls
        ctrl = QHBoxLayout()
        self.btn_add = QPushButton("Add tile…", self)
        ctrl.addWidget(self.btn_add)

        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("Layout:", self))
        self.cmb_layout = QComboBox(self)
        self.cmb_layout.addItem("Single column", userData=1)
        self.cmb_layout.addItem("Two columns", userData=2)
        self.cmb_layout.setCurrentIndex(1)  # default two columns
        self.cmb_layout.currentIndexChanged.connect(self._layout_changed)
        ctrl.addWidget(self.cmb_layout)

        ctrl.addStretch(1)
        v.addLayout(ctrl)

        # Grid
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(10)
        v.addLayout(self.grid)

        self.tiles: List[SummaryTile] = []
        self.max_cols = 2

        self.btn_add.clicked.connect(self._add_tile_dialog)

    # ---- change notifications ----
    def _notify_changed(self):
        if callable(self._changed_cb):
            try:
                self._changed_cb()
            except Exception:
                pass

    # ---- tiles CRUD ----
    def _add_tile_dialog(self):
        dlg = SummaryTileDialog(self.db_path, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.add_tile(dlg.result_config())

    def add_tile(self, cfg: SummaryTileConfig):
        tile = SummaryTile(
            self.db_path, cfg, alerts_provider=self.alerts_provider,
            move_cb=self._move_tile, remove_cb=self._remove_tile, edit_cb=self._edit_tile, parent=self
        )
        self.tiles.append(tile)
        self._regrid()
        self._notify_changed()
        tile.refresh()
        return tile

    def _remove_tile(self, tile: SummaryTile):
        if tile in self.tiles:
            self.tiles.remove(tile)
            tile.setParent(None)
            tile.deleteLater()
            self._regrid()
            self._notify_changed()

    def _edit_tile(self, tile: SummaryTile):
        dlg = SummaryTileDialog(self.db_path, self, preset=tile.config())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            tile.cfg = dlg.result_config()
            tile.setTitle(tile.cfg.table)
            tile.refresh()
            self._notify_changed()

    def _move_tile(self, tile: SummaryTile, direction: str):
        if tile not in self.tiles:
            return
        i = self.tiles.index(tile)
        if direction == "left" and i > 0:
            self.tiles[i - 1], self.tiles[i] = self.tiles[i], self.tiles[i - 1]
        elif direction == "right" and i < len(self.tiles) - 1:
            self.tiles[i], self.tiles[i + 1] = self.tiles[i + 1], self.tiles[i]
        elif direction == "up":
            j = i - self.max_cols
            if j >= 0:
                self.tiles.insert(j, self.tiles.pop(i))
        elif direction == "down":
            j = i + self.max_cols
            if j < len(self.tiles):
                self.tiles.insert(j, self.tiles.pop(i))
        self._regrid()
        self._notify_changed()

    def _layout_changed(self, *_):
        cols = int(self.cmb_layout.currentData() or 2)
        if cols != self.max_cols:
            self.max_cols = cols
            self._regrid()
        self._notify_changed()

    def _regrid(self):
        # clear existing widgets from grid
        for pos in reversed(range(self.grid.count())):
            w = self.grid.itemAt(pos).widget()
            if w:
                self.grid.removeWidget(w)
        # re-add in row-major order
        r = c = 0
        for t in self.tiles:
            self.grid.addWidget(t, r, c)
            c += 1
            if c >= self.max_cols:
                c = 0; r += 1

    # ---- refresh & persistence ----
    def refresh_all(self):
        for t in self.tiles:
            t.refresh()

    def export_state(self) -> Dict[str, Any]:
        return {"tiles": [t.config().to_json() for t in self.tiles], "max_cols": self.max_cols}

    def import_state(self, data: Dict[str, Any]):
        self.max_cols = int(data.get("max_cols", 2))
        idx = 0 if self.max_cols == 1 else 1
        self.cmb_layout.setCurrentIndex(idx)

        for t in list(self.tiles):
            self._remove_tile(t)
        for obj in data.get("tiles", []):
            cfg = SummaryTileConfig.from_json(obj)
            self.add_tile(cfg)
        self._notify_changed()


# ============================= Page ================================

class SummaryPage(QWidget):
    """
    Summary page:
      • Flagged alerts badge across all tiles
      • Quick Refresh / Export buttons
      • Grid of SummaryTiles (layout: 1 or 2 columns)
      • Per-row actions call each alert handler’s own viewer/editor
    """
    def __init__(self, db_path: str, alerts_provider: Optional[Any] = None, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.alerts_provider = alerts_provider

        # Scrollable content
        scroll = QScrollArea(self); scroll.setWidgetResizable(True)
        content = QWidget()
        root = QVBoxLayout(content); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(8)

        # Badge + controls
        badge_row = QHBoxLayout()
        self.badge_lbl = QLabel("⚑ 0 flagged alerts in view")
        self.badge_lbl.setStyleSheet(
            "padding:4px 8px; border-radius:10px; background:#adb5bd; color:white; font-weight:bold;"
        )
        badge_row.addWidget(self.badge_lbl)

        self.btn_refresh = QPushButton("Refresh", self)
        self.btn_refresh.clicked.connect(self._refresh_board)
        badge_row.addWidget(self.btn_refresh)

        self.btn_export = QPushButton("Export report", self)
        self.btn_export.setToolTip("Export alerts definitions and history to the project's alerts/ folder")
        self.btn_export.clicked.connect(self._on_export_report)
        badge_row.addWidget(self.btn_export)

        badge_row.addStretch(1)
        root.addLayout(badge_row)

        # Board
        self.board = SummaryBoard(self.db_path, alerts_provider=self.alerts_provider,
                                  parent=content, changed_cb=self._refresh_badge)
        root.addWidget(self.board, 1)

        content.setLayout(root)
        scroll.setWidget(content)

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(scroll)
        self.setLayout(outer)

        # Initial + immediate refresh
        self._refresh_board()
        QTimer.singleShot(0, self._refresh_board)

        # Lightweight auto refresh
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(4000)  # feel free to increase if you want less churn
        self._auto_timer.timeout.connect(self._refresh_board)
        self._auto_timer.start()

    # ---- flagged badge ----

    def _refresh_badge(self):
        def flagged_count(table: str) -> int:
            # 1) store-backed (respects manual unflags)
            if _alerts_count_flagged is not None:
                try:
                    return int(_alerts_count_flagged(self.db_path, table) or 0)
                except Exception:
                    pass
            # 2) provider fallback
            try:
                api = self.alerts_provider
                if api and hasattr(api, "count_flagged") and callable(api.count_flagged):
                    return int(api.count_flagged(table) or 0)
            except Exception:
                pass
            return 0

        tables = [t.config().table for t in self.board.tiles]
        per_table = [(t, flagged_count(t)) for t in tables]
        total = sum(n for _, n in per_table)

        self.badge_lbl.setText(f"⚑ {total} flagged alerts in view")
        tip = "\n".join(f"{t}: {n}" for t, n in per_table) or "No tiles"
        self.badge_lbl.setToolTip(tip)
        self.badge_lbl.setStyleSheet(
            "padding:4px 8px; border-radius:10px; "
            f"background:{'#f03e3e' if total else '#adb5bd'}; color:white; font-weight:bold;"
        )

    # ---- refresh hooks ----

    def _refresh_board(self):
        self.board.refresh_all()
        self._refresh_badge()

    # ---- export report ----

    def _on_export_report(self):
        files = self.export_alerts_report()
        msg = "No alerts/history found." if not files else "Saved:\n" + "\n".join(files)
        QMessageBox.information(self, "Export Alerts Report", msg)

    def export_alerts_report(self, *, out_dir: Optional[str] = None) -> List[str]:
        """
        Export alerts definitions and history for all tiles into alerts/ folder.
        Returns list of file paths written.
        """
        api = self.alerts_provider
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        root = out_dir or os.path.join(os.path.dirname(self.db_path), "alerts")
        os.makedirs(root, exist_ok=True)
        written: List[str] = []

        for tile in self.board.tiles:
            table = tile.config().table

            # 1) Definitions
            defs: List[dict] = []
            try:
                lst = getattr(api, "list_alerts", None) or getattr(api, "alerts_for_table", None)
                if callable(lst):
                    defs = list(lst(table) or [])
            except Exception:
                defs = []

            defs_path = os.path.join(root, f"{table}_alerts_{ts}.csv")
            if defs:
                cols = sorted({k for d in defs for k in d.keys()})
                with open(defs_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=cols)
                    w.writeheader()
                    for d in defs:
                        w.writerow(d)
                written.append(defs_path)

            # 2) History (provider-based)
            hist: List[dict] = []
            for fn_name in ("get_alerts_history", "history_for_table", "alerts_history"):
                fn = getattr(api, fn_name, None)
                if callable(fn):
                    try:
                        hist = list(fn(table) or [])
                        break
                    except Exception:
                        pass

            # normalize/enrich
            for h in hist:
                h["table"] = table
                h["alert_id"] = h.get("id") or h.get("alert_id")
                h["name"] = h.get("name")
                h["kind"] = h.get("kind")
                h["status"] = _norm_status(h.get("status"))
                h["active"] = bool(h.get("active")) if "active" in h else (not bool(h.get("resolved", False)))
                h["threshold"] = h.get("threshold") or h.get("threshold_value") or h.get("limit") or ""
                h["observed"] = h.get("observed") or h.get("value") or h.get("last_value") or ""
                for key in ("raised_at", "cleared_at", "timestamp", "occurred_at"):
                    if key in h and h[key] and not isinstance(h[key], str):
                        try:
                            h[key] = h[key].strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            h[key] = str(h[key])

            if hist:
                cols = sorted({k for d in hist for k in d.keys()})
                hist_path = os.path.join(root, f"{table}_history_{ts}.csv")
                with open(hist_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=cols)
                    w.writeheader()
                    for d in hist:
                        w.writerow(d)
                written.append(hist_path)

        return written

    # ---- persistence for ProjectData.save/load ----

    def export_state(self) -> Dict[str, Any]:
        data = self.board.export_state()
        data["max_cols"] = self.board.max_cols
        return data

    def import_state(self, data: Dict[str, Any]):
        self.board.import_state(data)
        self._refresh_board()
