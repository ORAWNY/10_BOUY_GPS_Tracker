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
    QLineEdit, QComboBox, QLabel, QToolButton, QHBoxLayout,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem
)

from utils.alerts import register, AlertSpec, AlertHandler, EvalResult, Status, Host
from utils.time_settings import local_zone, parse_series_to_local_naive


def fmt_duration(secs: float) -> str:
    secs = int(max(0, secs))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d: return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h: return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


class _Editor(QDialog):
    """
    Editor now supports 'Also check table(s)' — a comma/space/semicolon separated
    list of *other* email tables to co-monitor with this one.
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._parent = parent  # AlertsTab (for _uniquify_name)
        self.setWindowTitle("Stale-data alert")

        lay = QVBoxLayout(self)
        form = QFormLayout(); lay.addLayout(form)

        p = self.spec.payload or {}

        # Name
        self.name_edit = QLineEdit(spec.name or "Time since last data > thresholds")
        form.addRow("Alert name:", self.name_edit)

        # Thresholds (minutes)
        old_thr = int(p.get("threshold_min", 30))
        amber_default = int(p.get("amber_min", old_thr))
        red_default = int(p.get("red_min", max(amber_default * 2, 60)))

        self.thr_amber = QSpinBox(); self.thr_amber.setRange(1, 10_000_000); self.thr_amber.setValue(amber_default)
        self.thr_red   = QSpinBox(); self.thr_red.setRange(1, 10_000_000);   self.thr_red.setValue(red_default)
        self.scope_all = QCheckBox("Use ALL data (alert on maximum gap)"); self.scope_all.setChecked(bool(p.get("scope_all", False)))

        form.addRow("AMBER at (minutes) ≥", self.thr_amber)
        form.addRow("RED at (minutes) ≥", self.thr_red)
        form.addRow(self.scope_all)

        # ---- NEW: Additional tables to co-monitor ----
        also_tables = ", ".join(p.get("also_tables", []))
        self.also_tables_edit = QLineEdit(also_tables)
        self.also_tables_edit.setPlaceholderText("e.g. support_inbox, accounts_inbox")
        form.addRow("Also check table(s):", self.also_tables_edit)

        # Recipients + interval
        existing_rcpts = self.spec.recipients or p.get("recipients", [])
        self.recipients_edit = QLineEdit(", ".join(existing_rcpts))
        self.recipients_edit.setPlaceholderText("alice@company.com, bob@company.com")
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(1, 100000); self.interval_spin.setValue(int(p.get("interval_min", 15)))
        form.addRow("Email recipients:", self.recipients_edit)
        form.addRow("Check interval (min):", self.interval_spin)

        # Email throttle & toggles
        self.cooldown_spin = QSpinBox(); self.cooldown_spin.setRange(0, 100000); self.cooldown_spin.setValue(int(p.get("email_cooldown_min", 240)))
        self.escalation_combo = QComboBox(); self.escalation_combo.addItems(["No", "Yes"]); self.escalation_combo.setCurrentIndex(1 if p.get("email_on_escalation", True) else 0)
        self.recovery_combo   = QComboBox(); self.recovery_combo.addItems(["No", "Yes"]);   self.recovery_combo.setCurrentIndex(1 if p.get("email_on_recovery", False) else 0)
        form.addRow("Email cool-down (min):", self.cooldown_spin)
        form.addRow("Email on escalation (AMBER→RED):", self.escalation_combo)
        form.addRow("Email on recovery (→GREEN):", self.recovery_combo)

        hint = QLabel(
            "Status logic (per-source): < AMBER → GREEN, ≥ AMBER & < RED → AMBER, ≥ RED → RED.\n"
            "Multiple tables: we use the best (lowest) staleness among them, so RED happens only if ALL are RED.\n"
            "‘Use ALL data’ evaluates the maximum historical gap per table; otherwise it evaluates ‘time since last’ per table."
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(hint)

        btn_ok = QPushButton("OK"); btn_ok.clicked.connect(self.accept); lay.addWidget(btn_ok)

    def accept(self):
        p = self.spec.payload or {}

        # Name (uniquify if AlertsTab provided)
        new_name = (self.name_edit.text() or "").strip()
        if new_name:
            if hasattr(self._parent, "_uniquify_name") and callable(getattr(self._parent, "_uniquify_name")):
                self.spec.name = self._parent._uniquify_name(new_name, exclude_id=self.spec.id)
            else:
                self.spec.name = new_name

        # Thresholds
        amber = int(self.thr_amber.value()); red = int(self.thr_red.value())
        if red < amber: red = amber
        p["amber_min"] = amber
        p["red_min"]   = red
        p["scope_all"] = bool(self.scope_all.isChecked())
        p["threshold_min"] = amber  # legacy key preserved

        # NEW: also_tables
        raw_also = (self.also_tables_edit.text() or "").strip()
        parts = re.split(r"[,\s;]+", raw_also)
        also_tables = [t for t in (s.strip() for s in parts) if t]
        p["also_tables"] = also_tables

        # Recipients, interval, cool-down, toggles
        raw = (self.recipients_edit.text() or "").strip()
        parts = re.split(r"[,\s;]+", raw)
        emails = [e for e in (s.strip() for s in parts) if e and "@" in e]
        self.spec.recipients = emails
        p["recipients"] = emails
        p["interval_min"] = int(self.interval_spin.value())
        p["email_cooldown_min"] = int(self.cooldown_spin.value())
        p["email_on_escalation"] = (self.escalation_combo.currentIndex() == 1)
        p["email_on_recovery"]   = (self.recovery_combo.currentIndex() == 1)

        self.spec.payload = p
        super().accept()


class StaleViewDialog(QDialog):
    """
    Stale-data inspector (moved out of AlertsTab):
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

        # ---- Top controls -------------------------------------------------
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")

        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳ Refresh")
        self.refresh_btn.clicked.connect(self._rebuild)

        self.th_label = QLabel(" ")   # thresholds readout
        self.mode_label = QLabel(" ") # mode readout

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

        # ---- Matplotlib canvas + toolbar ---------------------------------
        self.fig = Figure(figsize=(8.4, 4.2), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas, 1)

        # ---- Data table ---------------------------------------------------
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["End time (local)", "Gap (minutes)", "Status", "Note"])
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)

        self._rebuild()

    # ---------- helpers ----------
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
        if red < amb: red = amb
        return amb, red, bool(p.get("scope_all", False))

    def _classify(self, gap_min: float, amber: int, red: int) -> str:
        if gap_min >= red: return "RED"
        if gap_min >= amber: return "AMBER"
        return "GREEN"

    def _series_local(self, s: pd.Series) -> pd.Series:
        return parse_series_to_local_naive(s)

    def _build_gaps(self) -> pd.DataFrame:
        df = getattr(self.host, "df", None)
        tcol = getattr(self.host, "datetime_col", None)
        if df is None or df.empty or not tcol or tcol not in df.columns:
            return pd.DataFrame(columns=["t_end", "gap_min", "status", "note"])

        ts = self._series_local(df[tcol]).dropna().sort_values()
        if ts.empty:
            return pd.DataFrame(columns=["t_end", "gap_min", "status", "note"])

        td = self._window_td()
        if td is not None:
            t_end = ts.max()
            t_start = t_end - td
            ts = ts[(ts >= t_start) & (ts <= t_end)]
            # allow “now gap” even with a single sample when not scope_all

        amber, red, scope_all = self._thresholds()

        # historical gaps (consecutive)
        gaps_s = ts.diff().dropna().dt.total_seconds()
        out = pd.DataFrame({
            "t_end": ts.iloc[1:],
            "gap_min": gaps_s.values / 60.0,
        })
        out["status"] = out["gap_min"].map(lambda v: self._classify(float(v), amber, red))
        out["note"] = ""

        # now→last gap (only when not scope_all)
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

    # ---------- actions ----------
    def _on_edit(self):
        # Prefer the host app's editor path so audits/baselines are handled consistently.
        try:
            cfg = getattr(self._parent, "configure_spec", None)
            if callable(cfg):
                cfg(self.spec)
            else:
                # Fallback: open the handler editor directly
                dlg = REGISTRY[self.spec.kind].create_editor(self.spec, self.host, self)  # type: ignore[name-defined]
                if dlg.exec():
                    pass
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

    # ---------- main builder ----------
    def _rebuild(self):
        self.ax.clear()

        amber, red, scope_all = self._thresholds()
        self.th_label.setText(f"AMBER ≥ {amber} min   •   RED ≥ {red} min")
        self.mode_label.setText("Mode: max gap" if scope_all else "Mode: since last")

        d = self._build_gaps()
        if d.empty:
            self.ax.text(0.5, 0.5, "No plottable data", ha="center", va="center")
            self.canvas.draw_idle()
            self.table.setRowCount(0)
            return

        # Line of gaps over time
        self.ax.plot(d["t_end"], d["gap_min"], linewidth=2)
        self.ax.axhline(amber, color="#f59f00", linestyle="--", linewidth=1, label=f"AMBER {amber}m")
        self.ax.axhline(red, color="#f03e3e", linestyle="--", linewidth=1, label=f"RED {red}m")

        locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
        self.ax.xaxis.set_major_locator(locator)
        try:
            self.ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        except Exception:
            pass

        self.ax.set_title(self.spec.name or "Stale preview")
        self.ax.set_xlabel("End time (local)")
        self.ax.set_ylabel("Gap (minutes)")
        self.ax.grid(True, linestyle="-", color="#e5e7eb", alpha=1.0)
        self.ax.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

        # Table
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
                "scope_all": False,       # False → since last; True → max historical gap
                "interval_min": 15,
                "email_cooldown_min": 240,
                "email_on_escalation": True,
                "email_on_recovery": False,
                "recipients": [],
                "threshold_min": 30,      # legacy
                "also_tables": [],        # NEW: other email tables to co-monitor
            },
        )

    # ---------- Helpers ----------
    @staticmethod
    def _candidate_time_cols(prefer: Optional[str], all_cols: List[str]) -> List[str]:
        """Choose likely datetime columns; prefer the current table's column name if present."""
        common = ["datetime", "timestamp", "ts", "created_at", "received_at", "date", "sent", "received"]
        ordered = []
        if prefer and prefer in all_cols:
            ordered.append(prefer)
        for c in common:
            if c in all_cols and c != prefer:
                ordered.append(c)
        # keep any remaining string-like columns as last resort
        for c in all_cols:
            if c not in ordered:
                ordered.append(c)
        return ordered

    def _load_times_for_table(
        self, db_path: Optional[str], table: str, prefer_col: Optional[str]
    ) -> pd.Series:
        """
        Load a pandas Series of local-naive datetimes for a named table.
        Tries to be robust even if we don't know the exact datetime column name.
        """
        if not db_path or not os.path.isfile(db_path):
            return pd.Series(dtype="datetime64[ns]")

        try:
            conn = sqlite3.connect(db_path)
            # discover columns
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
                    # parse to local-naive consistently with primary table
                    return parse_series_to_local_naive(s).dropna().sort_values()
                except Exception:
                    # try next candidate
                    conn.rollback()
                    continue
            conn.close()
        except Exception:
            pass

        return pd.Series(dtype="datetime64[ns]")

    @staticmethod
    def _combine_observed(observed_list: List[Tuple[str, float]]) -> float:
        """
        Combine observed seconds across multiple tables by taking the minimum (best freshness).
        If list is empty, return 0 to force GREEN? Better: return +inf → caller handles 'no valid'.
        """
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
        """New: provide the Stale 'preview' dialog from the handler (used by AlertsTab.view_selected)."""
        return StaleViewDialog(spec, host, parent)

    # ---------- Core evaluation ----------
    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult:
        df = getattr(host, "df", None)
        tcol = getattr(host, "datetime_col", None)

        # Primary table times
        primary_ts: pd.Series
        if df is None or df.empty or not tcol or tcol not in (df.columns if df is not None else []):
            primary_ts = pd.Series(dtype="datetime64[ns]")
        else:
            primary_ts = parse_series_to_local_naive(df[tcol]).dropna().sort_values()

        p = spec.payload or {}
        amber_min = int(p.get("amber_min", p.get("threshold_min", 30)))
        red_min   = int(p.get("red_min", max(amber_min * 2, 60)))
        if red_min < amber_min: red_min = amber_min
        amber_s = float(amber_min * 60)
        red_s   = float(red_min   * 60)

        scope_all = bool(p.get("scope_all", False))

        # Gather observed seconds for the primary + any additional tables
        observed_parts: List[Tuple[str, float]] = []
        primary_obs = self._observed_for_series(primary_ts, scope_all)
        if primary_obs != float("inf"):
            observed_parts.append((getattr(host, "table_name", "this_table"), primary_obs))

        also_tables: List[str] = list(p.get("also_tables", [])) or []

        # Try to resolve DB path for cross-table reads
        db_path = getattr(host, "db_path", None) or os.environ.get("BUOY_DB") or None

        for tname in also_tables:
            ts_other = self._load_times_for_table(db_path, tname, prefer_col=tcol)
            obs = self._observed_for_series(ts_other, scope_all)
            if obs != float("inf"):
                observed_parts.append((tname, obs))

        # If nothing usable, turn OFF
        if not observed_parts:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no valid times in any selected table"}

        # Combine by taking the best (lowest) staleness
        combined_observed = self._combine_observed(observed_parts)

        # Status from combined_observed
        if combined_observed >= red_s:
            status = Status.RED
        elif combined_observed >= amber_s:
            status = Status.AMBER
        else:
            status = Status.GREEN

        recipients = spec.recipients or (p.get("recipients") or [])
        interval_min = int(p.get("interval_min", 15))
        cooldown = int(p.get("email_cooldown_min", 240))

        # Build a compact per-table breakdown for the summary
        parts = []
        for name, secs in observed_parts:
            parts.append(f"{name}:{fmt_duration(secs)}")
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
            "observed": combined_observed,  # seconds
            "summary": summary,
            "extra": {
                "amber_min": amber_min,
                "red_min": red_min,
                "scope_all": scope_all,
                "sources": [n for n, _ in observed_parts],
            },
        }
