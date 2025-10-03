# alerts.py
import os
import json
import math
import sqlite3
import datetime
from typing import List, Optional

import pandas as pd

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGroupBox, QFormLayout, QSizePolicy,
    QPushButton, QCheckBox, QLineEdit, QSpinBox, QMessageBox, QTableWidget,
    QTableWidgetItem, QHBoxLayout, QScrollArea, QComboBox, QToolButton, QStyle
)
from PyQt6.QtCore import QTimer

# Try to use local Outlook (MAPI) via pywin32
try:
    import win32com.client as win32
except Exception:  # pragma: no cover
    win32 = None

# --- BAD VALUE FILTERS FOR LAT/LON ---
SENTINELS = {0, 0.0, 9999, 9999.0, -9999, -9999.0}

def _clean_lat_series(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINELS))
    # keep only plausible degrees
    return s.where((s >= -90) & (s <= 90))

def _clean_lon_series(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINELS))
    return s.where((s >= -180) & (s <= 180))


# -------------------- Config: hard-coded Outlook account --------------------
OUTLOOK_ACCOUNT_DISPLAY_NAME = "Metocean Configuration"


# -------------------- DB bootstrap --------------------
def ensure_alerts_table(db_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,
            table_name TEXT NOT NULL,
            condition TEXT NOT NULL,
            threshold REAL,
            observed REAL,
            last_lat REAL,
            last_lon REAL,
            last_time TEXT,
            recipients TEXT,
            map_path TEXT,
            notes TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def ensure_alerts_settings_log_table(db_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_settings_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            changed_utc TEXT NOT NULL,
            table_name TEXT NOT NULL,
            action TEXT NOT NULL,   -- created/updated/deleted/imported
            payload TEXT            -- JSON snapshot
        )
    """)
    conn.commit()
    conn.close()


# -------------------- utility --------------------
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fmt_duration(secs: float) -> str:
    secs = int(max(0, secs))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


# -------------------- alert item widget --------------------
class AlertItem(QWidget):
    TYPE_DISTANCE = "Max distance from deployment > threshold"
    TYPE_STALE    = "Time since last data > threshold"

    SCOPE_RECENT = "Most recent only"
    SCOPE_ALL    = "All data"

    def __init__(self, parent_tab: "AlertsTab", defaults: Optional[dict] = None):
        super().__init__()
        self.parent_tab = parent_tab

        # Keep a handle so we can recolor this card
        self.box = QGroupBox("Alert")
        form = QFormLayout(self.box)

        # Enable + selectors
        self.enable_check = QCheckBox("Enabled")
        self.enable_check.setChecked(False)  # default OFF; user must opt in
        self.type_combo   = QComboBox(); self.type_combo.addItems([self.TYPE_DISTANCE, self.TYPE_STALE])
        self.scope_combo  = QComboBox(); self.scope_combo.addItems([self.SCOPE_RECENT, self.SCOPE_ALL])

        # ---- Distance settings group ----
        self.deploy_lat = QLineEdit(); self.deploy_lat.setPlaceholderText("e.g., 53.3498")
        self.deploy_lon = QLineEdit(); self.deploy_lon.setPlaceholderText("e.g., -6.2603")
        self.threshold_m_spin = QSpinBox(); self.threshold_m_spin.setRange(1, 1_000_000)

        self.dist_group = QGroupBox("Distance settings")
        dist_form = QFormLayout(self.dist_group)
        dist_form.addRow("Deployment Lat:", self.deploy_lat)
        dist_form.addRow("Deployment Lon:", self.deploy_lon)
        dist_form.addRow("Threshold (m):", self.threshold_m_spin)

        # Show computed Lat/Lon (first 24 h mean)
        self.computed_deploy = QLabel("—")
        dist_form.addRow("Computed (first 24 h):", self.computed_deploy)

        # ---- Stale-data settings group ----
        self.avg_interval_label = QLabel("—")
        self.threshold_min_spin = QSpinBox(); self.threshold_min_spin.setRange(1, 10_000_000)

        self.stale_group = QGroupBox("Data freshness settings")
        stale_form = QFormLayout(self.stale_group)
        stale_form.addRow("Avg interval (computed):", self.avg_interval_label)
        stale_form.addRow("Threshold (minutes):", self.threshold_min_spin)

        # Recipients + buttons
        self.recipients_edit = QLineEdit(); self.recipients_edit.setPlaceholderText("email1@example.com, email2@example.com")
        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("Send test email")
        self.remove_btn = QToolButton(); self.remove_btn.setText("Remove")
        self.remove_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        btn_row.addWidget(self.test_btn); btn_row.addStretch(1); btn_row.addWidget(self.remove_btn)

        # Assemble form
        form.addRow(self.enable_check)
        form.addRow("Type:", self.type_combo)
        form.addRow("Scope:", self.scope_combo)
        form.addRow(self.dist_group)
        form.addRow(self.stale_group)
        form.addRow("Recipients:", self.recipients_edit)
        form.addRow(btn_row)

        root = QVBoxLayout(self)
        root.addWidget(self.box)

        # Signals AFTER groups exist
        self.remove_btn.clicked.connect(self.on_remove)
        self.test_btn.clicked.connect(self.on_send_test)
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)

        # color/visual state follows the Enabled checkbox
        self.enable_check.toggled.connect(self.update_enabled_style)

        # Apply defaults then toggle visibility + style
        self.apply_defaults(defaults)
        self.on_type_changed()
        self.update_enabled_style()

        # Change hooks for auditing
        self.enable_check.toggled.connect(lambda *_: self._notify_changed())
        self.type_combo.currentIndexChanged.connect(lambda *_: self._notify_changed())
        self.scope_combo.currentIndexChanged.connect(lambda *_: self._notify_changed())
        self.deploy_lat.textChanged.connect(lambda *_: self._notify_changed())
        self.deploy_lon.textChanged.connect(lambda *_: self._notify_changed())
        self.threshold_m_spin.valueChanged.connect(lambda *_: self._notify_changed())
        self.threshold_min_spin.valueChanged.connect(lambda *_: self._notify_changed())
        self.recipients_edit.textChanged.connect(lambda *_: self._notify_changed())

    def on_type_changed(self):
        t = self.type_combo.currentText()
        self.dist_group.setVisible(t == self.TYPE_DISTANCE)
        self.stale_group.setVisible(t == self.TYPE_STALE)

    def update_enabled_style(self):
        """Tint the card so it's obvious whether this alert is active."""
        if self.is_enabled():
            self.box.setStyleSheet("""
                QGroupBox {
                    background-color: #e9f9ef;
                    border: 2px solid #37b24d;
                    border-radius: 8px;
                    margin-top: 8px;
                }
                QGroupBox::title {
                    color: #2b8a3e;
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 3px 0 3px;
                }
            """)
        else:
            self.box.setStyleSheet("""
                QGroupBox {
                    background-color: #f8f9fa;
                    border: 2px solid #adb5bd;
                    border-radius: 8px;
                    margin-top: 8px;
                }
                QGroupBox::title {
                    color: #495057;
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 3px 0 3px;
                }
            """)

    def apply_defaults(self, defaults: Optional[dict]):
        """
        defaults keys:
          deploy_lat, deploy_lon, dist_threshold_m,
          avg_interval_s (for stale), stale_threshold_min (recommended)
        """
        # distance defaults
        if defaults and defaults.get("deploy_lat") is not None:
            self.deploy_lat.setText(f"{defaults['deploy_lat']:.6f}")
        if defaults and defaults.get("deploy_lon") is not None:
            self.deploy_lon.setText(f"{defaults['deploy_lon']:.6f}")

        if defaults and defaults.get("dist_threshold_m") is not None:
            self.threshold_m_spin.setValue(int(round(defaults["dist_threshold_m"])))
        else:
            self.threshold_m_spin.setValue(500)

        # stale defaults
        if defaults and defaults.get("avg_interval_s") is not None:
            self.avg_interval_label.setText(fmt_duration(defaults["avg_interval_s"]))
        if defaults and defaults.get("stale_threshold_min") is not None:
            self.threshold_min_spin.setValue(int(round(defaults["stale_threshold_min"])))
        else:
            self.threshold_min_spin.setValue(30)

        # show the computed (first 24 h) deployment location explicitly
        if defaults and defaults.get("deploy_lat") is not None and defaults.get("deploy_lon") is not None:
            self.computed_deploy.setText(f"{defaults['deploy_lat']:.6f}, {defaults['deploy_lon']:.6f}")
        else:
            self.computed_deploy.setText("not available")

    def is_enabled(self) -> bool:
        return self.enable_check.isChecked()

    def type_label(self) -> str:
        return self.type_combo.currentText()

    def scope_label(self) -> str:
        return self.scope_combo.currentText()

    def recipients(self) -> List[str]:
        text = self.recipients_edit.text().strip()
        if not text:
            return []
        return [p.strip() for p in text.split(',') if p.strip()]

    def config_distance(self) -> Optional[dict]:
        try:
            dep_lat = float(self.deploy_lat.text().strip())
            dep_lon = float(self.deploy_lon.text().strip())
        except Exception:
            return None
        return {
            "dep_lat": dep_lat,
            "dep_lon": dep_lon,
            "threshold_m": float(self.threshold_m_spin.value()),
            "scope": self.scope_label(),
        }

    def config_stale(self) -> dict:
        return {
            "threshold_s": float(self.threshold_min_spin.value() * 60),
            "scope": self.scope_label(),
        }

    def to_dict(self) -> dict:
        """Serialize this alert card to a JSON-safe dict."""
        t = self.type_label()
        out = {
            "enabled": self.is_enabled(),
            "type": t,
            "scope": self.scope_label(),
            "recipients": self.recipients(),
        }
        if t == self.TYPE_DISTANCE:
            cfg = self.config_distance()
            if cfg:
                out.update({
                    "deploy_lat": cfg["dep_lat"],
                    "deploy_lon": cfg["dep_lon"],
                    "threshold_m": cfg["threshold_m"],
                })
        elif t == self.TYPE_STALE:
            cfg = self.config_stale()
            out.update({
                "threshold_min": int(round(cfg["threshold_s"] / 60.0)),
            })
        return out

    def load_from_dict(self, data: dict):
        """Apply settings dict into the ui controls."""
        if not isinstance(data, dict):
            return
        # type + scope first
        t = data.get("type", self.TYPE_DISTANCE)
        s = data.get("scope", self.SCOPE_RECENT)
        idx_t = self.type_combo.findText(t)
        if idx_t >= 0: self.type_combo.setCurrentIndex(idx_t)
        idx_s = self.scope_combo.findText(s)
        if idx_s >= 0: self.scope_combo.setCurrentIndex(idx_s)

        # recipients
        recips = data.get("recipients") or []
        if isinstance(recips, list):
            self.recipients_edit.setText(", ".join(recips))
        elif isinstance(recips, str):
            self.recipients_edit.setText(recips)

        # distance fields
        if t == self.TYPE_DISTANCE:
            if data.get("deploy_lat") is not None:
                self.deploy_lat.setText(str(data["deploy_lat"]))
            if data.get("deploy_lon") is not None:
                self.deploy_lon.setText(str(data["deploy_lon"]))
            if data.get("threshold_m") is not None:
                self.threshold_m_spin.setValue(int(round(float(data["threshold_m"]))))

        # stale fields
        if t == self.TYPE_STALE and data.get("threshold_min") is not None:
            self.threshold_min_spin.setValue(int(data["threshold_min"]))

        # enabled last (also updates tint)
        self.enable_check.setChecked(bool(data.get("enabled", False)))
        self.update_enabled_style()

    def _notify_changed(self):
        """Tell the tab a setting changed (for audit + dirty flag)."""
        if hasattr(self.parent_tab, "_on_item_changed"):
            self.parent_tab._on_item_changed(self)

    def on_remove(self):
        self.parent_tab.remove_alert(self)

    def on_send_test(self):
        recips = self.recipients()
        if not recips:
            QMessageBox.warning(self, "Recipients", "Add at least one recipient email address.")
            return
        try:
            self.parent_tab.send_email_outlook(
                subject="Test alert email",
                body=f"This is a test email from the Alerts tab.\nTable: {self.parent_tab.host.table_name}",
                recipients=recips,
                attachment_path=None,
            )
            QMessageBox.information(self, "Email", "Test email sent.")
        except Exception as e:
            QMessageBox.critical(self, "Email error", str(e))


# -------------------- Alerts tab --------------------
class AlertsTab(QWidget):
    """
    Alerts manager with add/remove-able alert cards.

    Expected `host` interface (typically your TableTab instance):
      - host.df: pandas.DataFrame with Lat, Lon and a datetime column
      - host.datetime_col: str
      - host.table_name: str
      - host.latlong_widget.view: QWebEngineView (for snapshots)
      - host.update_map(): refresh the map
      - host.inner_tabs: QTabWidget containing this AlertsTab (for start/stop)
    """

    def _clean_latlon(self, df: pd.DataFrame):
        """Return cleaned (lat, lon) Series, or (None, None) if columns missing."""
        if "Lat" not in df.columns or "Lon" not in df.columns:
            return None, None
        lat = _clean_lat_series(df["Lat"])
        lon = _clean_lon_series(df["Lon"])
        return lat, lon

    def __init__(self, host, db_path: str):
        super().__init__()
        self.host = host
        self.db_path = db_path
        ensure_alerts_table(self.db_path)
        ensure_alerts_settings_log_table(self.db_path)

        self.alert_items: List[AlertItem] = []

        # --- ui scaffold ---
        page = QVBoxLayout(self)

        # Toolbar: add alert
        toolbar = QHBoxLayout()
        add_btn = QToolButton(); add_btn.setText("➕ New alert")
        add_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        add_btn.clicked.connect(self.add_alert)
        toolbar.addWidget(add_btn)
        toolbar.addStretch(1)
        page.addLayout(toolbar)

        info = QLabel(
            f"Emails will be sent via local Outlook account: <b>{OUTLOOK_ACCOUNT_DISPLAY_NAME}</b>.<br>"
            f"<i>Current table:</i> <b>{self.host.table_name}</b>"
        )
        page.addWidget(info)

        # Alert items list inside a scroll area
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch(1)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.cards_container)
        scroll.setMinimumHeight(260)
        page.addWidget(scroll)

        # Triggered alerts log table (filtered to this table)
        page.addWidget(QLabel("Triggered alerts (latest first)"))
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "UTC created", "Table", "Condition", "Threshold", "Observed",
            "Last Lat", "Last Lon", "Last time", "Recipients"
        ])
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page.addWidget(self.table)

        # Timer to evaluate alerts
        self.timer = QTimer(self)
        self.timer.setInterval(15_000)  # 15 seconds
        self.timer.timeout.connect(self.check_conditions)

        # Start/stop the timer based on which inner tab is active
        tabs = getattr(self.host, "inner_tabs", None)
        if tabs is not None:
            tabs.currentChanged.connect(self._on_parent_tab_changed)
            # sync now
            self._on_parent_tab_changed(tabs.currentIndex())
        else:
            # fallback: run but we'll still self-gate in check_conditions
            self.timer.start()

        # start with one default alert (distance, using first-24h defaults)
        self.add_alert()

        self.refresh_alerts_table()

    # ---------- tab visibility sync ----------
    def _on_parent_tab_changed(self, idx: int):
        tabs = getattr(self.host, "inner_tabs", None)
        if tabs is None:
            return
        current = tabs.widget(idx)
        if current is self:
            if not self.timer.isActive():
                self.timer.start()
        else:
            if self.timer.isActive():
                self.timer.stop()

    # ---------- time helpers (coerce safely) ----------
    def _time_series(self, df: pd.DataFrame) -> pd.Series:
        tcol = getattr(self.host, "datetime_col", None)
        if df is None or tcol not in df.columns:
            return pd.Series(index=df.index if df is not None else [], dtype="datetime64[ns]")
        return pd.to_datetime(df[tcol], errors="coerce")

    # ---------- default derivations ----------
    def _first_24h_window(self, df: pd.DataFrame) -> pd.DataFrame:
        ts = self._time_series(df)
        if ts.empty or ts.isna().all():
            return df.iloc[0:0]
        t0 = ts.min(skipna=True)
        cutoff = t0 + pd.Timedelta(hours=24)
        mask = (ts >= t0) & (ts <= cutoff)
        return df.loc[mask]

    def compute_defaults(self) -> dict:
        """
        Returns:
          {
            deploy_lat, deploy_lon, dist_threshold_m,
            avg_interval_s, stale_threshold_min
          }
        """
        out = {
            "deploy_lat": None,
            "deploy_lon": None,
            "dist_threshold_m": None,
            "avg_interval_s": None,
            "stale_threshold_min": None,
        }
        df = getattr(self.host, "df", None)
        if df is None or df.empty:
            return out

        # --- deployment location & default distance threshold from first 24h (CLEANED) ---
        lat, lon = self._clean_latlon(df)
        if lat is not None and lon is not None:
            # restrict to first 24h by index so we keep rows aligned
            win_idx = self._first_24h_window(df).index
            win_lat = lat.loc[win_idx].dropna()
            win_lon = lon.loc[win_idx].dropna()
            if not win_lat.empty and not win_lon.empty:
                mean_lat = float(win_lat.mean())
                mean_lon = float(win_lon.mean())
                out["deploy_lat"] = mean_lat
                out["deploy_lon"] = mean_lon
                try:
                    win_pairs = pd.concat([win_lat, win_lon], axis=1).dropna()
                    dists = win_pairs.apply(
                        lambda r: haversine_m(mean_lat, mean_lon, float(r.iloc[0]), float(r.iloc[1])), axis=1
                    )
                    # mean distance inside first 24h window, using only cleaned points
                    out["dist_threshold_m"] = float(dists.mean()) if len(dists) else None
                except Exception:
                    pass

        # --- average interval and default stale threshold = 3x mean interval ---
        try:
            ts = self._time_series(df).sort_values()
            intervals = ts.diff().dropna().dt.total_seconds()
            if len(intervals):
                avg_s = float(intervals.mean())
                out["avg_interval_s"] = avg_s
                out["stale_threshold_min"] = max(1, int(round((avg_s * 3) / 60)))
        except Exception:
            pass

        return out

    # ---------- high-level ops ----------
    def add_alert(self):
        # Insert before the stretch at the bottom
        if self.cards_layout.count() > 0:
            self.cards_layout.removeItem(self.cards_layout.itemAt(self.cards_layout.count() - 1))
        defaults = self.compute_defaults()
        item = AlertItem(self, defaults=defaults)
        self.alert_items.append(item)
        self.cards_layout.addWidget(item)
        self.cards_layout.addStretch(1)
        # audit
        self._log_settings_change("created", {"item": item.to_dict()})

    def remove_alert(self, item: AlertItem):
        # audit before removing so we can snapshot values
        self._log_settings_change("deleted", {"item": item.to_dict()})
        try:
            self.alert_items.remove(item)
        except ValueError:
            pass
        item.setParent(None)
        item.deleteLater()

    # ---------- email via Outlook ----------
    def _resolve_outlook_account(self, session) -> Optional[object]:
        try:
            for acc in session.Accounts:
                if str(acc.DisplayName).strip().lower() == OUTLOOK_ACCOUNT_DISPLAY_NAME.strip().lower():
                    return acc
        except Exception:
            pass
        return None

    def send_email_outlook(self, subject: str, body: str, recipients: List[str], attachment_path: Optional[str]):
        if win32 is None:
            raise RuntimeError("pywin32 is not installed. Install with: pip install pywin32")

        outlook = win32.Dispatch("Outlook.Application")
        session = outlook.GetNamespace("MAPI")

        mail = outlook.CreateItem(0)  # olMailItem
        mail.Subject = subject
        mail.Body = body
        mail.To = "; ".join(recipients)
        mail.SentOnBehalfOfName = OUTLOOK_ACCOUNT_DISPLAY_NAME

        acc = self._resolve_outlook_account(session)
        if acc is not None:
            try:
                mail.SendUsingAccount = acc
            except Exception:
                try:
                    mail._oleobj_.Invoke(*(64209, 0, 8, 0, acc))
                except Exception:
                    pass

        if attachment_path and os.path.exists(attachment_path):
            mail.Attachments.Add(attachment_path)

        mail.Send()

    # ---------- logging helpers ----------
    def log_alert(self, condition: str, threshold: float, observed: float,
                  last_lat: Optional[float], last_lon: Optional[float], last_time: Optional[pd.Timestamp],
                  recipients: List[str], map_path: Optional[str], notes: str = ""):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alerts_log (created_utc, table_name, condition, threshold, observed, last_lat, last_lon, last_time, recipients, map_path, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.datetime.utcnow().isoformat(timespec="seconds"),
                self.host.table_name,
                condition,
                threshold,
                observed,
                None if last_lat is None else float(last_lat),
                None if last_lon is None else float(last_lon),
                None if last_time is None else str(last_time),
                ", ".join(recipients),
                map_path,
                notes,
            ),
        )
        conn.commit()
        conn.close()

    def _log_settings_change(self, action: str, payload: dict):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO alerts_settings_log (changed_utc, table_name, action, payload) VALUES (?, ?, ?, ?)",
                (datetime.datetime.utcnow().isoformat(timespec="seconds"),
                 self.host.table_name,
                 action,
                 json.dumps(payload, ensure_ascii=False))
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _on_item_changed(self, item: "AlertItem"):
        # Audit and (optionally) mark project dirty via host/main window.
        self._log_settings_change("updated", {"item": item.to_dict()})

    def refresh_alerts_table(self):
        try:
            conn = sqlite3.connect(self.db_path)
            df = pd.read_sql_query(
                "SELECT created_utc, table_name, condition, threshold, observed, last_lat, last_lon, last_time, recipients "
                "FROM alerts_log WHERE table_name = ? ORDER BY id DESC",
                conn,
                params=(self.host.table_name,)
            )
            conn.close()
        except Exception as e:
            QMessageBox.critical(self, "DB Error", f"Failed loading alerts log: {e}")
            return

        self.table.setRowCount(0)
        for _, row in df.iterrows():
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, key in enumerate([
                "created_utc", "table_name", "condition", "threshold", "observed",
                "last_lat", "last_lon", "last_time", "recipients"
            ]):
                item = QTableWidgetItem(str(row.get(key, "")))
                self.table.setItem(r, c, item)

    # ---------- map snapshot ----------
    def grab_map_png(self) -> Optional[str]:
        try:
            self.host.update_map()
            view = self.host.latlong_widget.view
            pix = view.grab()
            if pix.isNull():
                return None
            os.makedirs("map_snaps", exist_ok=True)
            fname = datetime.datetime.utcnow().strftime("map_%Y%m%d_%H%M%S.png")
            fpath = os.path.join("map_snaps", fname)
            pix.save(fpath)
            return fpath
        except Exception:
            return None

    # ---------- evaluation loop ----------
    def check_conditions(self):
        # Gate: only evaluate for the active Alerts tab in THIS project's inner tab widget.
        tabs = getattr(self.host, "inner_tabs", None)
        if tabs is not None and tabs.currentWidget() is not self:
            return

        # If no cards are enabled, skip quickly
        if not any(item.is_enabled() for item in self.alert_items):
            return

        df = getattr(self.host, "df", None)
        if df is None or df.empty:
            return

        ts = self._time_series(df)

        lat, lon = self._clean_latlon(df)
        have_latlon = (lat is not None) and (lon is not None)

        if ts.empty or ts.dropna().empty:
            return

        have_latlon = {"Lat", "Lon"}.issubset(df.columns)

        ts_valid = ts.dropna()
        if ts_valid.empty:
            return

        last_time = ts_valid.max()

        # latest row that also has valid cleaned Lat/Lon
        if have_latlon:
            ll = pd.concat([lat, lon, ts], axis=1, keys=["Lat", "Lon", "T"]).dropna()
            if not ll.empty:
                last_idx = ll["T"].idxmax()
                last_lat = float(ll.loc[last_idx, "Lat"])
                last_lon = float(ll.loc[last_idx, "Lon"])
            else:
                last_lat = None
                last_lon = None
        else:
            last_lat = None
            last_lon = None

        # Precompute helpful stats for stale checks
        diffs_s = ts_valid.sort_values().diff().dropna().dt.total_seconds()
        avg_interval_s = float(diffs_s.mean()) if len(diffs_s) else 0.0
        now_utc = datetime.datetime.utcnow()

        for item in list(self.alert_items):
            if not item.is_enabled():
                continue

            tlabel = item.type_label()
            scope = item.scope_label()
            recips = item.recipients()

            if tlabel == AlertItem.TYPE_DISTANCE and have_latlon:
                cfg = item.config_distance()
                if not cfg:
                    continue  # missing lat/lon

                dep_lat, dep_lon = cfg["dep_lat"], cfg["dep_lon"]
                threshold_m = cfg["threshold_m"]

                try:
                    if scope == AlertItem.SCOPE_ALL:
                        pairs = pd.concat([lat, lon], axis=1).dropna()  # cleaned only
                        distances = pairs.apply(
                            lambda r: haversine_m(dep_lat, dep_lon, float(r.iloc[0]), float(r.iloc[1])), axis=1
                        )
                        observed_m = float(distances.max()) if len(distances) else 0.0
                    else:
                        observed_m = (
                            0.0 if (last_lat is None or last_lon is None)
                            else haversine_m(dep_lat, dep_lon, last_lat, last_lon)
                        )
                except Exception:
                    observed_m = 0.0

                if observed_m > threshold_m:
                    snap = self.grab_map_png()
                    body = (
                        f"Alert: max distance from deployment exceeded threshold\n\n"
                        f"Table: {self.host.table_name}\n"
                        f"Scope: {scope}\n"
                        f"Deployment: ({dep_lat:.6f}, {dep_lon:.6f})\n"
                        f"Threshold: {threshold_m:.1f} m\n"
                        f"Observed: {observed_m:.1f} m\n"
                        f"Last known: ({last_lat}, {last_lon}) at {last_time}\n"
                    )

                    self.log_alert(
                        condition="max_distance_from_deployment_exceeds",
                        threshold=threshold_m,
                        observed=observed_m,
                        last_lat=last_lat,
                        last_lon=last_lon,
                        last_time=last_time,
                        recipients=recips,
                        map_path=snap,
                    )
                    self.refresh_alerts_table()

                    if recips:
                        try:
                            self.send_email_outlook(
                                subject=f"ALERT: {self.host.table_name} distance {observed_m:.0f} m > {threshold_m:.0f} m ({scope})",
                                body=body,
                                recipients=recips,
                                attachment_path=snap,
                            )
                        except Exception as e:
                            QMessageBox.critical(self, "Email error", f"Failed to send alert email: {e}")

            elif tlabel == AlertItem.TYPE_STALE:
                cfg = item.config_stale()
                threshold_s = cfg["threshold_s"]

                if scope == AlertItem.SCOPE_ALL:
                    observed_s = float(diffs_s.max()) if len(diffs_s) else 0.0
                else:  # most recent only
                    observed_s = float((now_utc - last_time.to_pydatetime()).total_seconds())

                if observed_s > threshold_s:
                    body = (
                        f"Alert: data gap exceeded threshold\n\n"
                        f"Table: {self.host.table_name}\n"
                        f"Scope: {scope}\n"
                        f"Average interval: {fmt_duration(avg_interval_s)}\n"
                        f"Threshold: {fmt_duration(threshold_s)}\n"
                        f"Observed gap: {fmt_duration(observed_s)}\n"
                        f"Last data at: {last_time}\n"
                    )

                    self.log_alert(
                        condition="time_since_last_data_exceeds",
                        threshold=threshold_s,
                        observed=observed_s,
                        last_lat=last_lat,
                        last_lon=last_lon,
                        last_time=last_time,
                        recipients=recips,
                        map_path=None,
                    )
                    self.refresh_alerts_table()

                    if recips:
                        try:
                            self.send_email_outlook(
                                subject=f"ALERT: {self.host.table_name} data gap {fmt_duration(observed_s)} > {fmt_duration(threshold_s)} ({scope})",
                                body=body,
                                recipients=recips,
                                attachment_path=None,
                            )
                        except Exception as e:
                            QMessageBox.critical(self, "Email error", f"Failed to send alert email: {e}")

    # ---------- project round-trip ----------
    def export_settings(self) -> dict:
        """JSON-safe snapshot for this table."""
        return {
            "table": self.host.table_name,
            "version": 1,
            "timer_ms": int(self.timer.interval()),
            "items": [it.to_dict() for it in self.alert_items],
        }

    def import_settings(self, data: dict):
        """Rebuild from snapshot."""
        if not isinstance(data, dict):
            return
        if data.get("table") and data["table"] != self.host.table_name:
            return
        if "timer_ms" in data and isinstance(data["timer_ms"], int):
            self.timer.setInterval(max(1000, data["timer_ms"]))

        # clear existing alerts
        for it in list(self.alert_items):
            self.remove_alert(it)

        # rebuild
        for item_data in data.get("items", []):
            if self.cards_layout.count() > 0:
                self.cards_layout.removeItem(self.cards_layout.itemAt(self.cards_layout.count() - 1))  # drop stretch
            defaults = self.compute_defaults()
            it = AlertItem(self, defaults=defaults)
            it.load_from_dict(item_data)
            self.alert_items.append(it)
            self.cards_layout.addWidget(it)
            self.cards_layout.addStretch(1)

        # audit
        self._log_settings_change("imported", self.export_settings())
