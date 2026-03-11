from __future__ import annotations
from typing import Dict, Any, List, Tuple, Optional
import os
import sqlite3
import re
import pandas as pd
import matplotlib.dates as mdates

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT
from matplotlib.figure import Figure

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QSpinBox, QCheckBox, QPushButton, QVBoxLayout,
    QLineEdit, QComboBox, QLabel, QToolButton, QHBoxLayout, QWidget,
    QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem
)

from PyQt6.QtCore import Qt
from utils.alerts import register, AlertSpec, AlertHandler, EvalResult, Status, Host
from utils.time_settings import local_zone, parse_series_to_local_naive
from utils.time_utils import fmt_duration


# ── Days / Hours / Minutes compound input ─────────────────────────────────────
class _DhmWidget(QWidget):
    """Compound days / hours / minutes spinbox. Value is stored as total minutes."""

    def __init__(self, total_minutes: int, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        d, rem = divmod(max(0, int(total_minutes)), 1440)
        hr, mn = divmod(rem, 60)

        self._d = QSpinBox(); self._d.setRange(0, 365);  self._d.setValue(d);  self._d.setSuffix(" d")
        self._h = QSpinBox(); self._h.setRange(0, 23);   self._h.setValue(hr); self._h.setSuffix(" h")
        self._m = QSpinBox(); self._m.setRange(0, 59);   self._m.setValue(mn); self._m.setSuffix(" m")

        for w in (self._d, self._h, self._m):
            w.setFixedWidth(72)
            h.addWidget(w)
        h.addStretch(1)

    def value_minutes(self) -> int:
        return self._d.value() * 1440 + self._h.value() * 60 + self._m.value()


# ── Table multi-select for "also_tables" ──────────────────────────────────────
def _list_db_tables(db_path: str, exclude: str) -> List[str]:
    """Return all user table names from the SQLite DB, excluding *exclude*."""
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows if r[0] != exclude]
    except Exception:
        return []


class _TablesWidget(QWidget):
    """Scrollable checklist of DB tables for co-monitoring."""

    def __init__(self, db_path: str, exclude: str, checked: List[str], parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        self._list = QListWidget(self)
        self._list.setMaximumHeight(120)
        self._list.setAlternatingRowColors(True)

        tables = _list_db_tables(db_path, exclude)
        checked_set = set(checked or [])
        if tables:
            for t in tables:
                item = QListWidgetItem(t)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if t in checked_set else Qt.CheckState.Unchecked
                )
                self._list.addItem(item)
            v.addWidget(self._list)
        else:
            lbl = QLabel("No other tables found in this database.", self)
            lbl.setStyleSheet("color: #6b7280; font-size: 11px;")
            v.addWidget(lbl)

    def checked_tables(self) -> List[str]:
        result = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result


