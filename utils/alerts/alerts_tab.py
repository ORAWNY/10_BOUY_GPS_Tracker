# utils/alerts/alerts_tab.py
from __future__ import annotations
import json, sqlite3, uuid, copy
from typing import List, Callable, Optional, Tuple
from datetime import datetime, timezone

import pandas as pd

import datetime as _dt  # keep for parsing only
from functools import partial

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton, QStyle, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QMessageBox, QInputDialog, QCheckBox,
    QFileDialog, QApplication, QDialog, QComboBox
)
from PyQt6.QtGui import QColor, QBrush

from utils.alerts import REGISTRY, AlertSpec, Status
from utils.alerts.store import (
    ensure_alerts_tables, write_settings_audit, read_last_status, write_last_status,
    read_flag, set_flag, count_flagged, read_last_email, write_last_email,
    get_state_db_path_for, prune_alerts_log,
    write_current_settings, read_current_settings, purge_orphaned_alert_state,
    append_alert_csv, rotate_daily_alert_csvs,
    read_last_status_meta, clear_all_flags
)

from utils.alerts.emailer import send_email_outlook
from utils.time_settings import local_zone, parse_series_to_local_naive
from utils.alerts.view_helpers import enrich_extra_for_log


# ------------------------- robust logging wrapper -------------------------

def _make_log_fn(host, logger: Optional[Callable[[str], None]] = None) -> Callable[[str], None]:
    """
    Return a logging function that never raises:
      • If 'logger' looks like logging.Logger → use .info
      • If 'logger' is file-like → use .write
      • If 'logger' is callable → call it
      • Else fall back to host.log / host.append_log
      • Finally, print()
    """
    def _host_try(msg: str) -> bool:
        try:
            if hasattr(host, "log") and callable(getattr(host, "log")):
                host.log(msg)
                return True
            if hasattr(host, "append_log") and callable(getattr(host, "append_log")):
                host.append_log(msg)
                return True
        except Exception:
            pass
        return False

    # logging.Logger-like
    if hasattr(logger, "info"):
        def _log(msg: str):
            try:
                logger.info(msg)  # type: ignore[attr-defined]
            except Exception:
                if not _host_try(msg):
                    try:
                        print(msg)
                    except Exception:
                        pass
        return _log

    # file-like
    if hasattr(logger, "write"):
        def _log(msg: str):
            try:
                logger.write(msg + "\n")  # type: ignore[attr-defined]
            except Exception:
                if not _host_try(msg):
                    try:
                        print(msg)
                    except Exception:
                        pass
        return _log

    # callable
    if callable(logger):
        def _log(msg: str):
            try:
                logger(msg)  # type: ignore[misc]
            except Exception:
                if not _host_try(msg):
                    try:
                        print(msg)
                    except Exception:
                        pass
        return _log

    # no logger provided → host or print
    def _log(msg: str):
        if not _host_try(msg):
            try:
                print(msg)
            except Exception:
                pass
    return _log


# -------------------------------------------------------------------------


def _status_text(status: Status, enabled: bool) -> str:
    return "OFF" if not enabled else status.value

def _status_color(status: Status, enabled: bool) -> str:
    if not enabled or status == Status.OFF: return "#adb5bd"
    if status == Status.GREEN: return "#37b24d"
    if status == Status.AMBER: return "#f59f00"
    if status == Status.RED: return "#f03e3e"
    return "#adb5bd"


