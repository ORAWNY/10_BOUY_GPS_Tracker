# utils/summary_page.py
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtCore import Qt, QTimer, QRect

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHeaderView,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QFormLayout,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)

from utils.time_settings import local_zone, parse_series_to_local_naive
from utils.alerts import REGISTRY, AlertSpec
from utils.alerts import summary_stale_alerts as stale_mod

# Force registration of the Stale handler (ensures REGISTRY["Stale"] exists)
try:
    from utils.alerts import stale_alert as _stale_alert  # noqa: F401
except Exception:
    _stale_alert = None


# ------------------------- DB helpers -------------------------


def _list_user_tables(db_path: str) -> list[str]:
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


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def _choose_dt_col(conn: sqlite3.Connection, table: str) -> Optional[str]:
    cols = _table_columns(conn, table)
    pref = ["timestamp", "received_time", "datetime", "time", "date"]
    lower = {c.lower(): c for c in cols}
    for name in pref:
        if name in lower:
            return lower[name]
    return cols[0] if cols else None


def _fmt_dt(x: Optional[pd.Timestamp]) -> str:
    if x is None or pd.isna(x):
        return "—"
    try:
        return pd.Timestamp(x).to_pydatetime().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(x)


def _fmt_td(td: Optional[pd.Timedelta]) -> str:
    if td is None or pd.isna(td):
        return "—"
    total = int(max(0, td.total_seconds()))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h:02}:{m:02}:{s:02}" if d > 0 else f"{h:02}:{m:02}:{s:02}"


def _status_to_level(st: Any) -> str:
    """Map Status enum (or string) to 'green/amber/red/off/unknown'."""
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


def _status_color(level: str) -> QColor:
    lvl = (level or "").strip().lower()
    css = {
        "green": "#2f9e44",
        "amber": "#f59f00",
        "red": "#e03131",
        "unknown": "#868e96",
        "off": "#868e96",
    }.get(lvl, "#868e96")
    return QColor(css)


# ------------------------- Columns -------------------------

DEFAULT_COLS = ["Project", "Count", "Last time", "Since last", "Std gap"]
ALL_AVAILABLE_COLS = list(DEFAULT_COLS)

# Custom data role for the stale level stored on the "Since last" items
ROLE_SINCE_LEVEL = int(Qt.ItemDataRole.UserRole) + 1


# ------------------------- Delegate (the important bit) -------------------------

class SinceLastDelegate(QStyledItemDelegate):
    """
    Paint the "Since last" column ourselves so QSS / alternating rows cannot override it.
    Forces consistent text colour (white) ONLY for this column.
    """
    def paint(self, painter: QPainter, option, index):
        # Read level from our custom role
        level = index.data(ROLE_SINCE_LEVEL)
        level = str(level or "unknown").lower()
        bg = _status_color(level)

        # Text we want to show
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")

        painter.save()

        # 1) Fill background ourselves (this bypasses QSS + alternatingRowColors)
        painter.fillRect(option.rect, bg)

        # 2) Draw text ourselves (this bypasses QSS text colour)
        painter.setPen(QPen(QColor("#ffffff")))

        # Left aligned, vertically centered, with padding
        pad = 8
        r = QRect(option.rect)
        r.adjust(pad, 0, -pad, 0)

        fm = painter.fontMetrics()
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, r.width())
        painter.drawText(r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)

        painter.restore()

    def sizeHint(self, option, index):
        return super().sizeHint(option, index)



# ------------------------- Config -------------------------


def _default_stale_payload() -> Dict[str, Any]:
    """
    Mirrors utils/alerts/stale_alert.py defaults (safe subset; extra keys OK).
    """
    return {
        "amber_min": 30,
        "red_min": 60,
        "scope_all": False,  # False → since last; True → max historical gap
        "interval_min": 15,
        "email_cooldown_min": 240,
        "email_on_amber": False,
        "email_on_escalation": True,
        "email_on_recovery": False,
        "recipients": [],
        "threshold_min": 30,  # legacy
        "also_tables": [],  # optional multi-table monitoring
    }


