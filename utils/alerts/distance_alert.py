# utils/alerts/distance_alert.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple, List
import math
import pandas as pd

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox, QPushButton,
    QVBoxLayout, QWidget, QLabel, QHBoxLayout, QToolButton, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt

from utils.alerts import REGISTRY, register, AlertSpec, AlertHandler, EvalResult, Status, Host

SENTINELS = {0, 0.0, 9999, 9999.0, -9999, -9999.0}


def _clean_lat(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINELS))
    return s.where((s >= -90) & (s <= 90))


def _clean_lon(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINELS))
    return s.where((s >= -180) & (s <= 180))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.atan2(math.sqrt(a), math.sqrt(1-a))


def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or not col or col not in df.columns:
        return pd.Series([], dtype="float64")
    return df[col]


def _mean_latlon(
    df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    tcol: Optional[str],
    window: str
) -> Optional[Tuple[float, float]]:
    """Mean (lat, lon) over first day/week/month (or whole dataset if no time col)."""
    if df is None or df.empty or lat_col not in df.columns or lon_col not in df.columns:
        return None

    lat = _clean_lat(df[lat_col])
    lon = _clean_lon(df[lon_col])

    if not tcol or tcol not in df.columns:
        pairs = pd.concat([lat, lon], axis=1).dropna()
        if pairs.empty:
            return None
        return float(pairs.iloc[:, 0].mean()), float(pairs.iloc[:, 1].mean())

    ts = pd.to_datetime(df[tcol], errors="coerce")
    if ts.dropna().empty:
        pairs = pd.concat([lat, lon], axis=1).dropna()
        if pairs.empty:
            return None
        return float(pairs.iloc[:, 0].mean()), float(pairs.iloc[:, 1].mean())

    tmin = ts.min(skipna=True)
    if pd.isna(tmin):
        return None

    if window == "day":
        cutoff = tmin + pd.Timedelta(days=1)
    elif window == "week":
        cutoff = tmin + pd.Timedelta(days=7)
    else:
        cutoff = tmin + pd.Timedelta(days=30)

    mask = (ts >= tmin) & (ts <= cutoff)
    lat_w = lat.where(mask)
    lon_w = lon.where(mask)
    pairs = pd.concat([lat_w, lon_w], axis=1).dropna()
    if pairs.empty:
        return None
    return float(pairs.iloc[:, 0].mean()), float(pairs.iloc[:, 1].mean())


def _split_recipients(text: str) -> List[str]:
    if not text:
        return []
    raw = [p.strip() for chunk in text.replace(";", ",").split(",") for p in chunk.split()]
    return [p for p in raw if p]