class AlertsTab(QWidget):
    """
    Alerts list + embedded Alerts history table (same tab).
    Toolbar now includes: New, Remove, Configure, Enable, Disable, Duplicate,
    Export/Import/Copy/Paste, Generate Report (.csv), Refresh history.
    """
    COL_STATUS = 0
    COL_NAME   = 1
    COL_RECIPS = 2
    COL_SUMMARY= 3
    COL_FLAG   = 4

    def _mk_btn(self, icon: QStyle.StandardPixmap, tooltip: str, slot) -> QToolButton:
        btn = QToolButton(self)
        btn.setIcon(self.style().standardIcon(icon))
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)  # ← ensure icon-only
        btn.clicked.connect(slot)
        return btn

    def __init__(self, host, db_path: str, logger: Optional[Callable[[str], None]] = None):
        super().__init__()
        self.host = host
        self.db_path = db_path
        self._log = _make_log_fn(host, logger)  # safe, never raises

        # --- ensure state DB + tables exist early ---
        ensure_alerts_tables(self.db_path)

        # --- model state ---
        self.specs: List[AlertSpec] = []
        self._next_due: dict[str, datetime] = {}        # per-alert next evaluation time (UTC)
        self._skip_logged_until: dict[str, datetime] = {}  # per-alert "email:skipped" mute-until

        # ================= UI =================
        root = QVBoxLayout(self)

        # ---------- Toolbar ----------
        bar = QHBoxLayout()

        # --- Toolbar (icon-only with hover tooltips) --

        btn_new = self._mk_btn(QStyle.StandardPixmap.SP_FileDialogNewFolder, "New alert", self.add_alert_dialog);
        bar.addWidget(btn_new)
        btn_remove = self._mk_btn(QStyle.StandardPixmap.SP_TrashIcon, "Remove selected", self.delete_selected);
        bar.addWidget(btn_remove)
        btn_cfg = self._mk_btn(QStyle.StandardPixmap.SP_FileDialogDetailedView, "Configure…", self.configure_selected);
        bar.addWidget(btn_cfg)
        btn_view = self._mk_btn(QStyle.StandardPixmap.SP_DialogYesButton, "View selected", self.view_selected);
        bar.addWidget(btn_view)
        btn_enable = self._mk_btn(QStyle.StandardPixmap.SP_DialogApplyButton, "Enable selected", self.enable_selected);
        bar.addWidget(btn_enable)
        btn_disable = self._mk_btn(QStyle.StandardPixmap.SP_DialogCancelButton, "Disable selected",
                                   self.disable_selected);
        bar.addWidget(btn_disable)
        btn_dup = self._mk_btn(QStyle.StandardPixmap.SP_DialogOpenButton, "Duplicate selected",
                               self.duplicate_selected);
        bar.addWidget(btn_dup)
        btn_rename = self._mk_btn(QStyle.StandardPixmap.SP_FileDialogToParent, "Rename…", self.rename_selected);
        bar.addWidget(btn_rename)
        export_btn = self._mk_btn(QStyle.StandardPixmap.SP_ArrowDown, "Export…", self.export_alerts_dialog);
        bar.addWidget(export_btn)
        import_btn = self._mk_btn(QStyle.StandardPixmap.SP_ArrowUp, "Import…", self.import_alerts_dialog);
        bar.addWidget(import_btn)
        copy_btn = self._mk_btn(QStyle.StandardPixmap.SP_DialogSaveButton, "Copy selected to clipboard",
                                self.copy_selected_to_clipboard);
        bar.addWidget(copy_btn)
        paste_btn = self._mk_btn(QStyle.StandardPixmap.SP_DialogOpenButton, "Paste from clipboard",
                                 self.paste_from_clipboard);
        bar.addWidget(paste_btn)
        btn_report = self._mk_btn(QStyle.StandardPixmap.SP_FileIcon, "Generate report (.csv)",
                                  self.generate_report_dialog);
        bar.addWidget(btn_report)
        btn_hist_refresh = self._mk_btn(QStyle.StandardPixmap.SP_BrowserReload, "Refresh history",
                                        self._load_alerts_log);
        bar.addWidget(btn_hist_refresh)

        bar.addStretch(1)

        # Badge stays as-is
        self.badge = QLabel("⚑ 0", self)
        self.badge.setStyleSheet(
            "padding:4px 8px; border-radius:10px; background:#f03e3e; color:white; font-weight:bold;")
        bar.addWidget(self.badge)

        # Clear-all-flags button
        btn_clear_flags = self._mk_btn(QStyle.StandardPixmap.SP_DialogResetButton, "Clear all flags",
                                       self._clear_all_flags_clicked)
        bar.addWidget(btn_clear_flags)

        root.addLayout(bar)

        info = QLabel(
            f"Emails sent via Outlook account: <b>Metocean Configuration</b> &nbsp;&nbsp; | &nbsp;&nbsp; "
            f"<i>Table:</i> <b>{self.host.table_name}</b>",
            self
        )
        root.addWidget(info)

        # ---------- Alerts table ----------
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Status", "Alert name", "Recipients", "Summary / Thresholds", "Flag"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        root.addWidget(self.table)

        # ---------- Alerts history table (DB-backed) ----------
        self.history = QTableWidget(0, 8, self)
        self.history.setHorizontalHeaderLabels(
            ["Local time", "Condition", "Threshold", "Observed", "Last Lat", "Last Lon", "Recipients", "Notes"]
        )
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.verticalHeader().setVisible(False)
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        root.addWidget(self.history)

        # ---------- Timer ----------
        self.timer = QTimer(self)
        self._timer_min = 15
        self.timer.setInterval(max(60_000, int(self._timer_min) * 60_000))
        self.timer.timeout.connect(self.evaluate_all)
        self.timer.start()

        # ============ Load current settings or seed defaults ============
        saved = None
        try:
            saved = read_current_settings(self.db_path, self.host.table_name)
        except Exception:
            saved = None

        if saved:
            try:
                self.import_settings(saved)  # also updates timer interval
            except Exception:
                self.add_default_seeds()
        else:
            self.add_default_seeds()

        # initial paint & history
        self.refresh_table()
        self._load_alerts_log()

        # eval guard + banner
        self._eval_running = False
        self.quiet_refresh_logs = True
        self._log_quiet(f"AlertsTab ready ({self._timer_min} min interval)")

    def _clear_alerts_history(self):
        btn = QMessageBox.question(
            self, "Clear alerts history",
            "Clear ALL history entries for this table?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        try:
            state_db_path = get_state_db_path_for(self.db_path)
            conn = sqlite3.connect(state_db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM alerts_log WHERE table_name=?", (self.host.table_name,))
            conn.commit()
            conn.close()
            self._load_alerts_log()
            QMessageBox.information(self, "Alerts history", "History cleared.")
            self._write_alerts_log(condition="history:cleared", notes="User cleared history")
        except Exception as e:
            QMessageBox.critical(self, "Clear history error", str(e))

    def _debug_dump_flags(self):
        try:
            state_db_path = get_state_db_path_for(self.db_path)
            conn = sqlite3.connect(state_db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT alert_id, flagged, updated_utc FROM alerts_flags "
                "WHERE TRIM(table_name)=TRIM(?) COLLATE NOCASE ORDER BY updated_utc DESC",
                (self.host.table_name,)
            )
            rows = cur.fetchall()
            conn.close()
            self._log(f"[alerts][debug] flags for {self.host.table_name}: {rows}")
        except Exception as e:
            self._log(f"[alerts][debug] dump failed: {e}")

    def view_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "View alert", "Select one or more alerts first.")
            return

        for s in specs:
            try:
                handler = REGISTRY[s.kind]
                if hasattr(handler, "create_viewer") and callable(getattr(handler, "create_viewer")):
                    dlg = handler.create_viewer(s, self.host, self)  # handler-defined viewer
                    if isinstance(dlg, QDialog):
                        dlg.exec()
                        continue
            except Exception:
                pass

            QMessageBox.information(self, "View alert", f"No viewer available for type: {s.kind}")

    def _utc_str_to_local(self, s: str) -> str:
        try:
            dt = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
            return dt.astimezone(local_zone()).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return s

    # ---------- alerts_log helpers ----------
    def _write_alerts_log(
            self,
            condition: str,
            threshold: Optional[float] = None,
            observed: Optional[float] = None,
            last_lat: Optional[float] = None,
            last_lon: Optional[float] = None,
            last_time_utc: Optional[str] = None,
            recipients: Optional[str] = None,
            notes: Optional[str] = None,
            spec: Optional[AlertSpec] = None,
    ):
        try:
            from datetime import datetime
            state_db_path = get_state_db_path_for(self.db_path)
            conn = sqlite3.connect(state_db_path)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO alerts_log
                (created_utc, table_name, alert_id, name, kind,
                 condition, threshold, observed, last_lat, last_lon, last_time,
                 recipients, map_path, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    self.host.table_name,
                    (spec.id if spec else None),
                    (spec.name if spec else None),
                    (spec.kind if spec else None),
                    condition,
                    float(threshold) if threshold is not None else None,
                    float(observed) if observed is not None else None,
                    float(last_lat) if last_lat is not None else None,
                    float(last_lon) if last_lon is not None else None,
                    last_time_utc,
                    recipients,
                    None,
                    notes,
                ),
            )
            conn.commit()
            conn.close()

            # keep history lean in DB
            try:
                prune_alerts_log(self.db_path, self.host.table_name, keep_days=30, max_rows_per_table=50000)
            except Exception:
                pass

            # mirror to CSV history in alerts log/
            try:
                row = {
                    "created_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "table_name": self.host.table_name,
                    "alert_id": (spec.id if spec else None),
                    "name": (spec.name if spec else None),
                    "kind": (spec.kind if spec else None),
                    "condition": condition,
                    "threshold": threshold,
                    "observed": observed,
                    "last_lat": last_lat,
                    "last_lon": last_lon,
                    "last_time": last_time_utc,
                    "recipients": recipients,
                    "notes": notes,
                }
                append_alert_csv(self.db_path, row)
                rotate_daily_alert_csvs(self.db_path)
            except Exception:
                pass

        except Exception as e:
            self._log(f"[alerts] alerts_log write failed: {e}")

    def _load_alerts_log(self, limit: int = 200):
        """
        Load recent history rows and render them nicely.
        For Stale alerts, show observed in human time (h/m/s) and threshold in minutes.
        """

        def _fmt_duration(secs: float) -> str:
            try:
                secs = int(max(0, float(secs)))
            except Exception:
                return str(secs)
            d, r = divmod(secs, 86400)
            h, r = divmod(r, 3600)
            m, s = divmod(r, 60)
            if d: return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
            if h: return f"{h}h {m:02d}m {s:02d}s"
            return f"{m}m {s:02d}s"

        rows: List[Tuple] = []
        try:
            state_db_path = get_state_db_path_for(self.db_path)
            conn = sqlite3.connect(state_db_path)
            cur = conn.cursor()
            # include 'kind' so we can format columns per alert type
            cur.execute(
                """
                SELECT created_utc, condition, threshold, observed, last_lat, last_lon, recipients, notes, kind
                FROM alerts_log
                WHERE table_name=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.host.table_name, int(limit)),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            self._log(f"[alerts] alerts_log load failed: {e}")
            rows = []

        self.history.setRowCount(0)
        for r in rows:
            # unpack including 'kind' (last column)
            created_utc, condition, threshold, observed, last_lat, last_lon, recipients, notes, kind = r

            # time: stored as naive UTC -> show local
            created_local = self._utc_str_to_local(str(created_utc)) if created_utc is not None else ""

            # pretty format when this row comes from the Stale handler
            if (kind or "").strip() == "Stale":
                # threshold is minutes; observed is seconds
                th_disp = "" if threshold is None else f"{int(float(threshold))} m"
                obs_disp = "" if observed is None else _fmt_duration(float(observed))
            else:
                th_disp = "" if threshold is None else str(threshold)
                obs_disp = "" if observed is None else str(observed)

            rr = self.history.rowCount()
            self.history.insertRow(rr)
            self.history.setItem(rr, 0, QTableWidgetItem(created_local))
            self.history.setItem(rr, 1, QTableWidgetItem("" if condition is None else str(condition)))
            self.history.setItem(rr, 2, QTableWidgetItem(th_disp))
            self.history.setItem(rr, 3, QTableWidgetItem(obs_disp))
            self.history.setItem(rr, 4, QTableWidgetItem("" if last_lat is None else str(last_lat)))
            self.history.setItem(rr, 5, QTableWidgetItem("" if last_lon is None else str(last_lon)))
            self.history.setItem(rr, 6, QTableWidgetItem("" if recipients is None else str(recipients)))
            self.history.setItem(rr, 7, QTableWidgetItem("" if notes is None else str(notes)))

    def _key_for(self, spec: AlertSpec) -> str:
        """Use a stable UUID as the storage key; create if missing."""
        if not spec.id:
            spec.id = str(uuid.uuid4())
        return spec.id

    def _uniquify_name(self, desired: str, *, exclude_id: Optional[str] = None) -> str:
        """Ensure alert names are unique in UI (does not affect IDs)."""
        base = (desired or "Alert").strip()
        existing = {s.name for s in self.specs if s.name and s.id != exclude_id}
        name = base or "Alert"
        k = 2
        while name in existing:
            name = f"{base} ({k})"
            k += 1
        return name

    # -------- Export / Import / Copy / Paste helpers --------

    def rename_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Rename alerts", "Select one or more alerts first.")
            return
        for s in specs:
            current = s.name or s.kind
            new, ok = QInputDialog.getText(self, "Rename alert", "Name:", text=current)
            if ok:
                s.name = self._uniquify_name(new.strip(), exclude_id=self._key_for(s))
                write_settings_audit(self.db_path, self.host.table_name, "renamed",
                                     json.dumps(s.to_dict(), ensure_ascii=False))
        self._persist_current_settings()
        self.refresh_table()

    def _selected_specs(self) -> List[AlertSpec]:
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        return [self.specs[r] for r in sorted(rows) if 0 <= r < len(self.specs)]

    @staticmethod
    def _specs_to_payload(specs: List[AlertSpec]) -> dict:
        return {"version": 1, "items": [s.to_dict() for s in specs]}

    @staticmethod
    def _payload_to_specs(payload: dict) -> List[AlertSpec]:
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [AlertSpec.from_dict(d) for d in items if isinstance(d, dict)]

    def _ensure_unique_ids(self, incoming: List[AlertSpec]) -> List[AlertSpec]:
        existing_ids = {s.id for s in self.specs}
        existing_names = {s.name for s in self.specs if s.name}
        result: List[AlertSpec] = []
        for s in incoming:
            # new unique id if conflict/missing
            if not s.id or s.id in existing_ids:
                s.id = str(uuid.uuid4())
            existing_ids.add(s.id)
            # nudge name if collides
            base = (s.name or s.kind or "Alert").strip()
            name = base
            k = 1
            while name in existing_names:
                k += 1
                name = f"{base} ({k})"
            s.name = name
            existing_names.add(name)
            result.append(s)
        return result

    # ---- Toolbar actions ----
    def export_alerts_dialog(self):
        # If selection exists, confirm exporting only selected
        sel = self._selected_specs()
        specs = sel if sel else self.specs
        if not specs:
            QMessageBox.information(self, "Export alerts", "There are no alerts to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export alerts to JSON", "", "JSON files (*.json)")
        if not path:
            return
        try:
            payload = self._specs_to_payload(specs)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self._write_alerts_log(condition="export", notes=f"Exported {len(specs)} alert(s) to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    def import_alerts_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import alerts from JSON", "", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            incoming = self._payload_to_specs(payload)
            if not incoming:
                QMessageBox.warning(self, "Import alerts", "No alerts found in the selected file.")
                return

            # Ask: Replace or Merge?
            btn = QMessageBox.question(
                self, "Import alerts",
                f"Found {len(incoming)} alert(s).\n\nReplace current alerts or merge?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            # Yes = Replace, No = Merge
            if btn == QMessageBox.StandardButton.Yes:
                self.specs = []
            # De-duplicate ids/names and append
            incoming = self._ensure_unique_ids(incoming)
            self.specs.extend(incoming)
            self._persist_current_settings()

            # Quietly baseline each imported alert (prevents instant emails)
            for s in incoming:
                try:
                    res = REGISTRY[s.kind].evaluate(s, self.host)
                    cur_status = res.get("status", Status.OFF)
                    cur_obs = float(res.get("observed", 0.0))
                    write_last_status(self.db_path, self.host.table_name, self._key_for(s), cur_status.value, cur_obs)
                except Exception:
                    pass
                write_settings_audit(self.db_path, self.host.table_name, "created", json.dumps(s.to_dict(), ensure_ascii=False))

            self.refresh_table()
            self._write_alerts_log(condition="import", notes=f"Imported {len(incoming)} alert(s) from {path}")
        except Exception as e:
            QMessageBox.critical(self, "Import error", str(e))

    def copy_selected_to_clipboard(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Copy alerts", "Select one or more alerts first.")
            return
        payload = self._specs_to_payload(specs)
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False, indent=2))
        self._write_alerts_log(condition="copy", notes=f"Copied {len(specs)} alert(s) to clipboard")

    def paste_from_clipboard(self):
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "Paste alerts", "Clipboard is empty.")
            return
        try:
            payload = json.loads(text)
            incoming = self._payload_to_specs(payload)
            if not incoming:
                QMessageBox.warning(self, "Paste alerts", "Clipboard does not contain alert items.")
                return
            # Duplicate semantics: always merge and ensure new IDs/names
            incoming = self._ensure_unique_ids(incoming)
            self.specs.extend(incoming)
            self._persist_current_settings()

            # Quiet baseline
            for s in incoming:
                try:
                    res = REGISTRY[s.kind].evaluate(s, self.host)
                    cur_status = res.get("status", Status.OFF)
                    cur_obs = float(res.get("observed", 0.0))
                    write_last_status(self.db_path, self.host.table_name, self._key_for(s), cur_status.value, cur_obs)
                except Exception:
                    pass
                write_settings_audit(self.db_path, self.host.table_name, "created", json.dumps(s.to_dict(), ensure_ascii=False))

            self.refresh_table()
            self._write_alerts_log(condition="paste", notes=f"Pasted {len(incoming)} alert(s) from clipboard")
        except Exception as e:
            QMessageBox.critical(self, "Paste error", str(e))

    # -------- Add new alert --------
    def add_alert_dialog(self):
        kinds = list(REGISTRY.keys())
        kind, ok = QInputDialog.getItem(self, "New alert", "Type:", kinds, 0, False)
        if not ok or not kind:
            return
        handler = REGISTRY[kind]
        spec = handler.default_spec(self.host)

        # Always ensure a stable UUID id
        if not spec.id:
            spec.id = str(uuid.uuid4())

        # Ask user for a name (default to current; make unique)
        proposed = spec.name or spec.kind or "Alert"
        name, ok = QInputDialog.getText(self, "New alert", "Name:", text=proposed)
        if ok and name.strip():
            spec.name = self._uniquify_name(name.strip())
        else:
            spec.name = self._uniquify_name(proposed)

        self.specs.append(spec)
        write_settings_audit(self.db_path, self.host.table_name, "created",
                             json.dumps(spec.to_dict(), ensure_ascii=False))
        self._write_alerts_log(condition=f"created:{spec.kind}", notes=f"Created alert '{spec.name or spec.kind}'", spec=spec)
        self.configure_spec(spec)
        self._persist_current_settings()
        self.refresh_table()

    def add_default_seeds(self):
        for kind in ("Distance", "Stale", "Threshold", "MissingData"):
            if kind in REGISTRY:
                spec = REGISTRY[kind].default_spec(self.host)
                spec.id = spec.id or str(uuid.uuid4())
                spec.enabled = True  # <— ensure visible/evaluable by default
                self.specs.append(spec)
                self._persist_current_settings()

    def _persist_current_settings(self):
        try:
            import json as _json
            payload = self.export_settings()
            write_current_settings(self.db_path, self.host.table_name, _json.dumps(payload, ensure_ascii=False))
            purge_orphaned_alert_state(self.db_path, self.host.table_name, [s.id for s in self.specs])
        except Exception:
            pass

    # -------- Table rendering --------
    def refresh_table(self):
        self.table.setRowCount(0)
        for spec in self.specs:
            r = self.table.rowCount()
            self.table.insertRow(r)

            # Status cell carries id in UserRole for convenience
            item_status = QTableWidgetItem(_status_text(Status.OFF, spec.enabled))
            item_status.setData(Qt.ItemDataRole.UserRole, self._key_for(spec))
            self.table.setItem(r, self.COL_STATUS, item_status)

            # Name
            self.table.setItem(r, self.COL_NAME, QTableWidgetItem(spec.name or spec.kind))

            # Recipients
            self.table.setItem(r, self.COL_RECIPS, QTableWidgetItem(", ".join(spec.recipients)))

            # Summary (live)
            try:
                res = REGISTRY[spec.kind].evaluate(spec, self.host)
                summary = res.get("summary", "")
            except Exception:
                summary = ""
            self.table.setItem(r, self.COL_SUMMARY, QTableWidgetItem(summary))

            # Flag checkbox
            flag_cb = QCheckBox()
            key = self._key_for(spec)  # <— was spec.id or spec.name...
            is_flagged, _, _ = read_flag(self.db_path, self.host.table_name, key)
            flag_cb.setChecked(bool(is_flagged))
            flag_cb.stateChanged.connect(partial(self._on_flag_toggle, spec))
            self.table.setCellWidget(r, self.COL_FLAG, flag_cb)

        self._recolor_status_cells()
        self._refresh_badge()
        self._debug_dump_flags()
        self._log_quiet(f"[alerts] Table refreshed: {len(self.specs)} alert(s)")

    def configure_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Configure alerts", "Select one or more alerts first.")
            return
        for s in specs:
            self.configure_spec(s)
        self.refresh_table()

    def enable_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Enable alerts", "Select one or more alerts first.")
            return
        for s in specs:
            if not s.enabled:
                self.toggle_enable(s)
        self.refresh_table()

    def disable_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Disable alerts", "Select one or more alerts first.")
            return
        for s in specs:
            if s.enabled:
                self.toggle_enable(s)
        self.refresh_table()

    def duplicate_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Duplicate alerts", "Select one or more alerts first.")
            return
        for s in list(specs):
            self.duplicate_spec(s)
        self.refresh_table()

    def delete_selected(self):
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(self, "Remove alerts", "Select one or more alerts first.")
            return
        btn = QMessageBox.question(
            self, "Remove alerts", f"Remove {len(specs)} selected alert(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        for s in specs:
            self.delete_spec(s)
        self.refresh_table()

    def _refresh_badge(self):
        n = count_flagged(self.db_path, self.host.table_name)
        self.badge.setText(f"⚑ {n}")
        self.badge.setStyleSheet(
            "padding:4px 8px; border-radius:10px; "
            f"background:{'#f03e3e' if n else '#adb5bd'}; color:white; font-weight:bold;"
        )

    def _recolor_status_cells(self):
        for r in range(self.table.rowCount()):
            spec = self.specs[r]
            status = Status.OFF
            if spec.enabled:
                try:
                    status = REGISTRY[spec.kind].evaluate(spec, self.host).get("status", Status.OFF)
                except Exception:
                    status = Status.OFF
            item = self.table.item(r, self.COL_STATUS)
            item.setText(_status_text(status, spec.enabled))
            color = _status_color(status, spec.enabled)
            item.setForeground(QBrush(QColor("#ffffff")))
            item.setBackground(QBrush(QColor(color)))

    # -------- Actions --------
    def duplicate_spec(self, spec: AlertSpec):
        new_spec = copy.deepcopy(spec)
        new_spec.id = str(uuid.uuid4())
        # Make name unique and mark as copy
        base = (new_spec.name or new_spec.kind or "Alert").strip()
        candidate = f"{base} (copy)"
        names = {s.name for s in self.specs if s.name}
        i = 2
        while candidate in names:
            candidate = f"{base} (copy {i})"; i += 1
        new_spec.name = candidate
        self.specs.append(new_spec)
        write_settings_audit(self.db_path, self.host.table_name, "created", json.dumps(new_spec.to_dict(), ensure_ascii=False))
        # Quiet baseline
        try:
            res = REGISTRY[new_spec.kind].evaluate(new_spec, self.host)
            cur_status = res.get("status", Status.OFF)
            cur_obs = float(res.get("observed", 0.0))
            write_last_status(self.db_path, self.host.table_name, self._key_for(new_spec), cur_status.value, cur_obs)
        except Exception:
            pass
        self._persist_current_settings()
        self.refresh_table()
        self._write_alerts_log(condition="duplicate", notes=f"Duplicated '{spec.name or spec.kind}' → '{new_spec.name}'", spec=new_spec)

    def _on_flag_toggle(self, spec: AlertSpec, state: int):
        checked = bool(state)
        key = self._key_for(spec)
        set_flag(self.db_path, self.host.table_name, key, checked)
        self._write_alerts_log(
            condition=f"flag:{'raise' if checked else 'clear'}",
            recipients=", ".join(spec.recipients),
            notes=f"Flag {'RAISED' if checked else 'CLEARED'} for '{spec.name or spec.kind}'", spec=spec
        )
        self._refresh_badge()

    def _clear_all_flags_clicked(self):
        btn = QMessageBox.question(
            self, "Clear all flags",
            "Clear ALL raised flags for this table?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        try:
            clear_all_flags(self.db_path, self.host.table_name)
            self._write_alerts_log(condition="flag:clear_all", notes="User cleared all flags")
            self.refresh_table()
        except Exception as e:
            QMessageBox.critical(self, "Clear flags error", str(e))

    def configure_spec(self, spec: AlertSpec):
        dlg = REGISTRY[spec.kind].create_editor(spec, self.host, self)
        if dlg.exec():
            if not spec.name:
                name, ok = QInputDialog.getText(self, "Alert name", "Enter a name:", text=spec.name or spec.kind)
                if ok: spec.name = name.strip()

            try:
                res = REGISTRY[spec.kind].evaluate(spec, self.host)
                cur_status = res.get("status", Status.OFF)
                cur_obs = float(res.get("observed", 0.0))
                write_last_status(self.db_path, self.host.table_name, self._key_for(spec), cur_status.value, cur_obs)

                en = enrich_extra_for_log(spec, res, self.host)
                self._write_alerts_log(
                    condition="baseline:set",
                    threshold=en.get("threshold"),
                    observed=en.get("observed", cur_obs),
                    last_lat=en.get("last_lat"),
                    last_lon=en.get("last_lon"),
                    recipients=", ".join(spec.recipients),
                    notes=f"Baseline set for '{spec.name or spec.kind}': {cur_status.value}",
                    spec=spec
                )

            except Exception as e:
                self._write_alerts_log(condition="baseline:error", notes=f"Baseline failed for '{spec.name or spec.kind}': {e}", spec=spec)

            write_settings_audit(self.db_path, self.host.table_name, "updated", json.dumps(spec.to_dict(), ensure_ascii=False))
            self._write_alerts_log(condition="configured", notes=f"Configured '{spec.name or spec.kind}'", spec=spec)
            self._persist_current_settings()
            self.refresh_table()

    def toggle_enable(self, spec: AlertSpec):
        spec.enabled = not spec.enabled
        try:
            res = REGISTRY[spec.kind].evaluate(spec, self.host)
            cur_status = res.get("status", Status.OFF)
            cur_obs = float(res.get("observed", 0.0))
            write_last_status(self.db_path, self.host.table_name, self._key_for(spec), cur_status.value, cur_obs)
            self._write_alerts_log(
                condition=("enable" if spec.enabled else "disable"),
                observed=cur_obs,
                recipients=", ".join(spec.recipients),
                notes=f"{'Enabled' if spec.enabled else 'Disabled'} '{spec.name or spec.kind}'. "
                      f"Baseline: {cur_status.value}",
                spec=spec,  # <-- pass spec here
            )
        except Exception as e:
            self._write_alerts_log(
                condition="toggle:error",
                notes=f"Toggle baseline failed for '{spec.name or spec.kind}': {e}",
                spec=spec
            )
        write_settings_audit(self.db_path, self.host.table_name, "updated",
                             json.dumps(spec.to_dict(), ensure_ascii=False))
        self._persist_current_settings()  # <-- persist after mutation
        self.refresh_table()

    def delete_spec(self, spec: AlertSpec):
        self.specs = [s for s in self.specs if s.id != spec.id]
        self._persist_current_settings()
        write_settings_audit(self.db_path, self.host.table_name, "deleted", json.dumps(spec.to_dict(), ensure_ascii=False))
        self._write_alerts_log(condition="deleted", notes=f"Removed alert '{spec.name or spec.kind}'", spec=spec)
        self.refresh_table()

    # -------- Evaluation loop --------
    def evaluate_all(self):
        if self._eval_running:
            return
        self._eval_running = True
        try:
            if not any(s.enabled for s in self.specs):
                return

            now = datetime.utcnow().replace(tzinfo=timezone.utc)

            for spec in self.specs:
                if not spec.enabled:
                    continue

                # --- per-alert scheduling ---
                key = self._key_for(spec)
                try:
                    interval_min = int(spec.payload.get("interval_min", 15))
                except Exception:
                    interval_min = 15
                interval_min = max(1, interval_min)  # sanity

                next_due = self._next_due.get(key)
                if next_due is None:
                    # Seed from last evaluation time in state DB so we honour per-alert interval on startup.
                    try:
                        _, updated_utc = read_last_status_meta(self.db_path, self.host.table_name, key)
                    except Exception:
                        updated_utc = None
                    if updated_utc:
                        try:
                            last_dt = _dt.datetime.strptime(updated_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            next_due = last_dt + pd.Timedelta(minutes=interval_min)
                        except Exception:
                            next_due = now
                    else:
                        next_due = now
                    self._next_due[key] = next_due

                if now < next_due:
                    # not due yet → skip this alert this pass
                    continue

                # if we’re here, the alert is due; schedule the next run now
                self._next_due[key] = now + pd.Timedelta(minutes=interval_min)

                handler = REGISTRY[spec.kind]
                try:
                    res = handler.evaluate(spec, self.host)
                except Exception as e:
                    self._write_alerts_log(condition="evaluate:error",
                                           notes=f"Evaluate error in '{spec.name or spec.kind}': {e}", spec=spec)
                    continue

                status: Status = res.get("status", Status.OFF)
                observed = float(res.get("observed", 0.0))
                summary = res.get("summary", "")
                en = enrich_extra_for_log(spec, res, self.host)  # ensures threshold/lat/lon present

                last = read_last_status(self.db_path, self.host.table_name, key)

                # First-time baseline for this alert in this table
                if last is None:
                    write_last_status(self.db_path, self.host.table_name, key, status.value, observed)
                    if status in (Status.AMBER, Status.RED):
                        set_flag(self.db_path, self.host.table_name, key, True)
                        self._write_alerts_log(
                            condition="flag:raise",
                            observed=observed,
                            recipients=", ".join(spec.recipients),
                            notes=f"Flag raised (baseline {status.value}) for '{spec.name or spec.kind}'", spec=spec
                        )
                    else:
                        set_flag(self.db_path, self.host.table_name, key, False)

                    self._write_alerts_log(
                        condition="baseline:first",
                        threshold=en.get("threshold"),
                        observed=en.get("observed", observed),
                        last_lat=en.get("last_lat"),
                        last_lon=en.get("last_lon"),
                        recipients=", ".join(spec.recipients),
                        notes=f"{spec.name or spec.kind}: initial {status.value} (no email)",
                        spec=spec
                    )

                    continue

                # Transitions only
                if last != status.value:
                    prev_status = last or "OFF"
                    cur_status = status.value

                    self._write_alerts_log(
                        condition=f"transition:{prev_status}->{cur_status}",
                        threshold=en.get("threshold"),
                        observed=en.get("observed", observed),
                        last_lat=en.get("last_lat"),
                        last_lon=en.get("last_lon"),
                        recipients=", ".join(spec.recipients),
                        notes=f"{spec.name or spec.kind} | {summary}", spec=spec
                    )

                    # Raise/clear flag
                    if cur_status in ("AMBER", "RED"):
                        set_flag(self.db_path, self.host.table_name, key, True)
                        self._write_alerts_log(
                            condition="flag:raise",
                            observed=en.get("observed", observed),
                            recipients=", ".join(spec.recipients),
                            notes=f"Flag raised ({cur_status}) for '{spec.name or spec.kind}'", spec=spec
                        )
                    else:
                        # clear the flag in the DB
                        set_flag(self.db_path, self.host.table_name, key, False)
                        self._write_alerts_log(
                            condition="flag:clear",
                            observed=en.get("observed", observed),
                            recipients=", ".join(spec.recipients),
                            notes=f"Flag cleared ({cur_status}) for '{spec.name or spec.kind}'", spec=spec
                        )

                    # ---------- Email policy ----------
                    cooldown_min = int(spec.payload.get("email_cooldown_min", 240))  # default 4h
                    email_on_escalation = bool(spec.payload.get("email_on_escalation", False))
                    email_on_recovery = bool(spec.payload.get("email_on_recovery", False))

                    entered_alert = (prev_status in ("OFF", "GREEN") and cur_status in ("AMBER", "RED"))
                    escalated = (prev_status == "AMBER" and cur_status == "RED")
                    recovered = (prev_status in ("AMBER", "RED") and cur_status == "GREEN")

                    should_consider_email = False
                    if entered_alert:
                        should_consider_email = True
                    elif escalated and email_on_escalation:
                        should_consider_email = True
                    elif recovered and email_on_recovery:
                        should_consider_email = True

                    if should_consider_email:
                        last_em_status, last_em_utc = read_last_email(self.db_path, self.host.table_name, key)
                        can_email = True
                        if cooldown_min > 0 and last_em_utc:
                            try:
                                def _parse_email_ts_to_utc(s: str):
                                    """
                                    Timestamps written by write_last_email() are naive UTC (YYYY-mm-dd HH:MM:SS).
                                    If tzinfo is missing, treat as UTC (not local).
                                    """
                                    try:
                                        dt = datetime.fromisoformat(s)
                                    except Exception:
                                        try:
                                            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                                        except Exception:
                                            return None
                                    if dt.tzinfo is None:
                                        return dt.replace(tzinfo=timezone.utc)
                                    return dt.astimezone(timezone.utc)

                                prev_dt = _parse_email_ts_to_utc(last_em_utc)
                                if prev_dt and (now - prev_dt).total_seconds() < cooldown_min * 60:
                                    can_email = False
                                    # Avoid spamming "email:skipped" — only log once per cooldown window.
                                    mute_until = self._skip_logged_until.get(key)
                                    if not mute_until or now >= mute_until:
                                        self._write_alerts_log(
                                            condition="email:skipped",
                                            threshold=en.get("threshold"),
                                            observed=en.get("observed", observed),
                                            last_lat=en.get("last_lat"),
                                            last_lon=en.get("last_lon"),
                                            recipients=", ".join(spec.recipients),
                                            notes=f"Cool-down active ({cooldown_min} min) for '{spec.name or spec.kind}'",
                                            spec=spec
                                        )
                                        self._skip_logged_until[key] = now + pd.Timedelta(minutes=cooldown_min)
                            except Exception:
                                pass  # if parsing fails, allow email

                        if can_email:
                            recipients = list(spec.recipients) or list(res.get("recipients", []) or [])
                            if recipients:
                                subj = f"ALERT [{cur_status}]: {self.host.table_name} — {spec.name or spec.kind}"
                                body = (
                                    f"{spec.name or spec.kind}\n"
                                    f"Table: {self.host.table_name}\n"
                                    f"Status: {cur_status} (from {prev_status})\n"
                                    f"Observed: {observed}\n"
                                    f"Details: {summary}"
                                )
                                try:
                                    send_email_outlook(subj, body, recipients, None)
                                    write_last_email(self.db_path, self.host.table_name, key, cur_status)
                                    self._write_alerts_log(
                                        condition="email:sent",
                                        threshold=en.get("threshold"),
                                        observed=en.get("observed", observed),
                                        last_lat=en.get("last_lat"),
                                        last_lon=en.get("last_lon"),
                                        recipients=", ".join(recipients),
                                        notes=f"Email sent ({cur_status}) for '{spec.name or spec.kind}'",
                                        spec=spec
                                    )
                                except Exception as e:
                                    QMessageBox.critical(self, "Email error", str(e))
                                    self._write_alerts_log(
                                        condition="email:error",
                                        observed=observed,
                                        recipients=", ".join(recipients),
                                        notes=f"{e}",
                                        spec=spec
                                    )

                    # Persist the new status after handling transition/email
                    write_last_status(self.db_path, self.host.table_name, key, cur_status, observed)

            self._recolor_status_cells()
            self._refresh_badge()
            self._load_alerts_log()
        finally:
            self._eval_running = False

    def _log_quiet(self, msg: str):
        # Default: quiet. Flip to False to re-enable console prints for debugging.
        if not getattr(self, "quiet_refresh_logs", True):
            try:
                print(msg)
            except Exception:
                pass

    def generate_report_dialog(self):
        """
        Export a CSV based on alerts HISTORY (alerts_log), with a summary header
        and the full log appended afterward.
        """
        path, _ = QFileDialog.getSaveFileName(self, "Save alerts history report", "", "CSV files (*.csv)")
        if not path:
            return

        try:
            state_db_path = get_state_db_path_for(self.db_path)
            conn = sqlite3.connect(state_db_path)
            df = pd.read_sql_query(
                """
                SELECT id, created_utc, condition, threshold, observed, last_lat, last_lon, recipients, notes
                FROM alerts_log
                WHERE table_name=?
                ORDER BY id ASC
                """,
                conn, params=(self.host.table_name,)
            )
            conn.close()
        except Exception as e:
            QMessageBox.critical(self, "Report error", f"Failed to read history: {e}")
            return

        if df.empty:
            QMessageBox.information(self, "Generate report", "No history entries found.")
            return

        # ---- Build summary ----
        df["created_utc"] = pd.to_datetime(df["created_utc"], errors="coerce", utc=True)
        first_ts = df["created_utc"].min()
        last_ts = df["created_utc"].max()

        by_condition = df["condition"].value_counts().sort_index()
        emails_sent = int((df["condition"] == "email:sent").sum())
        emails_skipped = int((df["condition"] == "email:skipped").sum())
        transitions = int(df["condition"].str.startswith("transition:").sum())
        flags_raised = int((df["condition"] == "flag:raise").sum())
        flags_cleared = int((df["condition"] == "flag:clear").sum())

        # recipients frequency (rough parse)
        recips = (
            df["recipients"].dropna()
            .astype(str)
            .str.split(r"[;,]\s*")
            .explode()
            .str.strip()
        )
        recips = recips[recips != ""]
        top_recips = recips.value_counts().head(20)

        # ---- Write to CSV ----
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Alerts history report\n")
                f.write(f"# Table: {self.host.table_name}\n")
                if pd.notna(first_ts):
                    f.write(
                        f"# Range (UTC): {first_ts.strftime('%Y-%m-%d %H:%M:%S')}  →  {last_ts.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("#\n")
                f.write("# Counts by condition:\n")
                for cond, cnt in by_condition.items():
                    f.write(f"#   {cond}: {int(cnt)}\n")
                f.write(f"#\n# Emails sent: {emails_sent}\n")
                f.write(f"# Emails skipped (cooldown): {emails_skipped}\n")
                f.write(f"# Transitions: {transitions}\n")
                f.write(f"# Flags raised: {flags_raised}\n")
                f.write(f"# Flags cleared: {flags_cleared}\n")
                if not top_recips.empty:
                    f.write("#\n# Top recipients:\n")
                    for addr, cnt in top_recips.items():
                        f.write(f"#   {addr}: {int(cnt)}\n")
                f.write("#\n# --- full history (UTC) ---\n")

            # Write full log underneath
            out = df.rename(columns={
                "created_utc": "created_utc",
                "condition": "condition",
                "threshold": "threshold",
                "observed": "observed",
                "last_lat": "last_lat",
                "last_lon": "last_lon",
                "recipients": "recipients",
                "notes": "notes",
            })
            # keep ISO string for created_utc
            out["created_utc"] = out["created_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")
            out.to_csv(path, index=False, mode="a")

            self._write_alerts_log(condition="report", notes=f"History report saved to {path}")
            QMessageBox.information(self, "Generate report", f"Saved report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Report error", str(e))

    # -------- Persistence (unchanged external API) --------
    def export_settings(self) -> dict:
        return {
            "table": self.host.table_name,
            "version": 6,  # bumped for toolbar overhaul + minutes timer
            "items": [s.to_dict() for s in self.specs],
            "timer_min": int(max(1, getattr(self, "_timer_min", 5))),
        }

    def import_settings(self, data: dict):
        if not isinstance(data, dict): return
        if data.get("table") and data["table"] != self.host.table_name: return
        # accept legacy timer_ms, but prefer timer_min
        t_min = int(data.get("timer_min", 0))
        if t_min <= 0:
            legacy_ms = int(data.get("timer_ms", 300000))  # default to 5 min if absent
            t_min = max(1, int(round(legacy_ms / 60000)))
        self._timer_min = t_min
        self.timer.setInterval(max(60_000, int(self._timer_min) * 60_000))

        self.specs = [AlertSpec.from_dict(d) for d in data.get("items", [])]
        # Purge any orphaned state rows so badge and checkboxes match the current set.
        try:
            purge_orphaned_alert_state(self.db_path, self.host.table_name, [s.id for s in self.specs if s.id])
        except Exception:
            pass
        self.refresh_table()
        self._load_alerts_log()