class _Editor(QDialog):
    """
    Editor for stale-data alert:
      - Name
      - AMBER / RED thresholds as Days / Hours / Minutes
      - Also-check tables (checklist from DB)
      - Recipients / check interval / cooldown
      - Email trigger toggles
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._parent = parent
        self.setWindowTitle("Stale-data alert")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        lay.addLayout(form)

        p = self.spec.payload or {}

        # ── Name ──
        self.name_edit = QLineEdit(spec.name or "Time since last data > thresholds")
        form.addRow("Alert name:", self.name_edit)

        # ── Thresholds as D / H / M ──
        old_thr       = int(p.get("threshold_min", 30))
        amber_default = int(p.get("amber_min", old_thr))
        red_default   = int(p.get("red_min", max(amber_default * 2, 60)))

        self.thr_amber = _DhmWidget(amber_default)
        self.thr_red   = _DhmWidget(red_default)
        form.addRow("AMBER threshold ≥", self.thr_amber)
        form.addRow("RED threshold ≥",   self.thr_red)

        # ── Also check tables (checklist) ──
        db_path    = getattr(host, "db_path",    None) or ""
        table_name = getattr(host, "table_name", None) or ""
        also_now   = list(p.get("also_tables", []))
        self.also_tables_widget = _TablesWidget(db_path, table_name, also_now)
        form.addRow("Also check table(s):", self.also_tables_widget)

        # ── Recipients + interval ──
        existing_rcpts = self.spec.recipients or p.get("recipients", [])
        self.recipients_edit = QLineEdit(", ".join(existing_rcpts))
        self.recipients_edit.setPlaceholderText("alice@company.com, bob@company.com")

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 100_000)
        self.interval_spin.setValue(int(p.get("interval_min", 15)))
        self.interval_spin.setSuffix(" min")

        form.addRow("Email recipients:", self.recipients_edit)
        form.addRow("Check interval:",   self.interval_spin)

        # ── Cooldown ──
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 100_000)
        self.cooldown_spin.setValue(int(p.get("email_cooldown_min", 240)))
        self.cooldown_spin.setSuffix(" min")
        form.addRow("Email cool-down:", self.cooldown_spin)

        # ── Email trigger checkboxes ──
        self.email_amber_cb = QCheckBox("Email when entering AMBER")
        self.email_amber_cb.setChecked(bool(p.get("email_on_amber", True)))
        form.addRow(self.email_amber_cb)

        self.email_red_cb = QCheckBox("Email when entering RED")
        self.email_red_cb.setChecked(bool(p.get("email_on_red", True)))
        form.addRow(self.email_red_cb)

        self.escalation_cb = QCheckBox("Email on escalation (AMBER → RED)")
        self.escalation_cb.setChecked(bool(p.get("email_on_escalation", True)))
        form.addRow(self.escalation_cb)

        self.recovery_cb = QCheckBox("Email on recovery (→ GREEN)")
        self.recovery_cb.setChecked(bool(p.get("email_on_recovery", False)))
        form.addRow(self.recovery_cb)

        hint = QLabel(
            "Status logic (per-source): < AMBER → GREEN,  ≥ AMBER & < RED → AMBER,  ≥ RED → RED.\n"
            "Multiple tables: the best (lowest) staleness is used — RED only when ALL sources are RED."
        )
        hint.setStyleSheet("color: #6b7280; font-size: 11px;")
        lay.addWidget(hint)

        btn_ok = QPushButton("Save")
        btn_ok.clicked.connect(self.accept)
        lay.addWidget(btn_ok)

    def accept(self):
        p = self.spec.payload or {}

        # Name
        new_name = (self.name_edit.text() or "").strip()
        if new_name:
            if hasattr(self._parent, "_uniquify_name") and callable(
                getattr(self._parent, "_uniquify_name")
            ):
                self.spec.name = self._parent._uniquify_name(new_name, exclude_id=self.spec.id)
            else:
                self.spec.name = new_name

        # Thresholds
        amber = max(1, self.thr_amber.value_minutes())
        red   = self.thr_red.value_minutes()
        if red < amber:
            red = amber
        p["amber_min"]    = amber
        p["red_min"]      = red
        p["scope_all"]    = False          # always evaluate "since last"; option removed from UI
        p["threshold_min"] = amber         # legacy key preserved

        # Also-tables
        p["also_tables"] = self.also_tables_widget.checked_tables()

        # Recipients
        raw    = (self.recipients_edit.text() or "").strip()
        parts  = re.split(r"[,\s;]+", raw)
        emails = [e for e in (s.strip() for s in parts) if e and "@" in e]
        self.spec.recipients = emails
        p["recipients"] = emails

        # Scheduling / throttle / toggles
        p["interval_min"]        = int(self.interval_spin.value())
        p["email_cooldown_min"]  = int(self.cooldown_spin.value())
        p["email_on_amber"]      = bool(self.email_amber_cb.isChecked())
        p["email_on_red"]        = bool(self.email_red_cb.isChecked())
        p["email_on_escalation"] = bool(self.escalation_cb.isChecked())
        p["email_on_recovery"]   = bool(self.recovery_cb.isChecked())

        self.spec.payload = p
        super().accept()


class StaleViewDialog(QDialog):
    """
    Stale-data inspector:
      • Top bar: Range, Refresh, thresholds readout, Edit thresholds…, Export…
      • Chart: gap (minutes) vs end time of each gap (last row can be now→last)
      • Table: End time (local), Gap (minutes), Status, Note
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self._parent = parent
        self.setWindowTitle(spec.name or "Stale preview")
        self.setMinimumSize(980, 560)

        lay = QVBoxLayout(self)

        # ---- Top controls ----
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")

        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳ Refresh")
        self.refresh_btn.clicked.connect(self._rebuild)

        self.th_label = QLabel(" ")
        self.mode_label = QLabel(" ")

        self.edit_btn = QToolButton()
        self.edit_btn.setText("⚙ Edit thresholds…")
        self.edit_btn.clicked.connect(self._on_edit)

        self.export_btn = QToolButton()
        self.export_btn.setText("⬇ Export report…")
        self.export_btn.clicked.connect(self._export_report)

        ctrl.addWidget(self.range_combo)
        ctrl.addWidget(self.refresh_btn)
        ctrl.addStretch(1)
        ctrl.addWidget(self.th_label)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.mode_label)
        ctrl.addStretch(1)
        ctrl.addWidget(self.edit_btn)
        ctrl.addWidget(self.export_btn)
        lay.addLayout(ctrl)

        # ---- Matplotlib canvas + toolbar ----
        self.fig = Figure(figsize=(8.4, 4.2), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas, 1)

        # ---- Data table ----
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["End time (local)", "Gap (minutes)", "Status", "Note"])
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)

        self._rebuild()

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

    def _thresholds(self) -> tuple[int, int, bool]:
        p = self.spec.payload or {}
        amb = int(p.get("amber_min", p.get("threshold_min", 30)))
        red = int(p.get("red_min", max(amb * 2, 60)))
        if red < amb:
            red = amb
        return amb, red, bool(p.get("scope_all", False))

    def _classify(self, gap_min: float, amber: int, red: int) -> str:
        if gap_min >= red:
            return "RED"
        if gap_min >= amber:
            return "AMBER"
        return "GREEN"

    def _build_gaps(self) -> pd.DataFrame:
        df = getattr(self.host, "df", None)
        tcol = getattr(self.host, "datetime_col", None)
        if df is None or df.empty or not tcol or tcol not in df.columns:
            return pd.DataFrame(columns=["t_end", "gap_min", "status", "note"])

        ts = parse_series_to_local_naive(df[tcol]).dropna().sort_values()
        if ts.empty:
            return pd.DataFrame(columns=["t_end", "gap_min", "status", "note"])

        td = self._window_td()
        if td is not None:
            t_end = ts.max()
            t_start = t_end - td
            ts = ts[(ts >= t_start) & (ts <= t_end)]

        amber, red, scope_all = self._thresholds()

        gaps_s = ts.diff().dropna().dt.total_seconds()
        out = pd.DataFrame({
            "t_end": ts.iloc[1:],
            "gap_min": gaps_s.values / 60.0,
        })
        out["status"] = out["gap_min"].map(lambda v: self._classify(float(v), amber, red))
        out["note"] = ""

        if not scope_all and len(ts) > 0:
            now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)
            last_ts = ts.iloc[-1]
            now_gap_min = max(0.0, (now_local - last_ts).total_seconds() / 60.0)
            row = pd.DataFrame({
                "t_end": [now_local],
                "gap_min": [now_gap_min],
                "status": [self._classify(now_gap_min, amber, red)],
                "note": ["now→last"],
            })
            out = pd.concat([out, row], ignore_index=True)

        return out.sort_values("t_end")

    def _on_edit(self):
        try:
            cfg = getattr(self._parent, "configure_spec", None)
            if callable(cfg):
                cfg(self.spec)
        except Exception:
            pass
        self._rebuild()

    def _export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export stale-gaps report", "", "CSV files (*.csv)")
        if not path:
            return
        d = self._build_gaps()
        if d.empty:
            QMessageBox.information(self, "Export", "No data to export.")
            return
        amber, red, scope_all = self._thresholds()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Stale alert report\n")
                f.write(f"# Name: {self.spec.name or self.spec.kind}\n")
                f.write(f"# Range: {self.range_combo.currentText()}\n")
                f.write(f"# AMBER ≥ {amber} min\n")
                f.write(f"# RED   ≥ {red} min\n")
                f.write(f"# Mode: {'max gap' if scope_all else 'since last'}\n")
                f.write("# --- data ---\n")
            out = d.rename(columns={"t_end": "time_local", "gap_min": "gap_minutes"})
            out["time_local"] = out["time_local"].dt.strftime("%Y-%m-%d %H:%M:%S")
            out.to_csv(path, index=False, mode="a")
            QMessageBox.information(self, "Export", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    def _rebuild(self):
        self.ax.clear()
        self.fig.patch.set_facecolor("#ffffff")
        self.ax.set_facecolor("#ffffff")

        amber, red, scope_all = self._thresholds()
        self.th_label.setText(f"AMBER ≥ {amber} min   •   RED ≥ {red} min")
        self.mode_label.setText("Mode: max gap" if scope_all else "Mode: since last")

        d = self._build_gaps()
        if d.empty:
            self.ax.text(0.5, 0.5, "No data available for the selected range.",
                         ha="center", va="center", transform=self.ax.transAxes,
                         color="#9ca3af", fontsize=11)
            self.canvas.draw_idle()
            self.table.setRowCount(0)
            return

        gap_max = float(d["gap_min"].max())
        y_hi = max(gap_max * 1.15, red * 1.2, 1.0)

        # ── Coloured status bands ────────────────────────────────────────────
        self.ax.axhspan(0,              min(amber, y_hi), facecolor="#dcfce7", alpha=0.55, linewidth=0, zorder=0)
        self.ax.axhspan(min(amber,y_hi),min(red,   y_hi), facecolor="#fef9c3", alpha=0.55, linewidth=0, zorder=0)
        self.ax.axhspan(min(red,  y_hi),y_hi,             facecolor="#fee2e2", alpha=0.55, linewidth=0, zorder=0)

        # ── Threshold lines ──────────────────────────────────────────────────
        self.ax.axhline(amber, color="#f59f00", linestyle="--", linewidth=1.2,
                        alpha=0.9, zorder=2, label=f"AMBER  {amber} min")
        self.ax.axhline(red,   color="#ef4444", linestyle="--", linewidth=1.2,
                        alpha=0.9, zorder=2, label=f"RED  {red} min")

        # ── Gap line, coloured by status ─────────────────────────────────────
        _STATUS_COLOR = {"GREEN": "#16a34a", "AMBER": "#d97706", "RED": "#dc2626"}
        xs = d["t_end"].values
        ys = d["gap_min"].values
        statuses = d["status"].values
        for i in range(len(xs) - 1):
            col = _STATUS_COLOR.get(str(statuses[i]), "#2563eb")
            self.ax.plot(xs[i:i+2], ys[i:i+2], linewidth=2.2, color=col,
                         solid_capstyle="round", zorder=3)
        # Last point (no next segment) — draw a dot
        if len(xs):
            col = _STATUS_COLOR.get(str(statuses[-1]), "#2563eb")
            self.ax.plot(xs[-1], ys[-1], "o", color=col, markersize=5, zorder=4)

        # ── Axes styling ─────────────────────────────────────────────────────
        self.ax.set_ylim(0, y_hi)
        for sp in ["top", "right"]:
            self.ax.spines[sp].set_visible(False)
        self.ax.spines["left"].set_color("#d1d5db")
        self.ax.spines["bottom"].set_color("#d1d5db")
        self.ax.set_axisbelow(True)
        self.ax.yaxis.grid(True, linestyle="--", color="#e5e7eb", linewidth=0.8)
        self.ax.xaxis.grid(False)
        self.ax.tick_params(colors="#6b7280", labelsize=9)
        self.ax.set_ylabel("Gap (minutes)", color="#374151", fontsize=10, labelpad=8)
        self.ax.set_xlabel("Time (local)",  color="#374151", fontsize=10, labelpad=6)
        self.ax.set_title(self.spec.name or "Stale preview",
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

        self.table.setRowCount(0)
        for _, row in d.iterrows():
            r_i = self.table.rowCount()
            self.table.insertRow(r_i)
            self.table.setItem(r_i, 0, QTableWidgetItem(row["t_end"].strftime("%Y-%m-%d %H:%M:%S")))
            self.table.setItem(r_i, 1, QTableWidgetItem(f"{float(row['gap_min']):.2f}"))
            self.table.setItem(r_i, 2, QTableWidgetItem(str(row["status"])))
            self.table.setItem(r_i, 3, QTableWidgetItem(str(row["note"])))


@register
class StaleHandler(AlertHandler):
    kind = "Stale"

    def default_spec(self, host: Host) -> AlertSpec:
        return AlertSpec(
            id="",
            kind=self.kind,
            name="Time since last data > thresholds",
            enabled=False,
            recipients=[],
            payload={
                "amber_min": 30,
                "red_min": 60,
                "scope_all": False,
                "interval_min": 15,
                "email_cooldown_min": 240,

                # NEW: end-user controls
                "email_on_amber": True,
                "email_on_red": True,

                # Existing controls
                "email_on_escalation": True,
                "email_on_recovery": False,

                "recipients": [],
                "threshold_min": 30,  # legacy
                "also_tables": [],
            },
        )

    # ---------- Helpers ----------
    @staticmethod
    def _candidate_time_cols(prefer: Optional[str], all_cols: List[str]) -> List[str]:
        common = ["datetime", "timestamp", "ts", "created_at", "received_at", "date", "sent", "received"]
        ordered: List[str] = []
        if prefer and prefer in all_cols:
            ordered.append(prefer)
        for c in common:
            if c in all_cols and c != prefer:
                ordered.append(c)
        for c in all_cols:
            if c not in ordered:
                ordered.append(c)
        return ordered

    def _load_times_for_table(self, db_path: Optional[str], table: str, prefer_col: Optional[str]) -> pd.Series:
        if not db_path or not os.path.isfile(db_path):
            return pd.Series(dtype="datetime64[ns]")

        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({table})")
            rows = cur.fetchall()
            cols = [r[1] for r in rows] if rows else []
            if not cols:
                conn.close()
                return pd.Series(dtype="datetime64[ns]")

            for col in self._candidate_time_cols(prefer_col, cols):
                try:
                    q = f'SELECT "{col}" AS dt FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY 1 ASC'
                    s = pd.read_sql_query(q, conn)["dt"]
                    conn.close()
                    return parse_series_to_local_naive(s).dropna().sort_values()
                except Exception:
                    conn.rollback()
                    continue
            conn.close()
        except Exception:
            pass

        return pd.Series(dtype="datetime64[ns]")

    @staticmethod
    def _combine_observed(observed_list: List[Tuple[str, float]]) -> float:
        if not observed_list:
            return float("inf")
        return min(x for _, x in observed_list)

    def _observed_for_series(self, ts: pd.Series, scope_all: bool) -> float:
        if ts is None or ts.empty:
            return float("inf")
        if scope_all:
            diffs = ts.diff().dropna().dt.total_seconds()
            return float(diffs.max()) if len(diffs) else float("inf")
        now_local = pd.Timestamp.now(tz=local_zone()).tz_localize(None)
        return float((now_local - ts.iloc[-1]).total_seconds())

    # ---------- UI ----------
    def create_editor(self, spec: AlertSpec, host: Host, parent=None):
        return _Editor(spec, host, parent)

    def create_viewer(self, spec: AlertSpec, host: Host, parent=None) -> QDialog:
        return StaleViewDialog(spec, host, parent)

    # ---------- Core evaluation ----------
    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult:
        df = getattr(host, "df", None)
        tcol = getattr(host, "datetime_col", None)

        if df is None or df.empty or not tcol or tcol not in (df.columns if df is not None else []):
            primary_ts = pd.Series(dtype="datetime64[ns]")
        else:
            primary_ts = parse_series_to_local_naive(df[tcol]).dropna().sort_values()

        p = spec.payload or {}
        amber_min = int(p.get("amber_min", p.get("threshold_min", 30)))
        red_min = int(p.get("red_min", max(amber_min * 2, 60)))
        if red_min < amber_min:
            red_min = amber_min
        amber_s = float(amber_min * 60)
        red_s = float(red_min * 60)

        scope_all = bool(p.get("scope_all", False))

        observed_parts: List[Tuple[str, float]] = []
        primary_obs = self._observed_for_series(primary_ts, scope_all)
        if primary_obs != float("inf"):
            observed_parts.append((getattr(host, "table_name", "this_table"), primary_obs))

        also_tables: List[str] = list(p.get("also_tables", [])) or []
        db_path = getattr(host, "db_path", None) or os.environ.get("BUOY_DB") or None

        for tname in also_tables:
            ts_other = self._load_times_for_table(db_path, tname, prefer_col=tcol)
            obs = self._observed_for_series(ts_other, scope_all)
            if obs != float("inf"):
                observed_parts.append((tname, obs))

        if not observed_parts:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no valid times in any selected table"}

        combined_observed = self._combine_observed(observed_parts)

        if combined_observed >= red_s:
            status = Status.RED
        elif combined_observed >= amber_s:
            status = Status.AMBER
        else:
            status = Status.GREEN

        recipients = spec.recipients or (p.get("recipients") or [])
        interval_min = int(p.get("interval_min", 15))
        cooldown = int(p.get("email_cooldown_min", 240))

        parts = [f"{name}:{fmt_duration(secs)}" for name, secs in observed_parts]
        per_table = " | ".join(parts)
        mode_str = "max gap" if scope_all else "since last"

        summary = (
            f"{fmt_duration(combined_observed)} (amber≥{fmt_duration(amber_s)}, red≥{fmt_duration(red_s)}) • "
            f"{mode_str} • sources={len(observed_parts)} [{per_table}] • "
            f"recipients {len(recipients)} • every {interval_min} min"
        )
        if cooldown > 0:
            summary += f" • cooldown {cooldown} min"

        return {
            "status": status,
            "observed": combined_observed,
            "summary": summary,
            "extra": {
                "amber_min": amber_min,
                "red_min": red_min,
                "scope_all": scope_all,
                "sources": [n for n, _ in observed_parts],
            },
        }