@dataclass
class SummaryState:
    selected_tables: List[str]
    columns: List[str]
    sample_limit: int
    stale_cfg_by_table: Dict[str, Dict[str, Any]]

    def to_json(self) -> Dict[str, Any]:
        return {
            "selected_tables": list(self.selected_tables),
            "columns": list(self.columns),
            "sample_limit": int(self.sample_limit),
            "stale_cfg_by_table": dict(self.stale_cfg_by_table or {}),
        }

    @staticmethod
    def from_json(d: Dict[str, Any], *, db_tables: List[str]) -> "SummaryState":
        sel = d.get("selected_tables") or []
        sel = [t for t in sel if t in db_tables]
        if not sel:
            sel = list(db_tables)

        cols = d.get("columns") or []
        cols = [c for c in cols if c in ALL_AVAILABLE_COLS]
        if not cols:
            cols = list(DEFAULT_COLS)

        sample = int(d.get("sample_limit") or 20000)
        sample = max(2000, min(sample, 200000))

        raw_cfg = d.get("stale_cfg_by_table") or {}
        cfg: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_cfg, dict):
            for k, v in raw_cfg.items():
                if k in db_tables and isinstance(v, dict):
                    cfg[k] = dict(v)

        return SummaryState(
            selected_tables=sel,
            columns=cols,
            sample_limit=sample,
            stale_cfg_by_table=cfg,
        )

    def stale_cfg_for(self, table: str) -> Dict[str, Any]:
        base = _default_stale_payload()
        user = dict(self.stale_cfg_by_table.get(table, {}) or {})
        base.update(user)
        if "amber_min" in base:
            base["threshold_min"] = int(base.get("amber_min") or base.get("threshold_min") or 30)
        return base

    def set_stale_cfg(self, table: str, payload: Dict[str, Any]) -> None:
        if isinstance(payload, dict):
            self.stale_cfg_by_table[table] = dict(payload)


# ------------------------- Dialogs -------------------------


class _PickListDialog(QDialog):
    """Simple checklist dialog. Optionally supports re-order via move up/down."""

    def __init__(
        self,
        title: str,
        items: List[str],
        checked: List[str],
        parent=None,
        *,
        allow_reorder: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        v = QVBoxLayout(self)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        checked_set = set(checked or [])
        for it in items:
            li = QListWidgetItem(it)
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Checked if it in checked_set else Qt.CheckState.Unchecked)
            self.list.addItem(li)
        v.addWidget(self.list, 1)

        btn_row = QHBoxLayout()
        self.btn_up = QPushButton("Move up", self)
        self.btn_dn = QPushButton("Move down", self)
        self.btn_up.setVisible(allow_reorder)
        self.btn_dn.setVisible(allow_reorder)
        btn_row.addWidget(self.btn_up)
        btn_row.addWidget(self.btn_dn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self.btn_up.clicked.connect(self._move_up)
        self.btn_dn.clicked.connect(self._move_down)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _move_up(self):
        r = self.list.currentRow()
        if r <= 0:
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r - 1, it)
        self.list.setCurrentRow(r - 1)

    def _move_down(self):
        r = self.list.currentRow()
        if r < 0 or r >= self.list.count() - 1:
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + 1, it)
        self.list.setCurrentRow(r + 1)

    def result_checked_in_order(self) -> List[str]:
        out: List[str] = []
        for i in range(self.list.count()):
            li = self.list.item(i)
            if li.checkState() == Qt.CheckState.Checked:
                out.append(li.text())
        return out


