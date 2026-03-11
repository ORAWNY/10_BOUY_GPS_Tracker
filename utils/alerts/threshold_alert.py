# utils/alerts/threshold_alert.py
from __future__ import annotations
import re
from typing import Optional, Tuple

import pandas as pd
import matplotlib.dates as mdates
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT
from matplotlib.figure import Figure

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QComboBox, QDoubleSpinBox, QCheckBox, QVBoxLayout, QPushButton,
    QLabel, QLineEdit, QSpinBox, QHBoxLayout, QToolButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QMessageBox
)

from utils.alerts import register, AlertSpec, AlertHandler, EvalResult, Status, Host
from utils.time_settings import parse_series_to_local_naive
from utils.constants import is_battery_column


# --------------------------- Editor (unchanged) ---------------------------

class _Editor(QDialog):
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._parent = parent  # may be AlertsTab (for _uniquify_name)
        self.setWindowTitle("Generic threshold (R/A/G)")

        lay = QVBoxLayout(self)
        form = QFormLayout(); lay.addLayout(form)

        df = getattr(host, "df", None)
        cols = list(df.columns) if df is not None else []

        p = self.spec.payload

        # --- Name ---
        self.name_edit = QLineEdit(spec.name or "Generic threshold exceeded (R/A/G)")
        form.addRow("Alert name:", self.name_edit)

        # Column + mode
        self.col = QComboBox(); self.col.addItems(cols)
        if p.get("column") in cols: self.col.setCurrentText(p.get("column"))

        self.mode = QComboBox(); self.mode.addItems(["greater", "less"])
        self.mode.setCurrentText(p.get("mode", "greater"))

        # Red/Amber/Green numeric thresholds
        self.red = QDoubleSpinBox(); self.red.setRange(-1e12, 1e12); self.red.setDecimals(6); self.red.setValue(float(p.get("red", 0)))
        self.amb = QDoubleSpinBox(); self.amb.setRange(-1e12, 1e12); self.amb.setDecimals(6); self.amb.setValue(float(p.get("amber", 0)))
        self.grn = QDoubleSpinBox(); self.grn.setRange(-1e12, 1e12); self.grn.setDecimals(6); self.grn.setValue(float(p.get("green", 0)))

        # Scope
        self.scope = QComboBox(); self.scope.addItems(["most_recent", "max", "min", "mean"])
        self.scope.setCurrentText(p.get("scope", "most_recent"))

        # Recipients + interval
        existing_rcpts = self.spec.recipients or p.get("recipients", [])
        self.recipients_edit = QLineEdit(", ".join(existing_rcpts))
        self.recipients_edit.setPlaceholderText("alice@company.com, bob@company.com")
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(1, 100000); self.interval_spin.setValue(int(p.get("interval_min", 15)))

        # Email throttle
        self.cooldown_spin = QSpinBox(); self.cooldown_spin.setRange(0, 100000); self.cooldown_spin.setValue(int(p.get("email_cooldown_min", 240)))
        self.escalation_combo = QComboBox(); self.escalation_combo.addItems(["No", "Yes"])
        self.escalation_combo.setCurrentIndex(1 if p.get("email_on_escalation", False) else 0)
        self.recovery_combo = QComboBox(); self.recovery_combo.addItems(["No", "Yes"])
        self.recovery_combo.setCurrentIndex(1 if p.get("email_on_recovery", False) else 0)

        # Battery alert flag — auto-detected from column name, but user can override
        _initial_batt = bool(p.get("is_battery", False)) or is_battery_column(p.get("column", ""))
        self.is_battery_check = QCheckBox("Battery alert (voltage / charge monitoring)")
        self.is_battery_check.setChecked(_initial_batt)
        self.is_battery_check.setToolTip(
            "Auto-ticked when the column name contains 'batt' or 'volt'. "
            "Battery alerts appear in the Battery column; "
            "unticked alerts appear in the Alerts column."
        )
        self.col.currentTextChanged.connect(
            lambda col_name: self.is_battery_check.setChecked(is_battery_column(col_name))
        )

        form.addRow("Column:", self.col)
        form.addRow("Mode:", self.mode)
        form.addRow("Red threshold:", self.red)
        form.addRow("Amber threshold:", self.amb)
        form.addRow("Green threshold:", self.grn)
        form.addRow("Scope:", self.scope)
        form.addRow("Battery alert:", self.is_battery_check)
        form.addRow("Email recipients:", self.recipients_edit)
        form.addRow("Check interval (min):", self.interval_spin)
        form.addRow("Email cool-down (min):", self.cooldown_spin)
        form.addRow("Email on escalation (AMBER→RED):", self.escalation_combo)
        form.addRow("Email on recovery (→GREEN):", self.recovery_combo)

        hint = QLabel(
            "Emails are sent on GREEN→AMBER/RED transitions. AMBER→RED emails are optional (escalation). "
            "Recovery (→GREEN) emails are optional. All emails respect the cool-down."
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(hint)

        btn = QPushButton("OK"); btn.clicked.connect(self.accept); lay.addWidget(btn)

    def accept(self):
        p = self.spec.payload

        # Name (uniquify via AlertsTab if available)
        new_name = (self.name_edit.text() or "").strip()
        if new_name:
            if hasattr(self._parent, "_uniquify_name") and callable(getattr(self._parent, "_uniquify_name")):
                self.spec.name = self._parent._uniquify_name(new_name, exclude_id=self.spec.id)
            else:
                self.spec.name = new_name

        p["column"] = self.col.currentText()
        p["mode"] = self.mode.currentText()
        p["is_battery"] = self.is_battery_check.isChecked()
        p["red"] = float(self.red.value())
        p["amber"] = float(self.amb.value())
        p["green"] = float(self.grn.value())
        p["scope"] = self.scope.currentText()

        # Recipients (comma / semicolon / whitespace)
        raw = (self.recipients_edit.text() or "").strip()
        parts = re.split(r"[,\s;]+", raw)
        emails = [e for e in (s.strip() for s in parts) if e and "@" in e]
        self.spec.recipients = emails
        p["recipients"] = emails

        # Interval & cool-down + email toggles
        p["interval_min"] = int(self.interval_spin.value())
        p["email_cooldown_min"] = int(self.cooldown_spin.value())
        p["email_on_escalation"] = (self.escalation_combo.currentIndex() == 1)
        p["email_on_recovery"] = (self.recovery_combo.currentIndex() == 1)

        super().accept()


# --------------------------- Viewer (new) ---------------------------

class _ThresholdViewer(QDialog):
    """
    Standalone Threshold preview (plot + table + export).
    Mirrors the old inlined viewer from AlertsTab, now provided by the handler.
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None, handler: Optional[AlertHandler] = None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self._parent = parent     # AlertsTab (if available) for configure_spec()
        self._handler = handler   # for fallback editor
        self.setWindowTitle(spec.name or "Threshold preview")
        self.setMinimumSize(980, 560)

        lay = QVBoxLayout(self)

        # --- Top controls -----------------------------------------------------
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")
        self.refresh_btn = QToolButton(); self.refresh_btn.setText("⟳ Refresh")
        self.refresh_btn.clicked.connect(self._rebuild)

        self.pct_label = QLabel(" ")  # Green / Amber / Red %
        self.pct_label.setStyleSheet("font-weight: 600;")

        self.edit_btn = QToolButton(); self.edit_btn.setText("⚙ Edit thresholds…")
        self.edit_btn.clicked.connect(self._on_edit)

        self.export_btn = QToolButton(); self.export_btn.setText("⬇ Export report…")
        self.export_btn.clicked.connect(self._export_report)

        ctrl.addWidget(self.range_combo)
        ctrl.addWidget(self.refresh_btn)
        ctrl.addStretch(1)
        ctrl.addWidget(self.pct_label)
        ctrl.addStretch(1)
        ctrl.addWidget(self.edit_btn)
        ctrl.addWidget(self.export_btn)
        lay.addLayout(ctrl)

        # --- Matplotlib canvas + toolbar -------------------------------------
        self.fig = Figure(figsize=(8.4, 4.2), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas, 1)

        # --- Data table -------------------------------------------------------
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Time (local)", "Value", "Status"])
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)

        self._rebuild()

    # -------- utilities --------
    def _pick_time_col(self, df: pd.DataFrame) -> Optional[str]:
        # Prefer the column the host already identified as the datetime column
        host_col = getattr(self.host, "datetime_col", None)
        if host_col and host_col in df.columns:
            return host_col
        for c in ["__dt_iso", "timestamp", "time", "datetime", "date", "DateTime"]:
            if c in df.columns:
                return c
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                return c
        return None

    def _window_td(self) -> Optional[pd.Timedelta]:
        m = {
            "6 h": pd.Timedelta(hours=6),
            "12 h": pd.Timedelta(hours=12),
            "24 h": pd.Timedelta(hours=24),
            "3 d": pd.Timedelta(days=3),
            "7 d": pd.Timedelta(days=7),
            "30 d": pd.Timedelta(days=30),
        }
        return m.get(self.range_combo.currentText(), None)  # None → All

    def _classify(self, v: float) -> str:
        mode = str(self.spec.payload.get("mode", "greater"))
        r = float(self.spec.payload.get("red", 0.0))
        a = float(self.spec.payload.get("amber", 0.0))
        g = float(self.spec.payload.get("green", 0.0))
        if mode == "greater":
            if v >= r: return "RED"
            if v >= a: return "AMBER"
            return "GREEN"
        else:  # 'less' → lower is worse
            if v <= r: return "RED"
            if v <= a: return "AMBER"
            return "GREEN"

    def _compute_time_share(self, d: pd.DataFrame, tcol: str, vcol: str) -> tuple[dict, pd.DataFrame]:
        """
        Returns ({'GREEN': p, 'AMBER': p, 'RED': p}, d_with_status_and_duration)
        Time-weighted by the interval until the next row; last row extends to window end.
        """
        if d.empty:
            return {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}, d

        d2 = d[[tcol, vcol]].copy()
        d2["Status"] = d2[vcol].map(self._classify)

        d2["t_next"] = d2[tcol].shift(-1)
        window_end = d2[tcol].iloc[-1]
        d2.loc[d2.index[-1], "t_next"] = window_end
        d2["dur_s"] = (d2["t_next"] - d2[tcol]).dt.total_seconds().clip(lower=0).fillna(0.0)

        totals = d2.groupby("Status")["dur_s"].sum()
        total_s = float(totals.sum())
        share = {
            "GREEN": (totals.get("GREEN", 0.0) / total_s * 100.0) if total_s > 0 else 0.0,
            "AMBER": (totals.get("AMBER", 0.0) / total_s * 100.0) if total_s > 0 else 0.0,
            "RED": (totals.get("RED", 0.0) / total_s * 100.0) if total_s > 0 else 0.0,
        }
        return share, d2.drop(columns=["t_next"])

    # -------- actions --------
    def _on_edit(self):
        # Prefer the host app path so audits/baselines happen consistently
        try:
            if hasattr(self._parent, "configure_spec") and callable(getattr(self._parent, "configure_spec")):
                self._parent.configure_spec(self.spec)
            elif self._handler is not None and hasattr(self._handler, "create_editor"):
                dlg = self._handler.create_editor(self.spec, self.host, self)
                if dlg.exec():
                    pass
        except Exception:
            pass
        self._rebuild()

    def _export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export threshold report", "", "CSV files (*.csv)")
        if not path:
            return
        d24, tcol, vcol, share = self._current_windowed()
        try:
            _, d_status = self._compute_time_share(d24, tcol, vcol)
            out = d_status.rename(columns={tcol: "time_local", vcol: "value", "Status": "status", "dur_s": "duration_seconds"})
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Threshold alert report\n")
                f.write(f"# Name: {self.spec.name or self.spec.kind}\n")
                f.write(f"# Range: {self.range_combo.currentText()}\n")
                f.write(f"# Percent in GREEN: {share['GREEN']:.1f}%\n")
                f.write(f"# Percent in AMBER: {share['AMBER']:.1f}%\n")
                f.write(f"# Percent in RED:   {share['RED']:.1f}%\n")
                f.write("# --- data ---\n")
            out.to_csv(path, index=False, mode="a")
            QMessageBox.information(self, "Export", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    # -------- main builder --------
    def _current_windowed(self):
        df = getattr(self.host, "df", None)
        if df is None or df.empty:
            return pd.DataFrame(), "", "", {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}

        vcol = self.spec.payload.get("column", "")
        if not vcol or vcol not in df.columns:
            return pd.DataFrame(), "", "", {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}

        tcol = self._pick_time_col(df)
        if not tcol:
            return pd.DataFrame(), "", "", {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}

        d = df[[tcol, vcol]].copy()
        d[tcol] = parse_series_to_local_naive(d[tcol])
        d[vcol] = pd.to_numeric(d[vcol], errors="coerce")
        d = d.dropna(subset=[tcol, vcol]).sort_values(tcol)
        if d.empty:
            return d, tcol, vcol, {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}

        td = self._window_td()
        if td is not None:
            t_end = d[tcol].max()
            t_start = t_end - td
            d = d[(d[tcol] >= t_start) & (d[tcol] <= t_end)]

        share, _ = self._compute_time_share(d, tcol, vcol)
        return d, tcol, vcol, share

    def _rebuild(self):
        self.ax.clear()
        self.fig.patch.set_facecolor("#ffffff")
        self.ax.set_facecolor("#ffffff")

        d, tcol, vcol, share = self._current_windowed()
        if d.empty:
            self.ax.text(0.5, 0.5, "No data available for the selected range.",
                         ha="center", va="center", transform=self.ax.transAxes,
                         color="#9ca3af", fontsize=11)
            self.canvas.draw_idle()
            self.table.setRowCount(0)
            self.pct_label.setText(" ")
            return

        mode = str(self.spec.payload.get("mode", "greater"))
        r    = float(self.spec.payload.get("red",   0.0))
        a    = float(self.spec.payload.get("amber", 0.0))

        y_min = float(d[vcol].min())
        y_max = float(d[vcol].max())
        pad   = (y_max - y_min) * 0.12 if y_max != y_min else max(1.0, abs(y_max)) * 0.12
        y_lo, y_hi = y_min - pad, y_max + pad

        # ── Coloured status bands (drawn first, behind everything) ──────────
        if mode == "greater":
            self.ax.axhspan(y_lo,          min(a, y_hi),  facecolor="#dcfce7", alpha=0.55, linewidth=0, zorder=0)
            self.ax.axhspan(min(a, y_hi),  min(r, y_hi),  facecolor="#fef9c3", alpha=0.55, linewidth=0, zorder=0)
            self.ax.axhspan(min(r, y_hi),  y_hi,          facecolor="#fee2e2", alpha=0.55, linewidth=0, zorder=0)
        else:
            self.ax.axhspan(y_lo,          min(r, y_hi),  facecolor="#fee2e2", alpha=0.55, linewidth=0, zorder=0)
            self.ax.axhspan(min(r, y_hi),  min(a, y_hi),  facecolor="#fef9c3", alpha=0.55, linewidth=0, zorder=0)
            self.ax.axhspan(min(a, y_hi),  y_hi,          facecolor="#dcfce7", alpha=0.55, linewidth=0, zorder=0)

        # ── Threshold lines ──────────────────────────────────────────────────
        self.ax.axhline(r, color="#ef4444", linestyle="--", linewidth=1.2, alpha=0.9, zorder=2, label=f"RED  {r:g}")
        self.ax.axhline(a, color="#f59f00", linestyle="--", linewidth=1.2, alpha=0.9, zorder=2, label=f"AMBER  {a:g}")

        # ── Data line ────────────────────────────────────────────────────────
        self.ax.plot(d[tcol], d[vcol], linewidth=2, color="#2563eb",
                     zorder=3, solid_capstyle="round", solid_joinstyle="round")

        # ── Axes limits & styling ────────────────────────────────────────────
        self.ax.set_ylim(y_lo, y_hi)
        for sp in ["top", "right"]:
            self.ax.spines[sp].set_visible(False)
        self.ax.spines["left"].set_color("#d1d5db")
        self.ax.spines["bottom"].set_color("#d1d5db")
        self.ax.set_axisbelow(True)
        self.ax.yaxis.grid(True, linestyle="--", color="#e5e7eb", linewidth=0.8)
        self.ax.xaxis.grid(False)
        self.ax.tick_params(colors="#6b7280", labelsize=9)
        self.ax.set_ylabel(vcol, color="#374151", fontsize=10, labelpad=8)
        self.ax.set_xlabel("Time (local)", color="#374151", fontsize=10, labelpad=6)
        self.ax.set_title(self.spec.name or "Threshold preview",
                          color="#111827", fontsize=12, fontweight="bold", pad=10)

        locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
        self.ax.xaxis.set_major_locator(locator)
        try:
            self.ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        except Exception:
            pass

        self.ax.legend(loc="best", fontsize=8, framealpha=0.92,
                       edgecolor="#e5e7eb", facecolor="#ffffff")
        self.fig.tight_layout(pad=1.4)
        self.canvas.draw_idle()

        # ── Percent readout ──────────────────────────────────────────────────
        g_pct = share["GREEN"]
        a_pct = share["AMBER"]
        r_pct = share["RED"]
        self.pct_label.setText(
            f"<span style='color:#16a34a'>● {g_pct:.1f}% OK</span>"
            f"&nbsp;&nbsp;•&nbsp;&nbsp;"
            f"<span style='color:#d97706'>● {a_pct:.1f}% AMBER</span>"
            f"&nbsp;&nbsp;•&nbsp;&nbsp;"
            f"<span style='color:#dc2626'>● {r_pct:.1f}% RED</span>"
        )
        self.pct_label.setTextFormat(Qt.TextFormat.RichText)

        # Table (with status)
        self.table.setRowCount(0)
        for _, row in d[[tcol, vcol]].iterrows():
            r_i = self.table.rowCount()
            self.table.insertRow(r_i)
            self.table.setItem(r_i, 0, QTableWidgetItem(row[tcol].strftime("%Y-%m-%d %H:%M:%S")))
            self.table.setItem(r_i, 1, QTableWidgetItem(f"{row[vcol]:.6g}"))
            self.table.setItem(r_i, 2, QTableWidgetItem(self._classify(float(row[vcol]))))


# --------------------------- Handler ---------------------------

@register
class ThresholdHandler(AlertHandler):
    kind = "Threshold"

    def default_spec(self, host: Host) -> AlertSpec:
        first_numeric = ""
        df = getattr(host, "df", None)
        if df is not None:
            for c in df.columns:
                if pd.api.types.is_numeric_dtype(df[c]):
                    first_numeric = c
                    break

        return AlertSpec(
            id="",
            kind=self.kind,
            name="Generic threshold exceeded (R/A/G)",
            enabled=False,
            recipients=[],
            payload={
                "column": first_numeric,
                "mode": "greater",  # higher is worse
                "red": 100.0,
                "amber": 80.0,
                "green": 0.0,
                "scope": "most_recent",
                "is_battery": False,
                # email / scheduling
                "interval_min": 15,
                "email_cooldown_min": 240,
                "email_on_escalation": False,
                "email_on_recovery": False,
                "recipients": [],
            }
        )

    def create_editor(self, spec: AlertSpec, host: Host, parent=None):
        return _Editor(spec, host, parent)

    # NEW: viewer provided by the handler
    def create_viewer(self, spec: AlertSpec, host: Host, parent=None):
        return _ThresholdViewer(spec, host, parent, handler=self)

    def _observe(self, df: pd.DataFrame, col: str, scope: str) -> float:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            return float("nan")
        if scope == "most_recent":
            return float(s.iloc[-1])
        if scope == "max":
            return float(s.max())
        if scope == "min":
            return float(s.min())
        if scope == "mean":
            return float(s.mean())
        return float(s.iloc[-1])

    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult:
        df = getattr(host, "df", None)
        if df is None or df.empty:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no data"}

        p = spec.payload
        col = p.get("column")
        if not col or col not in df.columns:
            return {"status": Status.OFF, "observed": 0.0, "summary": "pick a column"}

        observed = self._observe(df, col, p.get("scope", "most_recent"))
        if pd.isna(observed):
            return {"status": Status.OFF, "observed": 0.0, "summary": "no numeric data"}

        mode = str(p.get("mode", "greater"))
        red, amber, green = float(p.get("red", 0)), float(p.get("amber", 0)), float(p.get("green", 0))

        if mode == "greater":
            if observed >= red:
                status, eff_thr = Status.RED, red
            elif observed >= amber:
                status, eff_thr = Status.AMBER, amber
            else:
                status, eff_thr = Status.GREEN, amber
        else:
            # lower is worse
            if observed <= red:
                status, eff_thr = Status.RED, red
            elif observed <= amber:
                status, eff_thr = Status.AMBER, amber
            else:
                status, eff_thr = Status.GREEN, amber

        recipients = spec.recipients if spec.recipients else (p.get("recipients") or [])
        interval_min = int(p.get("interval_min", 15))
        cooldown = int(p.get("email_cooldown_min", 240))

        summary = (
            f"{col}={observed:.3f} (R:{red:g} A:{amber:g} G:{green:g}) • "
            f"scope {p.get('scope', 'most_recent')} • recipients {len(recipients)} • every {interval_min} min"
        )
        if cooldown > 0:
            summary += f" • cooldown {cooldown} min"

        return {
            "status": status,
            "observed": float(observed),
            "summary": summary,
            "extra": {
                "threshold": eff_thr,
                "mode": mode,
                "column": col,
                "scope": p.get("scope", "most_recent"),
            }
        }
