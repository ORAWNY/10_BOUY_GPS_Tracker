# utils/alerts/missing_data_alert.py
from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple
import re
import numpy as np
import pandas as pd

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QSpinBox, QCheckBox, QPushButton, QVBoxLayout, QLineEdit,
    QLabel, QListWidget, QListWidgetItem, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QToolButton, QComboBox, QFileDialog, QMessageBox, QWidget,
    QHeaderView
)
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtCore import Qt

from utils.alerts import REGISTRY, register, AlertSpec, AlertHandler, EvalResult, Status, Host
from utils.constants import SENTINEL_VALUES
from utils.time_settings import local_zone, parse_series_to_local_naive


def _pick_time_col(df: pd.DataFrame, dt_name: Optional[str]) -> Optional[str]:
    if dt_name and dt_name in df.columns:
        return dt_name
    for c in ["__dt_iso", "timestamp", "received_time", "datetime", "time", "date", "DateTime"]:
        if c in df.columns:
            return c
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            return c
    return None


def _as_list_from_line_edit(s: str) -> List[str]:
    parts = re.split(r"[,\s;]+", (s or "").strip())
    return [p for p in (x.strip() for x in parts) if p]


class _Editor(QDialog):
    """
    Editor:
      • Select columns (multi) or 'use all'
      • Window length (minutes)
      • AMBER / RED thresholds (minimum % rows that must be populated)
      • Recipients / interval / cooldown toggles (consistent with other alerts)
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self._parent = parent
        self.setWindowTitle("Missing data (per-column completeness)")

        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        # --- SAFE: avoid pandas truthiness ---
        df = getattr(self.host, "df", None)
        if df is None:
            df = pd.DataFrame()

        tcol = _pick_time_col(df, getattr(self.host, "datetime_col", None))

        # ---- Column picker ----
        all_cols = [c for c in list(df.columns) if c != tcol]
        p = self.spec.payload or {}

        self.use_all_cb = QCheckBox("Use ALL columns")
        self.use_all_cb.setChecked(bool(p.get("use_all", True)))
        form.addRow(self.use_all_cb)

        self.cols_list = QListWidget(self)
        self.cols_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        for c in all_cols:
            self.cols_list.addItem(QListWidgetItem(c))

        # Preselect from payload (if any)
        preset = p.get("columns") or []
        if preset:
            want = set(preset)
            for i in range(self.cols_list.count()):
                it = self.cols_list.item(i)
                it.setSelected(it.text() in want)

        self.cols_list.setEnabled(not self.use_all_cb.isChecked())
        self.use_all_cb.stateChanged.connect(lambda _=None: self.cols_list.setEnabled(not self.use_all_cb.isChecked()))
        form.addRow(QLabel("Columns to check:"))
        form.addRow(self.cols_list)

        # ---- Time window (minutes) ----
        self.window_min = QSpinBox(); self.window_min.setRange(1, 1_000_000)
        self.window_min.setValue(int(p.get("window_minutes", 24 * 60)))
        form.addRow("Window length (minutes):", self.window_min)

        # ---- % thresholds (minimum completeness required) ----
        self.amb_pct = QSpinBox(); self.amb_pct.setRange(0, 100); self.amb_pct.setValue(int(p.get("amber_pct", 95)))
        self.red_pct = QSpinBox(); self.red_pct.setRange(0, 100); self.red_pct.setValue(int(p.get("red_pct", 80)))
        form.addRow("AMBER if % < :", self.amb_pct)
        form.addRow("RED if % < :", self.red_pct)

        hint = QLabel("Example: AMBER<95%, RED<80% — overall status is the worst across selected columns.")
        hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(hint)

        # ---- Recipients / interval / emails ----
        existing_rcpts = self.spec.recipients or p.get("recipients", [])
        self.recipients_edit = QLineEdit(", ".join(existing_rcpts))
        self.recipients_edit.setPlaceholderText("alice@company.com, bob@company.com")

        self.interval_min = QSpinBox(); self.interval_min.setRange(1, 100000)
        self.interval_min.setValue(int(p.get("interval_min", 15)))

        self.cooldown_min = QSpinBox(); self.cooldown_min.setRange(0, 100000)
        self.cooldown_min.setValue(int(p.get("email_cooldown_min", 240)))

        form.addRow("Email recipients:", self.recipients_edit)
        form.addRow("Check interval (min):", self.interval_min)
        form.addRow("Email cool-down (min):", self.cooldown_min)

        note = QLabel("Emails are sent on GREEN→AMBER/RED; cooldown throttles frequency.")
        note.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(note)

        # ---- OK
        btn = QPushButton("OK"); btn.clicked.connect(self.accept); lay.addWidget(btn)

    def accept(self):
        p = self.spec.payload or {}

        # Selected columns
        use_all = bool(self.use_all_cb.isChecked())
        cols = []
        if not use_all:
            for i in range(self.cols_list.count()):
                it = self.cols_list.item(i)
                if it.isSelected():
                    cols.append(it.text())

        # Name (make unique if parent provides helper)
        new_name = (self.spec.name or "Missing data (completeness)").strip()
        if hasattr(self._parent, "_uniquify_name") and callable(getattr(self._parent, "_uniquify_name")):
            self.spec.name = self._parent._uniquify_name(new_name, exclude_id=self.spec.id)
        else:
            self.spec.name = new_name

        p["use_all"] = use_all
        p["columns"] = cols
        p["window_minutes"] = int(self.window_min.value())
        p["amber_pct"] = int(self.amb_pct.value())
        p["red_pct"] = int(self.red_pct.value())

        # Email bits
        raw = (self.recipients_edit.text() or "").strip()
        parts = re.split(r"[,\s;]+", raw)
        emails = [e for e in (s.strip() for s in parts) if e and "@" in e]
        self.spec.recipients = emails
        p["recipients"] = emails
        p["interval_min"] = int(self.interval_min.value())
        p["email_cooldown_min"] = int(self.cooldown_min.value())

        self.spec.payload = p
        super().accept()


# ---------- helper: a frozen-first-column composite table ----------
class FrozenFirstColumn(QWidget):
    """
    Two synchronized QTableWidgets:
      - left: first column (frozen)
      - main: remaining columns (scrollable)
    Vertical scrollbars stay in sync. The header row is naturally frozen by Qt.
    """
    def __init__(self, left_header: str, parent=None):
        super().__init__(parent)
        self.left = QTableWidget(0, 1, self)
        self.left.setHorizontalHeaderLabels([left_header])
        self.left.verticalHeader().setVisible(False)
        self.left.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.left.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left.setWordWrap(False)
        self.left.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.left.horizontalHeader().setStretchLastSection(False)

        self.main = QTableWidget(0, 0, self)
        self.main.verticalHeader().setVisible(False)
        self.main.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.main.setWordWrap(False)

        # sync scrolling
        self.left.verticalScrollBar().valueChanged.connect(self.main.verticalScrollBar().setValue)
        self.main.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)

        # layout
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self.left, 0)
        h.addWidget(self.main, 1)

    def auto_fit_left(self, extra_padding: int = 24):
        """Resize the frozen left column to fit its widest content + padding."""
        self.left.resizeColumnToContents(0)
        pad = extra_padding
        vscroll = self.left.verticalScrollBar().sizeHint().width() if self.left.verticalScrollBar() else 0
        width = self.left.columnWidth(0) + pad + vscroll
        # clamp to fixed width so text is always fully visible
        self.left.setMinimumWidth(width)
        self.left.setMaximumWidth(width)

    # convenience methods used by caller
    def clear(self):
        self.left.setRowCount(0)
        self.main.setRowCount(0)

    def set_left_header(self, name: str):
        self.left.setHorizontalHeaderLabels([name])

    def set_main_headers(self, headers: List[str]):
        self.main.setColumnCount(len(headers))
        self.main.setHorizontalHeaderLabels(headers)

    def append_row(self, left_value: str, main_values: List[Any], bg_mask: Optional[List[bool]] = None):
        # left table
        lr = self.left.rowCount()
        self.left.insertRow(lr)
        self.left.setItem(lr, 0, QTableWidgetItem(left_value))

        # main table
        mr = self.main.rowCount()
        if mr != lr:
            self.main.insertRow(lr)
        else:
            self.main.insertRow(mr)
        row_idx = lr

        for j, v in enumerate(main_values):
            it = QTableWidgetItem("" if v is None else str(v))
            if bg_mask and j < len(bg_mask) and bg_mask[j]:
                it.setBackground(QBrush(QColor("#f59f00")))
            self.main.setItem(row_idx, j, it)

    def horizontal_header_item(self, j: int) -> Optional[QTableWidgetItem]:
        return self.main.horizontalHeaderItem(j)


# --- Summary-enhanced viewer dialog with frozen left columns & resizable panes ---
class _MissingDataViewerDialog(QDialog):
    """
    MissingData inspector:
      • Top bar: Range, Refresh, thresholds & selected columns, Edit…, Export…
      • (old quick-glance header hidden)
      • Summary matrix: metrics × columns, left metric names frozen, top header frozen
      • Table: Time (local) frozen on the left + selected columns; missing/bad values highlighted
      • Top and bottom panes are resizable (QSplitter vertical)
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self._parent = parent
        self.setWindowTitle(spec.name or "Missing data preview")
        self.setMinimumSize(1100, 620)

        outer = QVBoxLayout(self)

        # Top controls
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")
        self.refresh_btn = QToolButton(); self.refresh_btn.setText("⟳ Refresh"); self.refresh_btn.clicked.connect(self._rebuild)
        self.th_label = QLabel(" ")
        self.cols_label = QLabel(" ")
        self.edit_btn = QToolButton(); self.edit_btn.setText("⚙ Edit…"); self.edit_btn.clicked.connect(self._on_edit)
        self.export_btn = QToolButton(); self.export_btn.setText("⬇ Export…"); self.export_btn.clicked.connect(self._export_report)

        ctrl.addWidget(self.range_combo); ctrl.addWidget(self.refresh_btn)
        ctrl.addStretch(1); ctrl.addWidget(self.th_label); ctrl.addSpacing(12); ctrl.addWidget(self.cols_label)
        ctrl.addStretch(1); ctrl.addWidget(self.edit_btn); ctrl.addWidget(self.export_btn)
        outer.addLayout(ctrl)

        # Old quick-glance line hidden per request
        self.summary_lbl = QLabel(" "); self.summary_lbl.setStyleSheet("font-weight:600;")
        self.summary_lbl.hide()
        outer.addWidget(self.summary_lbl)

        # Splitter between summary matrix (top) and data table (bottom)
        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        outer.addWidget(self.splitter, 1)

        # --- Top: Summary matrix with frozen first column (Metric) ---
        self.summary_matrix = FrozenFirstColumn(left_header="Metric", parent=self)
        self.splitter.addWidget(self.summary_matrix)

        # --- Bottom: Data table with frozen left column (Time) ---
        self.data_grid = FrozenFirstColumn(left_header="Time (local)", parent=self)
        self.splitter.addWidget(self.data_grid)

        # reasonable initial sizes; user can drag to resize
        self.splitter.setSizes([300, 600])

        self._rebuild()

    # -------- helpers --------
    def _window_td(self) -> Optional[pd.Timedelta]:
        m = {
            "6 h": pd.Timedelta(hours=6),
            "12 h": pd.Timedelta(hours=12),
            "24 h": pd.Timedelta(hours=24),
            "3 d": pd.Timedelta(days=3),
            "7 d": pd.Timedelta(days=7),
            "30 d": pd.Timedelta(days=30),
        }
        return m.get(self.range_combo.currentText(), None)

    def _time_col(self, df: pd.DataFrame) -> Optional[str]:
        dt_pref = getattr(self.host, "datetime_col", None)
        if dt_pref and dt_pref in df.columns:
            return dt_pref
        for c in ["__dt_iso", "timestamp", "received_time", "datetime", "time", "date", "DateTime"]:
            if c in df.columns: return c
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]): return c
        return None

    def _columns_to_check(self, df: pd.DataFrame, tcol: Optional[str]) -> List[str]:
        p = self.spec.payload or {}
        if bool(p.get("use_all", True)):
            return [c for c in list(df.columns) if c != tcol]
        cols = p.get("columns") or []
        return [c for c in cols if c in df.columns and c != tcol]

    def _on_edit(self):
        try:
            cfg = getattr(self._parent, "configure_spec", None)
            if callable(cfg):
                cfg(self.spec)
            else:
                dlg = REGISTRY[self.spec.kind].create_editor(self.spec, self.host, self)
                if dlg.exec():
                    pass
        except Exception:
            pass
        self._rebuild()

    # -------- export --------
    def _export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export missing-data report", "", "CSV files (*.csv)")
        if not path:
            return
        dfw, tcol, cols, pct = self._current_windowed()
        if dfw.empty:
            QMessageBox.information(self, "Export", "No data to export.")
            return
        try:
            summary_map = self._compute_summary_per_column(dfw, tcol, cols)
            out = dfw[[tcol] + cols].copy().rename(columns={tcol: "time_local"})
            out["time_local"] = out["time_local"].dt.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "w", encoding="utf-8") as f:
                p = self.spec.payload or {}
                f.write("# Missing data report\n")
                f.write(f"# Name: {self.spec.name or self.spec.kind}\n")
                f.write(f"# Range: {self.range_combo.currentText()}\n")
                f.write(f"# AMBER<{int(p.get('amber_pct', 95))}%, RED<{int(p.get('red_pct', 80))}%\n")
                for c in cols:
                    f.write(f"# {c}: {pct.get(c, 0.0):.1f}% filled\n")
                f.write("# --- summary (metrics × columns) ---\n")
                metrics = self._summary_metric_names()
                for m in metrics:
                    row = [m] + [str(summary_map.get(m, {}).get(c, "")) for c in cols]
                    f.write("# " + ", ".join(row) + "\n")
                f.write("# --- data ---\n")
            out.to_csv(path, index=False, mode="a")
            QMessageBox.information(self, "Export", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    # -------- data slicing --------
    def _current_windowed(self) -> Tuple[pd.DataFrame, str, List[str], Dict[str, float]]:
        df = getattr(self.host, "df", None)
        if df is None or df.empty:
            return pd.DataFrame(), "", [], {}

        tcol = self._time_col(df)
        if not tcol:
            return pd.DataFrame(), "", [], {}

        d = df.copy()
        d[tcol] = parse_series_to_local_naive(d[tcol]).dropna()
        d = d.dropna(subset=[tcol]).sort_values(tcol)

        td = self._window_td()
        if td is not None and not d.empty:
            t_end = d[tcol].max()
            t_start = t_end - td
            d = d[(d[tcol] >= t_start) & (d[tcol] <= t_end)]

        cols = self._columns_to_check(d, tcol)
        if not cols:
            return pd.DataFrame(), tcol, [], {}

        n = int(d.shape[0])
        pct = {}
        if n > 0:
            for c in cols:
                pct[c] = float(d[c].notna().sum()) / n * 100.0
        return d, tcol, cols, pct

    # -------- summary (per column) --------
    def _summary_metric_names(self) -> List[str]:
        return [
            "Usual gap (median)",
            "Avg gap (mean)",
            "Missing timestamps (# >2× usual gap)",
            "Availability % (valid cells)",
            "Valid numeric min",
            "Valid numeric max",
            "Valid numeric mean",
            "Count empty-string",
            "Count zero",
            "Count 9999",
            "Count -9999",
            "Count NaN",
            "Rows (window)",
        ]

    def _compute_summary_per_column(self, d: pd.DataFrame, tcol: str, cols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Returns a dict: metric_name -> { column_name: value }
        Time-gap metrics are common to all columns (copied across).
        'Availability' counts cells that are NOT: NaN, empty-string, 0, 9999, -9999.
        Numeric stats use coerced numeric values, excluding 0 and ±9999.
        """
        out: Dict[str, Dict[str, Any]] = {m: {} for m in self._summary_metric_names()}
        if d.empty or not cols:
            return out

        # time gap metrics (shared)
        dt_series = d[tcol].sort_values()
        gaps = dt_series.diff().dropna()
        if gaps.empty:
            med_gap = pd.Timedelta(0); mean_gap = pd.Timedelta(0); miss_ts = 0
        else:
            med_gap = gaps.median(); mean_gap = gaps.mean()
            thr = 2 * med_gap if med_gap.total_seconds() > 0 else pd.Timedelta.max
            miss_ts = int((gaps > thr).sum()) if thr != pd.Timedelta.max else 0

        def fmt_td(td: pd.Timedelta) -> str:
            secs = int(td.total_seconds())
            if secs <= 0: return "0s"
            parts = []
            days, rem = divmod(secs, 86400)
            if days: parts.append(f"{days}d")
            hrs, rem = divmod(rem, 3600)
            if hrs: parts.append(f"{hrs}h")
            mins, rem = divmod(rem, 60)
            if mins: parts.append(f"{mins}m")
            if rem or not parts: parts.append(f"{rem}s")
            return " ".join(parts)

        # per-column stats
        for c in cols:
            s = d[c]

            empty_mask = s.apply(lambda x: isinstance(x, str) and x.strip() == "")
            zero_mask = s.apply(lambda x: isinstance(x, (int, float, np.integer, np.floating)) and x == 0)
            n9999_mask = s.apply(lambda x: isinstance(x, (int, float, np.integer, np.floating)) and x == -9999)
            p9999_mask = s.apply(lambda x: isinstance(x, (int, float, np.integer, np.floating)) and x == 9999)
            nan_mask   = s.isna()
            bad_mask   = empty_mask | zero_mask | n9999_mask | p9999_mask | nan_mask

            total = int(s.shape[0])
            good = int(total - int(bad_mask.sum()))
            availability = 100.0 * good / total if total else 0.0

            # numeric stats on valid numbers only
            num = pd.to_numeric(s, errors="coerce").mask(lambda x: x.isin(SENTINEL_VALUES))
            num_valid = num.dropna()

            out["Usual gap (median)"][c] = fmt_td(med_gap)
            out["Avg gap (mean)"][c] = fmt_td(mean_gap)
            out["Missing timestamps (# >2× usual gap)"][c] = miss_ts
            out["Availability % (valid cells)"][c] = f"{availability:.1f}"
            out["Valid numeric min"][c] = f"{float(num_valid.min()):.6g}" if not num_valid.empty else "n/a"
            out["Valid numeric max"][c] = f"{float(num_valid.max()):.6g}" if not num_valid.empty else "n/a"
            out["Valid numeric mean"][c] = f"{float(num_valid.mean()):.6g}" if not num_valid.empty else "n/a"
            out["Count empty-string"][c] = int(empty_mask.sum())
            out["Count zero"][c] = int(zero_mask.sum())
            out["Count 9999"][c] = int(p9999_mask.sum())
            out["Count -9999"][c] = int(n9999_mask.sum())
            out["Count NaN"][c] = int(nan_mask.sum())
            out["Rows (window)"][c] = total

        return out

    # -------- builder --------
    def _rebuild(self):
        p = self.spec.payload or {}
        amber = int(p.get("amber_pct", 95))
        red = int(p.get("red_pct", 80))
        self.th_label.setText(f"AMBER<{amber}%   •   RED<{red}%")

        d, tcol, cols, pct = self._current_windowed()
        self.cols_label.setText(f"Columns: {', '.join(cols) if cols else '(none)'}")

        # Old header line disabled per user request
        self.summary_lbl.clear()

        # --- TOP: Summary matrix (metrics × columns) ---
        self.summary_matrix.clear()
        self.summary_matrix.set_left_header("Metric")
        self.summary_matrix.set_main_headers(cols)

        if d.empty or not cols:
            for m in self._summary_metric_names():
                self.summary_matrix.append_row(m, [""] * len(cols))
        else:
            summary_map = self._compute_summary_per_column(d, tcol, cols)
            for m in self._summary_metric_names():
                row_vals = [summary_map.get(m, {}).get(c, "") for c in cols]
                self.summary_matrix.append_row(m, row_vals)
        # ensure the frozen metric column is fully readable
        self.summary_matrix.auto_fit_left()

        # --- BOTTOM: Data grid with frozen Time column ---
        self.data_grid.clear()
        self.data_grid.set_left_header("Time (local)")
        self.data_grid.set_main_headers(cols)

        if not d.empty and cols:
            for _, row in d[[tcol] + cols].iterrows():
                ts = row[tcol].strftime("%Y-%m-%d %H:%M:%S")
                vals = []
                bads = []
                for c in cols:
                    val = row[c]
                    vals.append("" if pd.isna(val) else str(val))
                    is_bad = (
                        pd.isna(val)
                        or (isinstance(val, str) and val.strip() == "")
                        or (isinstance(val, (int, float, np.integer, np.floating)) and val in SENTINEL_VALUES)
                    )
                    bads.append(is_bad)
                self.data_grid.append_row(ts, vals, bg_mask=bads)

            # Header tint for columns completely missing (per NaN basis like before)
            for j, c in enumerate(cols):
                if pct.get(c, 0.0) <= 0.0:
                    item = self.data_grid.horizontal_header_item(j)
                    if item:
                        item.setBackground(QBrush(QColor("#f03e3e")))

        # ensure the frozen time column is fully readable
        self.data_grid.auto_fit_left()


@register
class MissingDataHandler(AlertHandler):
    """
    Evaluate % completeness for selected columns in the last window_minutes.
    Overall status is the worst across columns using AMBER/RED % thresholds.
    """
    kind = "MissingData"

    def default_spec(self, host: Host) -> AlertSpec:
        # --- SAFE: avoid pandas truthiness ---
        df = getattr(host, "df", None)
        if df is None:
            df = pd.DataFrame()

        tcol = _pick_time_col(df, getattr(host, "datetime_col", None))
        # default: all non-time columns
        default_cols = [c for c in list(df.columns) if c != tcol]
        return AlertSpec(
            id="",
            kind=self.kind,
            name="Missing data (completeness)",
            enabled=False,
            recipients=[],
            payload={
                "use_all": True,
                "columns": default_cols,
                "window_minutes": 24 * 60,
                "amber_pct": 95,
                "red_pct": 80,
                # email / scheduling
                "interval_min": 15,
                "email_cooldown_min": 240,
                "recipients": [],
            },
        )

    def create_editor(self, spec: AlertSpec, host: Host, parent=None):
        return _Editor(spec, host, parent)

    def create_viewer(self, spec: AlertSpec, host: Host, parent=None):
        return _MissingDataViewerDialog(spec, host, parent)

    def _choose_columns(self, df: pd.DataFrame, tcol: Optional[str], p: dict) -> List[str]:
        if bool(p.get("use_all", True)):
            return [c for c in list(df.columns) if c != tcol]
        cols = p.get("columns") or []
        return [c for c in cols if c in df.columns and c != tcol]

    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult:
        df = getattr(host, "df", None)
        if df is None or df.empty:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no data"}

        p = spec.payload or {}
        tcol = _pick_time_col(df, getattr(host, "datetime_col", None))
        if not tcol or tcol not in df.columns:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no time col"}

        cols = self._choose_columns(df, tcol, p)
        if not cols:
            return {"status": Status.OFF, "observed": 0.0, "summary": "pick columns"}

        # local tz-naive
        ts = parse_series_to_local_naive(df[tcol]).dropna()
        if ts.empty:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no valid times"}

        dff = df.copy()
        dff[tcol] = ts
        dff = dff.sort_values(tcol)

        # Window [now - window_minutes, now]
        now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)
        win_min = int(p.get("window_minutes", 24 * 60))
        t_start = now_local - pd.Timedelta(minutes=win_min)
        dff = dff[(dff[tcol] >= t_start) & (dff[tcol] <= now_local)]

        n_rows = int(dff.shape[0])
        if n_rows <= 0:
            # Nothing in window = 0% completeness across the board
            worst_pct = 0.0
            amb, red = float(p.get("amber_pct", 95)), float(p.get("red_pct", 80))
            status = Status.RED if worst_pct < red else (Status.AMBER if worst_pct < amb else Status.GREEN)
            summary = f"0 rows in window ({win_min} min) • {len(cols)} column(s)"
            return {"status": status, "observed": worst_pct, "summary": summary,
                    "extra": {"window_minutes": win_min, "columns": cols, "per_column": {}}}

        # Per-column % filled (NaN-only basis for alert semantics)
        per_col_pct: Dict[str, float] = {}
        for c in cols:
            filled = int(dff[c].notna().sum())
            per_col_pct[c] = (filled / n_rows) * 100.0

        # Overall status: worst across selected columns
        amb, red = float(p.get("amber_pct", 95)), float(p.get("red_pct", 80))
        worst_col = min(per_col_pct, key=lambda k: per_col_pct[k])
        worst_pct = float(per_col_pct[worst_col])

        if worst_pct < red:
            status = Status.RED
        elif worst_pct < amb:
            status = Status.AMBER
        else:
            status = Status.GREEN

        recipients = spec.recipients if spec.recipients else (p.get("recipients") or [])
        interval_min = int(p.get("interval_min", 15))
        cooldown = int(p.get("email_cooldown_min", 240))

        summary = (f"min filled {worst_pct:.1f}% on '{worst_col}' "
                   f"(AMBER<{amb:g}%, RED<{red:g}%) • {n_rows} rows / {win_min} min • {len(cols)} col(s) "
                   f"• recipients {len(recipients)} • every {interval_min} min")
        if cooldown > 0:
            summary += f" • cooldown {cooldown} min"

        return {
            "status": status,
            "observed": worst_pct,  # percentage (higher is better)
            "summary": summary,
            "extra": {
                "window_minutes": win_min,
                "columns": cols,
                "per_column": per_col_pct,
                "amber_pct": amb,
                "red_pct": red,
                "rows": n_rows,
            },
        }