class _SettingsDialog(QDialog):
    def __init__(self, state: SummaryState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Summary settings")
        self.setModal(True)
        self._state = state

        v = QVBoxLayout(self)
        form = QFormLayout()
        self.spin_sample = QSpinBox(self)
        self.spin_sample.setRange(2000, 200000)
        self.spin_sample.setSingleStep(2000)
        self.spin_sample.setValue(int(state.sample_limit))
        form.addRow("Gap stats sample size (rows):", self.spin_sample)
        v.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def result_sample_limit(self) -> int:
        return int(self.spin_sample.value())


# ------------------------- Minimal Host (for stale viewer/editor) -------------------------


class _LiteHost:
    """Minimal Host-like object for alert viewers/editors: db_path, table_name, df, datetime_col."""

    def __init__(self, db_path: str, table: str, df: pd.DataFrame, dt_col: str):
        self.db_path = db_path
        self.table_name = table
        self.df = df
        self.datetime_col = dt_col


# ------------------------- Main Page -------------------------


class SummaryPage(QWidget):
    """
    Summary dashboard as a single table.

    Performance model:
    - Full refresh (expensive): refresh_from_db() -> recompute everything.
    - Light refresh (cheap): refresh_light_from_db() -> updates count + last timestamp only.
    - Tick: updates Since last visually every second using cached last_dt (no DB I/O).
    """

    def __init__(self, db_path: str, alerts_provider: Optional[Any] = None, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.alerts_provider = alerts_provider

        self._db_tables = stale_mod.list_user_tables(self.db_path)
        self._state = SummaryState(
            selected_tables=list(self._db_tables),
            columns=list(DEFAULT_COLS),
            sample_limit=20000,
            stale_cfg_by_table={},
        )

        # table -> dict(count, last_dt, last_time_str, std_gap_str, std_gap_tip, since_last_level, since_last_tip, dt_col)
        self._stats_cache: Dict[str, Dict[str, Any]] = {}
        self._host_by_table: Dict[str, _LiteHost] = {}
        self._building = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Header
        top = QHBoxLayout()
        self.badge_lbl = QLabel("—")
        self.badge_lbl.setStyleSheet(
            "padding:4px 10px; border-radius:10px; background:#343a40; color:white; font-weight:bold;"
        )
        top.addWidget(self.badge_lbl)

        self.btn_refresh = QPushButton("Refresh", self)
        self.btn_refresh.clicked.connect(self.refresh_from_db)
        top.addWidget(self.btn_refresh)

        self.btn_projects = QPushButton("Projects…", self)
        self.btn_projects.clicked.connect(self._pick_projects)
        top.addWidget(self.btn_projects)

        self.btn_columns = QPushButton("Columns…", self)
        self.btn_columns.clicked.connect(self._pick_columns)
        top.addWidget(self.btn_columns)

        self.btn_settings = QPushButton("Settings…", self)
        self.btn_settings.clicked.connect(self._open_settings)
        top.addWidget(self.btn_settings)

        top.addStretch(1)
        root.addLayout(top)

        # Table
        self.table = QTableWidget(self)
        self.table.setColumnCount(0)
        self.table.setRowCount(0)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.cellClicked.connect(self._on_cell_clicked)
        root.addWidget(self.table, 1)

        # Tick timer: updates “Since last” visually without DB calls
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_since_last_only)
        self._tick.start()

        self.refresh_from_db()

    # ---------------- persistence hooks ----------------

    def export_state(self) -> Dict[str, Any]:
        return self._state.to_json()

    def import_state(self, data: Dict[str, Any]):
        self._db_tables = stale_mod.list_user_tables(self.db_path)
        self._state = SummaryState.from_json(data or {}, db_tables=self._db_tables)
        self.refresh_from_db()

    # ---------------- pickers / settings ----------------

    def _pick_projects(self):
        self._db_tables = stale_mod.list_user_tables(self.db_path)
        dlg = _PickListDialog(
            "Select projects to show",
            items=list(self._db_tables),
            checked=list(self._state.selected_tables),
            parent=self,
            allow_reorder=False,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.result_checked_in_order()
            self._state.selected_tables = picked if picked else list(self._db_tables)
            self.refresh_from_db()

    def _pick_columns(self):
        dlg = _PickListDialog(
            "Select columns (and reorder)",
            items=list(ALL_AVAILABLE_COLS),
            checked=list(self._state.columns),
            parent=self,
            allow_reorder=True,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cols = dlg.result_checked_in_order()
            self._state.columns = cols if cols else list(DEFAULT_COLS)
            self._render_from_cache()

    def _open_settings(self):
        dlg = _SettingsDialog(self._state, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._state.sample_limit = dlg.result_sample_limit()
            self.refresh_from_db()

    # ---------------- click handling ----------------
    # ---------------- stale alert syncing (single source of truth) ----------------

    def _provider_find_stale(self, table: str) -> Optional[AlertSpec]:
        """
        Ask the alerts_provider for the *canonical* Stale alert for this table.

        We intentionally duck-type this so it works with whatever your Alerts tab/manager exposes.
        Supported provider methods (first match wins):
          - get_stale_alert(table) -> AlertSpec|None
          - find_alert(table, kind) -> AlertSpec|None
          - get_alert(table, kind) -> AlertSpec|None
        """
        ap = self.alerts_provider
        if ap is None:
            return None

        for fn_name in ("get_stale_alert", "find_alert", "get_alert"):
            fn = getattr(ap, fn_name, None)
            if callable(fn):
                try:
                    # common conventions
                    if fn_name == "get_stale_alert":
                        spec = fn(table)
                    else:
                        spec = fn(table, "Stale")
                    if isinstance(spec, AlertSpec):
                        return spec
                except Exception:
                    pass
        return None

    def _provider_upsert_alert(self, table: str, spec: AlertSpec) -> None:
        """
        Persist the spec back into the alerts system.
        Supported provider methods:
          - upsert_alert(table, spec)
          - save_alert(table, spec)
          - set_alert(table, spec)
        """
        ap = self.alerts_provider
        if ap is None:
            return

        for fn_name in ("upsert_alert", "save_alert", "set_alert"):
            fn = getattr(ap, fn_name, None)
            if callable(fn):
                try:
                    fn(table, spec)
                    return
                except Exception:
                    pass

    def _get_canonical_stale_spec(self, table: str) -> AlertSpec:
        """
        Return the *single* alert spec we should use for 'Since last'.
        Priority:
          1) alerts_provider's stored Stale alert (canonical)
          2) SummaryState fallback (legacy)
        """
        # 1) canonical from provider
        spec = self._provider_find_stale(table)
        if spec is not None:
            return spec

        # 2) fallback: build from SummaryState (your current behavior)
        payload = self._state.stale_cfg_for(table)

    def _on_cell_clicked(self, row: int, col: int):
        headers = [self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())]
        if col < 0 or col >= len(headers):
            return
        if headers[col] != "Since last":
            return

        table = self._table_name_from_row(row, headers)
        if not table:
            return

        host = self._build_host_for_stale(table, for_viewer=True)
        if host is None:
            QMessageBox.information(self, "Since last", f"Could not load data for {table}.")
            return
        self._host_by_table[table] = host

        payload = self._state.stale_cfg_for(table)
        spec = AlertSpec(
            id=table,
            kind="Stale",
            name=f"Since last — {table}",
            enabled=True,
            recipients=list(payload.get("recipients", []) or []),
            payload=payload,
        )

        handler = REGISTRY.get("Stale")
        if not handler:
            QMessageBox.information(
                self,
                "Since last",
                "Stale handler not registered. Import utils.alerts.stale_alert.",
            )
            return

        try:
            dlg = handler.create_viewer(spec, host, self)
            if isinstance(dlg, QDialog):
                dlg.exec()
        except Exception as e:
            QMessageBox.critical(self, "Since last", str(e))
            return

        # Persist edits:
        # 1) If we have an alerts_provider, save back into the canonical alert store
        self._provider_upsert_alert(table, spec)

        # 2) Also keep SummaryState in sync as fallback/legacy (optional but safe)
        try:
            self._state.set_stale_cfg(table, dict(spec.payload or {}))
        except Exception:
            pass

        # Thresholds changed → recompute status colours (no DB refresh)
        self._recompute_status_for_table_from_cache(table)
        self._render_from_cache()

    def _table_name_from_row(self, row: int, headers: List[str]) -> Optional[str]:
        for c in range(self.table.columnCount()):
            if headers[c] == "Project":
                it = self.table.item(row, c)
                if it:
                    return str(it.data(Qt.ItemDataRole.UserRole) or it.text())
        return None

    # Used by StaleViewDialog._on_edit() (it checks for parent.configure_spec(spec))
    def configure_spec(self, spec: AlertSpec):
        table = str(getattr(spec, "id", "") or "")
        if not table:
            return
        host = self._host_by_table.get(table)
        if host is None:
            host = self._build_host_for_stale(table, for_viewer=True)
            if host is None:
                return
            self._host_by_table[table] = host

        handler = REGISTRY.get(spec.kind)
        if not handler or not hasattr(handler, "create_editor"):
            return

        try:
            dlg = handler.create_editor(spec, host, self)
            if isinstance(dlg, QDialog) and dlg.exec():
                self._state.set_stale_cfg(table, dict(spec.payload or {}))
        except Exception:
            pass

    # ---------------- refresh (DB vs cache) ----------------

    def refresh_from_db(self):
        """Full refresh FROM SQLite (expensive). Only runs on user action."""
        if self._building:
            return
        self._building = True
        try:
            self._load_cache_from_db()
            self._render_from_cache()
        finally:
            self._building = False

    def refresh_light_from_db(self):
        """
        Light refresh FROM SQLite (cheap):
          - updates count + last_dt
          - DOES NOT recompute std-gap sampling (expensive)
          - recomputes stale level/tooltips using the handler (fast eval slice) per touched table
        """
        if self._building:
            return
        self._building = True
        try:
            tables_live = set(_list_user_tables(self.db_path))
            tables = [t for t in self._state.selected_tables if t in tables_live]
            if not tables:
                tables = list(sorted(tables_live))

            new_cache: Dict[str, Dict[str, Any]] = dict(self._stats_cache or {})

            with sqlite3.connect(self.db_path, timeout=10) as conn:
                for table in tables:
                    dt_col = _choose_dt_col(conn, table)
                    if not dt_col:
                        new_cache[table] = {"table": table, "count": 0, "last_dt": None, "dt_col": None}
                        continue

                    try:
                        df_basic = pd.read_sql_query(
                            f'SELECT COUNT(1) AS n, MAX("{dt_col}") AS mx FROM "{table}"',
                            conn,
                        )
                    except Exception:
                        df_basic = pd.DataFrame()

                    count = 0
                    last_dt: Optional[pd.Timestamp] = None
                    if not df_basic.empty:
                        try:
                            count = int(df_basic.loc[0, "n"] or 0)
                        except Exception:
                            count = 0
                        try:
                            mx_s = pd.Series([df_basic.loc[0, "mx"]])
                            mx = parse_series_to_local_naive(mx_s).dropna()
                            last_dt = mx.iloc[0] if not mx.empty else None
                        except Exception:
                            last_dt = None

                    prev = new_cache.get(table, {})
                    new_cache[table] = {
                        "table": table,
                        "dt_col": dt_col,
                        "count": count,
                        "last_dt": last_dt,
                        "last_time_str": _fmt_dt(last_dt),
                        "std_gap_str": prev.get("std_gap_str", "—"),
                        "std_gap_tip": prev.get("std_gap_tip", ""),
                        "since_last_level": prev.get("since_last_level", "unknown"),
                        "since_last_tip": prev.get("since_last_tip", ""),
                    }

            self._stats_cache = new_cache

            for table in tables:
                self._recompute_status_for_table_from_cache(table)

            self._render_from_cache()
        finally:
            self._building = False

    def refresh(self):
        """Compatibility method: re-render cached values (no DB)."""
        self._render_from_cache()

    def _load_cache_from_db(self):
        tables_live = set(_list_user_tables(self.db_path))
        tables = [t for t in self._state.selected_tables if t in tables_live]
        if not tables:
            tables = list(sorted(tables_live))

        new_cache: Dict[str, Dict[str, Any]] = {}

        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                for table in tables:
                    dt_col = _choose_dt_col(conn, table)
                    if not dt_col:
                        new_cache[table] = {"table": table, "count": 0, "last_dt": None, "dt_col": None}
                        continue

                    # cheap stats: COUNT + MAX(dt)
                    try:
                        df_basic = pd.read_sql_query(
                            f'SELECT COUNT(1) AS n, MAX("{dt_col}") AS mx FROM "{table}"',
                            conn,
                        )
                    except Exception:
                        df_basic = pd.DataFrame()

                    count = 0
                    last_dt: Optional[pd.Timestamp] = None
                    if not df_basic.empty:
                        try:
                            count = int(df_basic.loc[0, "n"] or 0)
                        except Exception:
                            count = 0
                        try:
                            mx_s = pd.Series([df_basic.loc[0, "mx"]])
                            mx = parse_series_to_local_naive(mx_s).dropna()
                            last_dt = mx.iloc[0] if not mx.empty else None
                        except Exception:
                            last_dt = None

                    # "Std gap": typical cadence estimate (mode of binned deltas)
                    std_gap_str = "—"
                    std_gap_tip = ""
                    if count >= 2:
                        lim = int(self._state.sample_limit or 20000)
                        lim = max(2000, min(lim, 200000))
                        try:
                            df_ts = pd.read_sql_query(
                                f'SELECT "{dt_col}" AS t FROM "{table}" ORDER BY "{dt_col}" DESC LIMIT {lim}',
                                conn,
                            )
                        except Exception:
                            df_ts = pd.DataFrame()

                        if not df_ts.empty:
                            ts = parse_series_to_local_naive(df_ts["t"]).dropna()
                            if ts.shape[0] >= 2:
                                ts = ts.sort_values(kind="stable")
                                deltas = ts.diff().dropna()
                                if not deltas.empty:
                                    sec = deltas.dt.total_seconds().astype(float)
                                    sec = sec[sec > 0]
                                    if not sec.empty:
                                        bucket_s = 60.0  # 1-minute buckets
                                        binned = (sec / bucket_s).round() * bucket_s

                                        q1 = binned.quantile(0.25)
                                        q3 = binned.quantile(0.75)
                                        iqr = max(1.0, float(q3 - q1))
                                        cutoff = float(q3 + 3.0 * iqr)
                                        regular = binned[binned <= cutoff]
                                        if regular.empty:
                                            regular = binned

                                        vc = regular.value_counts()
                                        typical_s = float(vc.index[0])
                                        std_gap_str = _fmt_td(pd.Timedelta(seconds=typical_s))
                                        std_gap_tip = (
                                            f"Typical gap (mode) using {bucket_s:.0f}s bins; "
                                            f"n={int(regular.shape[0])} gaps; cutoff={int(cutoff)}s."
                                        )

                    new_cache[table] = {
                        "table": table,
                        "dt_col": dt_col,
                        "count": count,
                        "last_dt": last_dt,
                        "last_time_str": _fmt_dt(last_dt),
                        "std_gap_str": std_gap_str,
                        "std_gap_tip": std_gap_tip,
                    }

        except Exception:
            return

        self._stats_cache = new_cache

        for table in list(self._stats_cache.keys()):
            self._recompute_status_for_table_from_cache(table)

    def _recompute_status_for_table_from_cache(self, table: str):
        row = self._stats_cache.get(table)
        if not row:
            return

        spec = self._get_canonical_stale_spec(table)

        handler = REGISTRY.get("Stale")
        if not handler:
            row["since_last_level"] = "unknown"
            row["since_last_tip"] = "Stale handler not registered."
            return

        host = self._build_host_for_stale(table, for_viewer=False)
        if host is None:
            row["since_last_level"] = "unknown"
            row["since_last_tip"] = "Could not load host data."
            return

        try:
            res = handler.evaluate(spec, host)
            row["since_last_level"] = _status_to_level(res.get("status"))
            row["since_last_tip"] = str(res.get("summary") or "") or "Click to view chart and edit thresholds."
        except Exception:
            row["since_last_level"] = self._fallback_level_from_last_dt(table)
            row["since_last_tip"] = "Click to view chart and edit thresholds."

    def _fallback_level_from_last_dt(self, table: str) -> str:
        row = self._stats_cache.get(table) or {}
        last_dt = row.get("last_dt", None)
        if last_dt is None:
            return "unknown"

        now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)
        since_s = float((now_local - last_dt).total_seconds())

        payload = self._state.stale_cfg_for(table)
        amb = int(payload.get("amber_min", payload.get("threshold_min", 30)) or 30)
        red = int(payload.get("red_min", max(amb * 2, 60)) or max(amb * 2, 60))
        if red < amb:
            red = amb

        if since_s >= float(red * 60):
            return "red"
        if since_s >= float(amb * 60):
            return "amber"
        return "green"

    def _update_badge(self, tables: List[str]) -> None:
        g = a = r = u = 0
        for t in tables:
            row = self._stats_cache.get(t, {})
            last_dt = row.get("last_dt")
            lvl = self._fallback_level_from_last_dt(t) if last_dt is not None else row.get("since_last_level", "unknown")
            lvl = str(lvl or "unknown").lower()
            if lvl == "green":
                g += 1
            elif lvl == "amber":
                a += 1
            elif lvl == "red":
                r += 1
            else:
                u += 1

        parts = []
        if r:
            parts.append(f"🔴 {r}")
        if a:
            parts.append(f"🟠 {a}")
        if g:
            parts.append(f"🟢 {g}")
        if u:
            parts.append(f"⚪ {u}")
        self.badge_lbl.setText(f"Status: {'  '.join(parts) if parts else '—'}")

    def _apply_since_delegate(self, cols: List[str]):
        """
        Important: set the delegate AFTER columns exist (and after re-render),
        otherwise Qt may ignore it for new models/headers.
        """
        if "Since last" not in cols:
            return
        since_col = cols.index("Since last")
        self.table.setItemDelegateForColumn(since_col, SinceLastDelegate(self.table))

    def _render_from_cache(self):
        tables_live = set(_list_user_tables(self.db_path))
        tables = [t for t in self._state.selected_tables if t in tables_live]
        if not tables:
            tables = list(sorted(tables_live))

        self._update_badge(tables)

        cols = list(self._state.columns)

        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setRowCount(len(tables))

        tips = {
            "Project": "SQLite table name (one project per table).",
            "Count": "Total number of rows in the table.",
            "Last time": "Timestamp of the most recent datapoint (from last Refresh).",
            "Since last": "Time since the most recent datapoint. Click to view chart and edit thresholds/email settings.",
            "Std gap": "Typical cadence estimate from recent datapoints (computed on Refresh).",
        }
        for i, name in enumerate(cols):
            hi = QTableWidgetItem(name)
            hi.setToolTip(tips.get(name, name))
            self.table.setHorizontalHeaderItem(i, hi)

        # ✅ Make sure the Since last delegate is active
        self._apply_since_delegate(cols)

        now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)

        for r, table_name in enumerate(tables):
            row = self._stats_cache.get(table_name, {"table": table_name})
            last_dt = row.get("last_dt", None)
            since_td = (now_local - last_dt) if last_dt is not None else None
            since_last_str = _fmt_td(since_td)

            since_last_level = self._fallback_level_from_last_dt(table_name) if last_dt is not None else "unknown"
            since_last_tip = row.get("since_last_tip", "") or ""

            for c, colname in enumerate(cols):
                if colname == "Project":
                    it = QTableWidgetItem(table_name)
                    it.setData(Qt.ItemDataRole.UserRole, table_name)
                    self.table.setItem(r, c, it)

                elif colname == "Count":
                    self.table.setItem(r, c, QTableWidgetItem(str(row.get("count", "—"))))

                elif colname == "Last time":
                    self.table.setItem(r, c, QTableWidgetItem(row.get("last_time_str", "—")))

                elif colname == "Since last":
                    # ✅ Do NOT setForeground/setBackground here.
                    # We store level as data; the delegate paints it reliably.
                    it = QTableWidgetItem(since_last_str)
                    it.setData(ROLE_SINCE_LEVEL, str(since_last_level))
                    it.setToolTip(since_last_tip)
                    self.table.setItem(r, c, it)

                elif colname == "Std gap":
                    it = QTableWidgetItem(row.get("std_gap_str", "—"))
                    it.setToolTip(row.get("std_gap_tip", ""))
                    self.table.setItem(r, c, it)

                else:
                    self.table.setItem(r, c, QTableWidgetItem("—"))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        # Force a repaint so the delegate kicks in immediately
        self.table.viewport().update()

    def _tick_since_last_only(self):
        """Update only the Since last cell display + colour, without DB reads."""
        if self.table.rowCount() <= 0:
            return

        headers = [self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())]
        if "Since last" not in headers or "Project" not in headers:
            return

        col_since = headers.index("Since last")
        col_proj = headers.index("Project")

        now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)

        for r in range(self.table.rowCount()):
            it_proj = self.table.item(r, col_proj)
            if not it_proj:
                continue
            table = str(it_proj.data(Qt.ItemDataRole.UserRole) or it_proj.text())
            row = self._stats_cache.get(table)
            if not row:
                continue

            last_dt = row.get("last_dt", None)
            if last_dt is None:
                continue

            since_td = now_local - last_dt
            since_str = _fmt_td(since_td)
            level = self._fallback_level_from_last_dt(table)

            it_since = self.table.item(r, col_since)
            if not it_since:
                it_since = QTableWidgetItem()
                self.table.setItem(r, col_since, it_since)

            it_since.setText(since_str)
            it_since.setData(ROLE_SINCE_LEVEL, str(level))

        # repaint only once at end
        self.table.viewport().update()

    # ---------------- host builder for stale viewer/editor ----------------

    def _build_host_for_stale(self, table: str, *, for_viewer: bool = True) -> Optional[_LiteHost]:
        """
        Build a Host-like object for the Stale viewer/editor.

        - for_viewer=True loads up to sample_limit rows (chronological) for charting.
        - for_viewer=False loads a smaller slice (fast eval path).
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                dt_col = _choose_dt_col(conn, table)
                if not dt_col:
                    return None

                lim = int(self._state.sample_limit or 20000) if for_viewer else 5000
                lim = max(2000, min(lim, 200000))

                q = f'SELECT "{dt_col}" FROM "{table}" ORDER BY "{dt_col}" DESC LIMIT {lim}'
                df = pd.read_sql_query(q, conn)

                if df.empty:
                    return _LiteHost(self.db_path, table, df, dt_col)

                df = df.iloc[::-1].reset_index(drop=True)  # chronological
                df[dt_col] = parse_series_to_local_naive(df[dt_col])
                return _LiteHost(self.db_path, table, df, dt_col)
        except Exception:
            return None
