# utils/alerts/missing_data_alert.py
from __future__ import annotations

from typing import List, Optional, Dict, Any
import re
import pandas as pd

# --- add/extend imports at the top of missing_data_alert.py ---
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QSpinBox, QCheckBox, QPushButton, QVBoxLayout, QLineEdit,
    QLabel, QListWidget, QListWidgetItem, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QToolButton, QComboBox, QFileDialog, QMessageBox
)
from PyQt6.QtGui import QColor, QBrush

from utils.alerts import REGISTRY, register, AlertSpec, AlertHandler, EvalResult, Status, Host

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

        df = getattr(host, "df", None)
        if df is None:
            df = pd.DataFrame()

        tcol = _pick_time_col(df, getattr(host, "datetime_col", None))

        # ---- Column picker ----
        all_cols = [c for c in list(df.columns) if c != tcol]
        p = self.spec.payload or {}

        self.use_all_cb = QCheckBox("Use ALL columns")
        self.use_all_cb.setChecked(bool(p.get("use_all", True)))
        form.addRow(self.use_all_cb)

        self.cols_list = QListWidget(self)
        self.cols_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        for c in all_cols:
            it = QListWidgetItem(c)
            self.cols_list.addItem(it)

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

# --- add this class anywhere below _Editor, above MissingDataHandler (or just above the handler methods) ---

class _MissingDataViewerDialog(QDialog):
    """
    MissingData inspector:
      • Top bar: Range, Refresh, thresholds & selected columns, Edit…, Export…
      • Header: % rows populated per selected column (in current window)
      • Table: Time (local) + selected columns; missing values highlighted
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self._parent = parent
        self.setWindowTitle(spec.name or "Missing data preview")
        self.setMinimumSize(980, 560)

        lay = QVBoxLayout(self)

        # Top controls
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳ Refresh")
        self.refresh_btn.clicked.connect(self._rebuild)
        self.th_label = QLabel(" ")
        self.cols_label = QLabel(" ")

        self.edit_btn = QToolButton()
        self.edit_btn.setText("⚙ Edit…")
        self.edit_btn.clicked.connect(self._on_edit)
        self.export_btn = QToolButton()
        self.export_btn.setText("⬇ Export…")
        self.export_btn.clicked.connect(self._export_report)

        ctrl.addWidget(self.range_combo)
        ctrl.addWidget(self.refresh_btn)
        ctrl.addStretch(1)
        ctrl.addWidget(self.th_label)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.cols_label)
        ctrl.addStretch(1)
        ctrl.addWidget(self.edit_btn)
        ctrl.addWidget(self.export_btn)
        lay.addLayout(ctrl)

        # Summary % per column
        self.summary_lbl = QLabel(" ")
        self.summary_lbl.setStyleSheet("font-weight: 600;")
        lay.addWidget(self.summary_lbl)

        # Table
        self.table = QTableWidget(0, 1, self)
        self.table.setHorizontalHeaderLabels(["Time (local)"])
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)

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
        # prefer the host's configured datetime column if present
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
            out = dfw[[tcol] + cols].copy()
            out = out.rename(columns={tcol: "time_local"})
            out["time_local"] = out["time_local"].dt.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "w", encoding="utf-8") as f:
                p = self.spec.payload or {}
                f.write("# Missing data report\n")
                f.write(f"# Name: {self.spec.name or self.spec.kind}\n")
                f.write(f"# Range: {self.range_combo.currentText()}\n")
                f.write(f"# AMBER<{int(p.get('amber_pct', 95))}%, RED<{int(p.get('red_pct', 80))}%\n")
                for c in cols:
                    f.write(f"# {c}: {pct.get(c, 0.0):.1f}% filled\n")
                f.write("# --- data ---\n")
            out.to_csv(path, index=False, mode="a")
            QMessageBox.information(self, "Export", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    # -------- data slicing --------
    def _current_windowed(self):
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

    # -------- builder --------
    def _rebuild(self):
        p = self.spec.payload or {}
        amber = int(p.get("amber_pct", 95))
        red = int(p.get("red_pct", 80))
        self.th_label.setText(f"AMBER<{amber}%   •   RED<{red}%")

        d, tcol, cols, pct = self._current_windowed()
        self.cols_label.setText(f"Columns: {', '.join(cols) if cols else '(none)'}")

        # Summary line
        if pct:
            txt = "   •   ".join(f"{c}: {pct[c]:.1f}%" for c in cols)
        else:
            txt = "No data"
        self.summary_lbl.setText(txt)

        # Table
        self.table.setRowCount(0)
        headers = ["Time (local)"] + cols
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)

        if d.empty or not cols:
            return

        # Fill rows; color missing cells amber; if a column is 0% filled, tint its header red
        for _, row in d[[tcol] + cols].iterrows():
            r_i = self.table.rowCount()
            self.table.insertRow(r_i)
            self.table.setItem(r_i, 0, QTableWidgetItem(row[tcol].strftime("%Y-%m-%d %H:%M:%S")))
            for j, c in enumerate(cols, start=1):
                val = row[c]
                item = QTableWidgetItem("" if pd.isna(val) else str(val))
                if pd.isna(val):
                    item.setBackground(QBrush(QColor("#f59f00")))  # amber
                self.table.setItem(r_i, j, item)

        # Header tint for columns completely missing
        for j, c in enumerate(cols, start=1):
            if pct.get(c, 0.0) <= 0.0:
                self.table.horizontalHeaderItem(j).setBackground(QBrush(QColor("#f03e3e")))



@register
class MissingDataHandler(AlertHandler):
    """
    Evaluate % completeness for selected columns in the last window_minutes.
    Overall status is the worst across columns using AMBER/RED % thresholds.
    """
    kind = "MissingData"

    def default_spec(self, host: Host) -> AlertSpec:
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

        # Irish local, tz-naive
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

        # Per-column % filled
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