class _Editor(QDialog):
    """
    Single config window:
      - Lat/Lon columns
      - Scope: recent/all
      - Deployment: Manual or compute from first Day/Week/Month
      - Thresholds: AMBER (warning) & RED (critical)
      - Recipients (per alert) and Check interval (minutes)
      - Email throttle options
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self.setWindowTitle("Distance alert")
        self._parent = parent  # AlertsTab (for _uniquify_name)
        p = self.spec.payload or {}

        df: pd.DataFrame = getattr(host, "df", pd.DataFrame())
        cols = list(df.columns) if isinstance(df, pd.DataFrame) else []

        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        # --- Name ---
        self.name_edit = QLineEdit(spec.name or "Distance from deployment (AMBER/RED)")
        form.addRow("Alert name:", self.name_edit)

        # Column selectors
        self.lat_col = QComboBox(); self.lat_col.addItems(cols)
        if p.get("lat_col") in cols: self.lat_col.setCurrentText(p["lat_col"])

        self.lon_col = QComboBox(); self.lon_col.addItems(cols)
        if p.get("lon_col") in cols: self.lon_col.setCurrentText(p["lon_col"])

        form.addRow("Latitude column:", self.lat_col)
        form.addRow("Longitude column:", self.lon_col)

        # Scope
        self.scope = QComboBox(); self.scope.addItems(["Most recent only", "All data"])
        self.scope.setCurrentIndex(1 if p.get("scope") == "all" else 0)
        form.addRow("Scope:", self.scope)

        # Deployment mode
        self.mode = QComboBox()
        self.mode.addItems(["Manual", "Compute: first day", "Compute: first week", "Compute: first month"])
        mode = p.get("deploy_mode", "manual")
        idx = {"manual": 0, "first_day": 1, "first_week": 2, "first_month": 3}.get(mode, 0)
        self.mode.setCurrentIndex(idx)

        # Manual lat/lon
        self.manual_lat = QLineEdit("" if p.get("deploy_lat") in (None, "") else str(p.get("deploy_lat")))
        self.manual_lon = QLineEdit("" if p.get("deploy_lon") in (None, "") else str(p.get("deploy_lon")))
        row_manual = QWidget(); row_lay = QHBoxLayout(row_manual); row_lay.setContentsMargins(0,0,0,0)
        row_lay.addWidget(QLabel("Lat:")); row_lay.addWidget(self.manual_lat)
        row_lay.addSpacing(12)
        row_lay.addWidget(QLabel("Lon:")); row_lay.addWidget(self.manual_lon)

        self.preview = QLabel("—"); self.preview.setStyleSheet("color:#666;")

        form.addRow("Deployment mode:", self.mode)
        form.addRow("Manual coordinates:", row_manual)
        form.addRow("Computed preview:", self.preview)

        # Thresholds (amber + red)
        self.thr_amber = QSpinBox(); self.thr_amber.setRange(1, 1_000_000); self.thr_amber.setValue(int(p.get("amber_threshold_m", 300)))
        self.thr_red   = QSpinBox(); self.thr_red.setRange(1, 1_000_000);   self.thr_red.setValue(int(p.get("red_threshold_m",   500)))
        form.addRow("AMBER threshold (m):", self.thr_amber)
        form.addRow("RED threshold (m):", self.thr_red)

        # Recipients (per-alert) + interval
        recipients_text = ", ".join(spec.recipients or p.get("recipients", []))
        self.recipients_edit = QLineEdit(recipients_text)
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(1, 100000); self.interval_spin.setValue(int(p.get("interval_min", 15)))
        form.addRow("Email recipients:", self.recipients_edit)
        form.addRow("Check interval (min):", self.interval_spin)

        hint = QLabel("Emails are sent when entering AMBER/RED (GREEN→AMBER/RED). "
                      "AMBER↔RED flips are muted unless you enable 'Email on escalation'. "
                      "All emails obey the cool-down.")
        hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(hint)

        btn = QPushButton("OK"); btn.clicked.connect(self.accept); lay.addWidget(btn)

        # Email throttle (cool-down & options)
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 100000)
        self.cooldown_spin.setValue(int(p.get("email_cooldown_min", 240)))
        self.chk_escalation = QComboBox()
        self.chk_escalation.addItems(["No", "Yes"])
        self.chk_escalation.setCurrentIndex(1 if p.get("email_on_escalation", False) else 0)
        self.chk_recovery = QComboBox()
        self.chk_recovery.addItems(["No", "Yes"])
        self.chk_recovery.setCurrentIndex(1 if p.get("email_on_recovery", False) else 0)

        form.addRow("Email cool-down (min):", self.cooldown_spin)
        form.addRow("Email on escalation (AMBER→RED):", self.chk_escalation)
        form.addRow("Email on recovery (→GREEN):", self.chk_recovery)

        # Wiring
        self.mode.currentIndexChanged.connect(self._update_preview)
        self.lat_col.currentIndexChanged.connect(self._update_preview)
        self.lon_col.currentIndexChanged.connect(self._update_preview)
        self.mode.currentIndexChanged.connect(self._toggle_manual_enabled)
        self._toggle_manual_enabled()
        self._update_preview()

    def _toggle_manual_enabled(self):
        manual = (self.mode.currentIndex() == 0)
        self.manual_lat.setEnabled(manual)
        self.manual_lon.setEnabled(manual)

    def _update_preview(self):
        if self.mode.currentIndex() == 0:
            try:
                lat = float(self.manual_lat.text().strip())
                lon = float(self.manual_lon.text().strip())
                self.preview.setText(f"Manual: {lat:.6f}, {lon:.6f}")
            except Exception:
                self.preview.setText("Manual: —")
            return

        df: pd.DataFrame = getattr(self.host, "df", pd.DataFrame())
        lat_c = self.lat_col.currentText().strip()
        lon_c = self.lon_col.currentText().strip()
        tcol: Optional[str] = getattr(self.host, "datetime_col", None)
        window = {1: "day", 2: "week", 3: "month"}.get(self.mode.currentIndex(), "day")
        pair = _mean_latlon(df, lat_c, lon_c, tcol, window)
        self.preview.setText(
            "Not available (insufficient data)" if pair is None else f"{window}: {pair[0]:.6f}, {pair[1]:.6f}"
        )

    def accept(self):
        p = self.spec.payload
        # Name (uniquify at tab-level if possible)
        new_name = (self.name_edit.text() or "").strip()
        if new_name:
            if hasattr(self._parent, "_uniquify_name") and callable(getattr(self._parent, "_uniquify_name")):
                self.spec.name = self._parent._uniquify_name(new_name, exclude_id=self.spec.id)
            else:
                self.spec.name = new_name

        # Email throttle options
        p["email_cooldown_min"] = int(self.cooldown_spin.value())
        p["email_on_escalation"] = (self.chk_escalation.currentIndex() == 1)
        p["email_on_recovery"] = (self.chk_recovery.currentIndex() == 1)
        p["lat_col"] = self.lat_col.currentText().strip()
        p["lon_col"] = self.lon_col.currentText().strip()
        p["scope"] = "all" if self.scope.currentIndex() == 1 else "recent"

        mode_map = {0: "manual", 1: "first_day", 2: "first_week", 3: "first_month"}
        p["deploy_mode"] = mode_map.get(self.mode.currentIndex(), "manual")
        if p["deploy_mode"] == "manual":
            try:
                p["deploy_lat"] = float(self.manual_lat.text().strip())
                p["deploy_lon"] = float(self.manual_lon.text().strip())
            except Exception:
                p["deploy_lat"] = ""
                p["deploy_lon"] = ""
        else:
            p["deploy_lat"] = ""
            p["deploy_lon"] = ""

        p["amber_threshold_m"] = float(self.thr_amber.value())
        p["red_threshold_m"]   = float(self.thr_red.value())

        # per-alert recipients & interval
        recips = _split_recipients(self.recipients_edit.text())
        self.spec.recipients = recips
        p["recipients"] = recips
        p["interval_min"] = int(self.interval_spin.value())

        # Arm logic — start QUIET and remember current enabled state
        p["initialized"] = False
        p["last_status_str"] = ""            # unknown at creation
        p["last_emailed_status_str"] = ""    # for extra safety
        p["was_enabled"] = bool(self.spec.enabled)

        super().accept()


class _DistanceViewerDialog(QDialog):
    """
    Distance alert inspector (moved out of AlertsTab):
      • Top bar: Range, Refresh, % time in G/A/R, Edit thresholds…, Export report…
      • Table: Time (local), Lat, Lon, Distance (m), Status
      • Map: LocalGISViewer with radius rings (Amber/Red), centroid/last point, etc.
    """
    def __init__(self, spec: AlertSpec, host: Host, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.host = host
        self.setWindowTitle(spec.name or "Distance preview")
        self.setMinimumSize(980, 640)

        from utils.local_gis_viewer import LocalGISViewer  # lazy import

        top = QVBoxLayout(self)
        # ---- Controls --------------------------------------------------------
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItems(["6 h", "12 h", "24 h", "3 d", "7 d", "30 d", "All"])
        self.range_combo.setCurrentText("24 h")

        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳ Refresh")
        self.refresh_btn.clicked.connect(self._rebuild)

        self.pct_label = QLabel(" ")  # Green / Amber / Red %
        self.pct_label.setStyleSheet("font-weight: 600;")

        self.th_label = QLabel(" ")  # thresholds readout

        self.edit_btn = QToolButton()
        self.edit_btn.setText("⚙ Edit thresholds…")
        self.edit_btn.clicked.connect(self._on_edit)

        self.export_btn = QToolButton()
        self.export_btn.setText("⬇ Export report…")
        self.export_btn.clicked.connect(self._export_report)

        ctrl.addWidget(self.range_combo)
        ctrl.addWidget(self.refresh_btn)
        ctrl.addStretch(1)
        ctrl.addWidget(self.pct_label)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.th_label)
        ctrl.addStretch(1)
        ctrl.addWidget(self.edit_btn)
        ctrl.addWidget(self.export_btn)
        top.addLayout(ctrl)

        # ---- Table -----------------------------------------------------------
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Time (local)", "Lat", "Lon", "Distance (m)", "Status"])
        self.table.verticalHeader().setVisible(False)
        top.addWidget(self.table, 1)

        # ---- Map -------------------------------------------------------------
        a0, r0 = self._get_radii_m()
        self.viewer = LocalGISViewer(
            pd.DataFrame(columns=["Lat", "Lon"]),
            table_name=(self.spec.name or "Distance View"),
            radius1=int(round(a0)),
            radius2=int(round(r0)),
            parent=self
        )
        top.addWidget(self.viewer, 0, Qt.AlignmentFlag.AlignHCenter)

        # Initial build
        self._rebuild()

    # ---------- helpers ----------
    def _pick_time_col(self, df: pd.DataFrame) -> Optional[str]:
        for c in ["__dt_iso", "timestamp", "time", "datetime", "date", "DateTime", "received_time"]:
            if c in df.columns:
                return c
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                return c
        return None

    def _pick_latlon_cols(self, df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
        # prefer canonical names
        lat_candidates = ["Lat", "lat", "Latitude", "latitude"]
        lon_candidates = ["Lon", "lon", "Longitude", "longitude", "lng"]
        lat = next((c for c in lat_candidates if c in df.columns), None)
        lon = next((c for c in lon_candidates if c in df.columns), None)
        # allow payload override if provided
        lat = str(self.spec.payload.get("lat_col", lat or "")) or lat
        lon = str(self.spec.payload.get("lon_col", lon or "")) or lon
        return (lat if lat in df.columns else None,
                lon if lon in df.columns else None)

    def _to_local_naive(self, s: pd.Series) -> pd.Series:
        # Use shared helper from time_settings for consistency
        from utils.time_settings import parse_series_to_local_naive
        return parse_series_to_local_naive(s)

    def _window_td(self) -> Optional[pd.Timedelta]:
        m = {
            "6 h": pd.Timedelta(hours=6),
            "12 h": pd.Timedelta(hours=12),
            "24 h": pd.Timedelta(hours=24),
            "3 d": pd.Timedelta(days=3),
            "7 d": pd.Timedelta(days=7),
            "30 d": pd.Timedelta(days=30),
        }
        return m.get(self.range_combo.currentText(), None)  # None = All

    def _get_radii_m(self) -> Tuple[float, float]:
        """
        Return (amber_m, red_m) using ONLY the Distance alert's user-set thresholds.
        Falls back to 300 / 500 if missing.
        """
        p = self.spec.payload or {}
        a = p.get("amber_threshold_m", p.get("amber_m", p.get("amber", p.get("r1", 300.0))))
        r = p.get("red_threshold_m",   p.get("red_m",   p.get("red",   p.get("r2", 500.0))))
        try:
            a = float(a)
        except Exception:
            a = 300.0
        try:
            r = float(r)
        except Exception:
            r = 500.0
        if r < a:
            a, r = r, a
        return a, r

    def _classify(self, dist_m: float, r1: float, r2: float) -> str:
        # farther is worse
        if dist_m >= r2: return "RED"
        if dist_m >= r1: return "AMBER"
        return "GREEN"

    def _dist_m(self, lat1, lon1, lat2, lon2) -> float:
        try:
            from pyproj import Geod
            g = Geod(ellps="WGS84")
            _, _, d = g.inv(float(lon1), float(lat1), float(lon2), float(lat2))
            return float(d)
        except Exception:
            return haversine_m(float(lat1), float(lon1), float(lat2), float(lon2))

    def _deployment_point(self, df: pd.DataFrame, latcol: str, loncol: str, tcol: Optional[str]) -> Optional[Tuple[float, float]]:
        """Mirror handler semantics: manual or mean of first day/week/month over the WHOLE dataset."""
        p = self.spec.payload or {}
        dep_mode = str(p.get("deploy_mode", "manual"))
        if dep_mode == "manual":
            try:
                return float(p.get("deploy_lat")), float(p.get("deploy_lon"))
            except Exception:
                return None
        window = {"first_day": "day", "first_week": "week", "first_month": "month"}.get(dep_mode, "day")
        return _mean_latlon(df, latcol, loncol, tcol, window)

    def _compute_time_share(self, d: pd.DataFrame, tcol: str, r1: float, r2: float) -> tuple[dict, pd.DataFrame]:
        if d.empty:
            return {"GREEN": 0.0, "AMBER": 0.0, "RED": 0.0}, d
        d2 = d[[tcol, "Lat", "Lon", "dist_m"]].copy()
        d2["Status"] = d2["dist_m"].map(lambda v: self._classify(float(v), r1, r2))
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

    # ---------- data slicing ----------
    def _current_windowed(self):
        df = getattr(self.host, "df", None)
        if df is None or df.empty:
            return pd.DataFrame(), "", 0.0, 0.0, {"GREEN": 0, "AMBER": 0, "RED": 0}

        tcol = self._pick_time_col(df)
        latcol, loncol = self._pick_latlon_cols(df)
        if not tcol or not latcol or not loncol:
            return pd.DataFrame(), "", 0.0, 0.0, {"GREEN": 0, "AMBER": 0, "RED": 0}

        d = df[[tcol, latcol, loncol]].copy()
        d[tcol] = self._to_local_naive(d[tcol])
        d["Lat"] = pd.to_numeric(d[latcol], errors="coerce")
        d["Lon"] = pd.to_numeric(d[loncol], errors="coerce")
        d = d.dropna(subset=[tcol, "Lat", "Lon"]).sort_values(tcol)
        if d.empty:
            return d, tcol, 0.0, 0.0, {"GREEN": 0, "AMBER": 0, "RED": 0}

        # deployment = manual or computed from WHOLE df (like handler.evaluate)
        dep = self._deployment_point(df, latcol, loncol, tcol)
        if dep is None:
            return pd.DataFrame(), tcol, 0.0, 0.0, {"GREEN": 0, "AMBER": 0, "RED": 0}
        dep_lat, dep_lon = dep

        # time window (apply to viewed samples)
        td = self._window_td()
        if td is not None:
            t_end = d[tcol].max()
            t_start = t_end - td
            d = d[(d[tcol] >= t_start) & (d[tcol] <= t_end)]
        if d.empty:
            return d, tcol, 0.0, 0.0, {"GREEN": 0, "AMBER": 0, "RED": 0}

        # distances to deployment
        d["dist_m"] = [
            self._dist_m(dep_lat, dep_lon, float(lat), float(lon))
            for lat, lon in zip(d["Lat"].to_numpy(), d["Lon"].to_numpy())
        ]

        r1, r2 = self._get_radii_m()
        share, _ = self._compute_time_share(d, tcol, r1, r2)
        return d, tcol, r1, r2, share

    # ---------- actions ----------
    def _on_edit(self):
        try:
            # Let the AlertsTab open the editor (so audits/baselines match)
            parent = self.parent()
            if hasattr(parent, "configure_spec") and callable(getattr(parent, "configure_spec")):
                parent.configure_spec(self.spec)  # type: ignore[misc]
            else:
                dlg = REGISTRY[self.spec.kind].create_editor(self.spec, self.host, self)  # fallback
                if dlg.exec():
                    pass
        except Exception:
            pass
        self._rebuild()

    def _export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export distance report", "", "CSV files (*.csv)")
        if not path:
            return
        d, tcol, r1, r2, share = self._current_windowed()
        if d.empty:
            QMessageBox.information(self, "Export", "No data to export.")
            return
        try:
            _, d_status = self._compute_time_share(d, tcol, r1, r2)
            out = d_status.rename(
                columns={tcol: "time_local", "Lat": "lat", "Lon": "lon",
                         "dist_m": "distance_m", "Status": "status", "dur_s": "duration_seconds"}
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Distance alert report\n")
                f.write(f"# Name: {self.spec.name or self.spec.kind}\n")
                f.write(f"# Range: {self.range_combo.currentText()}\n")
                f.write(f"# Amber radius: {r1:.1f} m\n")
                f.write(f"# Red radius:   {r2:.1f} m\n")
                f.write(f"# Percent in GREEN: {share['GREEN']:.1f}%\n")
                f.write(f"# Percent in AMBER: {share['AMBER']:.1f}%\n")
                f.write(f"# Percent in RED:   {share['RED']:.1f}%\n")
                f.write("# --- data ---\n")
            out.to_csv(path, index=False, mode="a")
            QMessageBox.information(self, "Export", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    def _rebuild(self):
        # data
        d, tcol, r1, r2, share = self._current_windowed()

        # % readout + thresholds
        self.pct_label.setText(
            f"Green {share['GREEN']:.1f}%   •   Amber {share['AMBER']:.1f}%   •   Red {share['RED']:.1f}%")
        self.th_label.setText(f"Amber {r1:.1f} m   •   Red {r2:.1f} m")

        # table
        self.table.setRowCount(0)
        if not d.empty:
            for _, row in d.iterrows():
                status = self._classify(float(row["dist_m"]), r1, r2)
                r_i = self.table.rowCount()
                self.table.insertRow(r_i)
                self.table.setItem(r_i, 0, QTableWidgetItem(row[tcol].strftime("%Y-%m-%d %H:%M:%S")))
                self.table.setItem(r_i, 1, QTableWidgetItem(f"{float(row['Lat']):.6f}"))
                self.table.setItem(r_i, 2, QTableWidgetItem(f"{float(row['Lon']):.6f}"))
                self.table.setItem(r_i, 3, QTableWidgetItem(f"{float(row['dist_m']):.1f}"))
                self.table.setItem(r_i, 4, QTableWidgetItem(status))

        # map
        pts = d[["Lat", "Lon"]].copy() if not d.empty else pd.DataFrame(columns=["Lat", "Lon"])
        try:
            self.viewer.set_radii(int(round(r1)), int(round(r2)))
        except Exception:
            pass
        self.viewer.set_data(pts)
        self.viewer.fit_to_view()


@register
class DistanceHandler(AlertHandler):
    kind = "Distance"

    def default_spec(self, host: Host) -> AlertSpec:
        cols = list(getattr(host, "df", pd.DataFrame()).columns) if getattr(host, "df", None) is not None else []
        lat_guess = next((c for c in cols if c.lower() in ("lat", "latitude")), (cols[0] if cols else ""))
        lon_guess = next((c for c in cols if c.lower() in ("lon", "lng", "longitude")), (cols[1] if len(cols) > 1 else ""))

        return AlertSpec(
            id="",
            kind=self.kind,
            name="Distance from deployment (AMBER/RED)",
            enabled=False,
            recipients=[],
            payload={
                "lat_col": lat_guess,
                "lon_col": lon_guess,
                "scope": "recent",
                "deploy_mode": "manual",
                "deploy_lat": "",
                "deploy_lon": "",
                "amber_threshold_m": 300.0,
                "red_threshold_m": 500.0,
                "interval_min": 15,

                # Email throttle options
                "email_cooldown_min": 240,       # 4h default
                "email_on_escalation": False,    # AMBER->RED emails?
                "email_on_recovery": False,      # GREEN recovery emails?

                # runtime notification state
                "initialized": False,
                "last_status_str": "",
                "last_emailed_status_str": "",
                "was_enabled": False,
                "recipients": [],
            },
        )

    def create_editor(self, spec: AlertSpec, host: Host, parent=None):
        return _Editor(spec, host, parent)

    def create_viewer(self, spec: AlertSpec, host: Host, parent=None):
        # AlertsTab.view_selected already prefers handler.create_viewer if present
        return _DistanceViewerDialog(spec, host, parent)

    def _classify(self, observed: float, amb: float, red: float) -> Status:
        if red is not None and observed > red:
            return Status.RED
        if amb is not None and observed > amb:
            return Status.AMBER
        return Status.GREEN

    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult:
        df: pd.DataFrame = getattr(host, "df", None)
        if df is None or df.empty:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no data"}

        p = spec.payload or {}
        lat_col: str = p.get("lat_col", "")
        lon_col: str = p.get("lon_col", "")
        if lat_col not in df.columns or lon_col not in df.columns:
            return {"status": Status.OFF, "observed": 0.0, "summary": "pick lat/lon columns"}

        lat = _clean_lat(_safe_series(df, lat_col))
        lon = _clean_lon(_safe_series(df, lon_col))
        latlon = pd.concat([lat, lon], axis=1, keys=["Lat", "Lon"]).dropna()
        if latlon.empty:
            return {"status": Status.OFF, "observed": 0.0, "summary": "no valid Lat/Lon"}

        # Deployment point
        dep_mode = str(p.get("deploy_mode", "manual"))
        if dep_mode == "manual":
            try:
                dep = (float(p.get("deploy_lat")), float(p.get("deploy_lon")))
            except Exception:
                dep = None
        else:
            tcol: Optional[str] = getattr(host, "datetime_col", None)
            window = {"first_day": "day", "first_week": "week", "first_month": "month"}.get(dep_mode, "day")
            dep = _mean_latlon(df, lat_col, lon_col, tcol, window)

        if dep is None:
            return {"status": Status.OFF, "observed": 0.0, "summary": "deployment location unavailable"}

        dep_lat, dep_lon = dep

        scope = str(p.get("scope", "recent"))
        last_lat_val: Optional[float] = None
        last_lon_val: Optional[float] = None

        try:
            if scope == "all":
                # distance for every point; pick the max and remember that point
                dists = latlon.apply(lambda r: haversine_m(dep_lat, dep_lon, float(r["Lat"]), float(r["Lon"])), axis=1)
                if dists.empty:
                    observed = 0.0
                else:
                    observed = float(dists.max())
                    idx = dists.idxmax()
                    last_point = latlon.loc[idx]
                    last_lat_val = float(last_point["Lat"])
                    last_lon_val = float(last_point["Lon"])
            else:
                last_row = latlon.iloc[-1]
                observed = float(haversine_m(dep_lat, dep_lon, float(last_row["Lat"]), float(last_row["Lon"])))
                last_lat_val = float(last_row["Lat"])
                last_lon_val = float(last_row["Lon"])
        except Exception:
            observed = 0.0

        amb = float(p.get("amber_threshold_m", 300.0))
        red = float(p.get("red_threshold_m", 500.0))
        status = self._classify(observed, amb, red)

        # choose an "effective" threshold to record in history
        if status == Status.RED:
            eff_thr = red
        elif status == Status.AMBER:
            eff_thr = amb
        else:
            eff_thr = amb  # next boundary to watch

        # ------- Notification policy flags from payload -------
        last_status_str = str(p.get("last_status_str", ""))
        initialized = bool(p.get("initialized", False))
        was_enabled = bool(p.get("was_enabled", False))
        now_enabled = bool(spec.enabled)

        # On enable/disable flip: re-baseline quietly, no email
        if was_enabled != now_enabled:
            p["initialized"] = False
            p["last_status_str"] = status.name
            p["was_enabled"] = now_enabled

            recipients = spec.recipients if spec.recipients else (p.get("recipients") or [])
            interval_min = int(p.get("interval_min", 15))

            summary = (
                f"{observed:.0f} m • amb {amb:.0f} • red {red:.0f} • "
                f"scope: {'all' if scope == 'all' else 'recent'} • "
                f"recipients {len(recipients)} • every {interval_min} min"
            )
            cool = int(p.get("email_cooldown_min", 240))
            if cool > 0:
                summary += f" • cooldown {cool} min"

            return {
                "status": status,
                "observed": observed,
                "summary": summary,
                "should_email": False,
                "recipients": recipients,
                "interval_min": interval_min,
                "extra": {
                    "last_lat": last_lat_val,
                    "last_lon": last_lon_val,
                    "threshold": eff_thr,
                    "amber_threshold_m": amb,
                    "red_threshold_m": red,
                    "deploy_lat": dep_lat,
                    "deploy_lon": dep_lon,
                    "scope": scope,
                }
            }

        # Regular transitions: only consider GREEN->AMBER/RED here; AMBER<->RED & recovery are gated in AlertsTab
        should_email = False
        if initialized:
            if status in (Status.AMBER, Status.RED) and status.name != last_status_str:
                should_email = True
        else:
            should_email = False

        p["initialized"] = True
        p["last_status_str"] = status.name
        if should_email:
            p["last_emailed_status_str"] = status.name
        p["was_enabled"] = now_enabled

        recipients = spec.recipients if spec.recipients else (p.get("recipients") or [])
        interval_min = int(p.get("interval_min", 15))
        mode_label = {"manual": "manual", "first_day": "first day", "first_week": "first week",
                      "first_month": "first month"}.get(dep_mode, dep_mode)
        scope_label = "all data" if scope == "all" else "most recent"

        summary = (
            f"{observed:.0f} m • amb {amb:.0f} • red {red:.0f} • "
            f"dep {dep_lat:.5f},{dep_lon:.5f} ({mode_label}) • "
            f"scope: {scope_label} • recipients {len(recipients)} • every {interval_min} min"
        )
        cool = int(p.get("email_cooldown_min", 240))
        if cool > 0:
            summary += f" • cooldown {cool} min"

        if status == Status.GREEN:
            should_email = False

        return {
            "status": status,
            "observed": observed,
            "summary": summary,
            "should_email": should_email,
            "recipients": recipients,
            "interval_min": interval_min,
            "extra": {
                "last_lat": last_lat_val,
                "last_lon": last_lon_val,
                "threshold": eff_thr,
                "amber_threshold_m": amb,
                "red_threshold_m": red,
                "deploy_lat": dep_lat,
                "deploy_lon": dep_lon,
                "scope": scope,
            }
        }
