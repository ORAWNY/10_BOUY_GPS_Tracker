from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import os
import sys
import time
import subprocess
import re

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QRunnable, QThreadPool
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QCheckBox, QSpinBox, QHeaderView
)

from .email_parser_dialog import EmailParserDialog
from .email_parser_core import EmailParserConfig as CoreConfig, run_parser as core_run_parser


# --------------------------- Manager-side model ---------------------------
@dataclass
class ManagedParser:
    name: str
    core: CoreConfig
    refresh_tabs: bool = False
    timer: Optional[QTimer] = None  # not persisted


class _RunTask(QRunnable):
    def __init__(self, manager: 'EmailParserManager', idx: int, force_refresh: bool):
        super().__init__()
        self.manager = manager
        self.idx = idx
        self.force = force_refresh

    def run(self):
        self.manager._run_one_index(self.idx, self.force)


class EmailParserManager(QObject):
    """
    Holds parser configs, persists them, and runs them on a GLOBAL interval.

    Guardrails:
      - Auto-restart Outlook on Outlook/COM errors (optional).
      - If retry still fails, optionally ask the GUI to restart the app.
    """
    log = pyqtSignal(str)
    request_refresh_tabs = pyqtSignal(str, bool)  # (db_path, force)
    app_restart_requested = pyqtSignal(str)       # reason

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parsers: List[ManagedParser] = []
        self._global_interval_min: int = 15
        self._pool = QThreadPool.globalInstance()
        # Ensure Outlook/Webhook aren’t hit in parallel
        self._pool.setMaxThreadCount(1)

        # Guardrail toggles
        self._auto_restart_outlook: bool = True
        self._restart_app_on_outlook_fail: bool = True

    # -------------------- Guardrail API (called by GUI) --------------------
    def set_auto_restart_outlook(self, enabled: bool):
        self._auto_restart_outlook = bool(enabled)
        self.log.emit(f"Auto-restart Outlook: {'ON' if self._auto_restart_outlook else 'OFF'}")

    def set_restart_app_on_outlook_fail(self, enabled: bool):
        self._restart_app_on_outlook_fail = bool(enabled)
        self.log.emit(f"Restart app on persistent Outlook error: {'ON' if self._restart_app_on_outlook_fail else 'OFF'}")

    @staticmethod
    def _looks_like_outlook_error(exc: Exception) -> bool:
        s = (str(exc) or "").lower()
        needles = (
            "pywintypes.com_error",
            "microsoft outlook",
            "mapi",
            "rpc_e_server_unavailable",
            "the rpc server is unavailable",
            "call was rejected by callee",
            "class not registered",
            "outlook is not running",
            "the attempted operation failed.  an object could not be found.",
            "folder not found",
            "-2147221233",
            "-2147352567",
        )
        return any(n in s for n in needles)

    @staticmethod
    def _restart_outlook(wait_s: float = 12.0):
        try:
            if sys.platform.startswith("win"):
                try:
                    subprocess.run(["taskkill", "/F", "/IM", "OUTLOOK.EXE"], capture_output=True)
                except Exception:
                    pass
                try:
                    subprocess.Popen(["cmd", "/c", "start", "", "outlook.exe"], shell=True)
                except Exception:
                    try:
                        os.startfile("outlook")
                    except Exception:
                        pass
        finally:
            time.sleep(wait_s)

    # -------------------- Helpers for state & naming --------------------
    @staticmethod
    def _compute_state_dir(core_cfg: CoreConfig) -> str:
        """
        Base directory for per-parser state. Core will place DBs under a 'state/' subfolder.
        For DB mode → folder of the .db; for file modes → output_dir; else CWD.
        """
        data_base = (
            os.path.dirname(core_cfg.db_path)
            if (core_cfg.output_format or "db").lower() == "db" and core_cfg.db_path
            else core_cfg.output_dir or os.getcwd()
        )
        os.makedirs(data_base, exist_ok=True)
        return data_base  # core puts the file under data_base/state/

    @staticmethod
    def _slug(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"[^a-z0-9_.-]+", "_", s)
        return re.sub(r"_+", "_", s).strip("._") or "parser"

    # -------------------- Persistence --------------------
    def to_json(self) -> Dict[str, Any]:
        """
        Persist the ENTIRE CoreConfig (so we don't forget new fields like FTP ones).
        Falls back to a manual dict if to_dict() isn't available for any reason.
        """
        payload: Dict[str, Any] = {
            "global_interval_min": self._global_interval_min,
            "parsers": []
        }

        for p in self._parsers:
            # Prefer the dataclass serializer if present
            core: Dict[str, Any]
            if hasattr(p.core, "to_dict"):
                try:
                    core = p.core.to_dict()  # includes ftp_host, ftp_port, ftp_username, ftp_password, ...
                except Exception:
                    core = {}  # fall through to manual mapping below
            else:
                core = {}

            if not core:
                # Manual mapping (kept for robustness)
                core = {
                    "mailbox": p.core.mailbox,
                    "db_path": p.core.db_path,
                    "folder_paths": list(p.core.folder_paths or []),
                    "auto_run": bool(p.core.auto_run),
                    "output_format": p.core.output_format,
                    "output_dir": p.core.output_dir,
                    "file_granularity": p.core.file_granularity,
                    "lookup_path": p.core.lookup_path,
                    "filename_pattern": p.core.filename_pattern,
                    "filename_code": p.core.filename_code,
                    "missing_value": p.core.missing_value,
                    "parser_name": p.core.parser_name,
                    "state_dir": p.core.state_dir,
                    "lookback_hours": getattr(p.core, "lookback_hours", 2),

                    # --- FTP / FTPS fields ---
                    "use_local_output": getattr(p.core, "use_local_output", True),
                    "use_ftp_output": getattr(p.core, "use_ftp_output", False),
                    "ftp_host": getattr(p.core, "ftp_host", ""),
                    "ftp_port": getattr(p.core, "ftp_port", 21),
                    "ftp_username": getattr(p.core, "ftp_username", ""),
                    "ftp_password": getattr(p.core, "ftp_password", ""),
                    "ftp_remote_dir": getattr(p.core, "ftp_remote_dir", ""),
                    "ftp_use_tls": getattr(p.core, "ftp_use_tls", False),
                    "ftp_passive": getattr(p.core, "ftp_passive", True),
                    "ftp_timeout": getattr(p.core, "ftp_timeout", 20),
                    "ftp_check_on_start": getattr(p.core, "ftp_check_on_start", True),
                    "ftp_delete_local_after_upload": getattr(p.core, "ftp_delete_local_after_upload", False),

                    # WEBHOOK fields
                    "webhook_enabled": getattr(p.core, "webhook_enabled", False),
                    "webhook_url": getattr(p.core, "webhook_url", ""),
                    "webhook_auth_header": getattr(p.core, "webhook_auth_header", ""),
                    "webhook_since_param": getattr(p.core, "webhook_since_param", "since"),
                    "webhook_limit_param": getattr(p.core, "webhook_limit_param", "limit"),
                    "webhook_limit": getattr(p.core, "webhook_limit", 200),

                    # Manual / checkpoint
                    "manual_from": getattr(p.core, "manual_from", ""),
                    "manual_to": getattr(p.core, "manual_to", ""),
                    "respect_checkpoint": getattr(p.core, "respect_checkpoint", True),
                    "update_checkpoint": getattr(p.core, "update_checkpoint", True),
                    "reset_state_before_run": getattr(p.core, "reset_state_before_run", False),

                    # TXT options
                    "txt_timestamp_mode": getattr(p.core, "txt_timestamp_mode", "payload"),
                    "quiet": getattr(p.core, "quiet", True),
                }

            payload["parsers"].append({
                "name": p.name,
                "refresh_tabs": p.refresh_tabs,
                "core": core,
            })

        return payload

    def from_json(self, payload: Dict[str, Any]):
        self._parsers.clear()
        if not payload:
            return

        self._global_interval_min = int(payload.get("global_interval_min", 15))

        for raw in payload.get("parsers", []):
            c = raw.get("core", raw) or {}
            core = CoreConfig.from_dict(c)
            name = raw.get("name") or self._derive_name(core)
            refresh_tabs = bool(raw.get("refresh_tabs", False))

            if not getattr(core, "parser_name", ""):
                core.parser_name = name
            core.state_dir = core.state_dir or self._compute_state_dir(core)

            self._parsers.append(ManagedParser(name=name, core=core, refresh_tabs=refresh_tabs))

        self._rebuild_all_timers()

    # -------------------- Public API --------------------
    def add_parser(self, core_cfg: CoreConfig, name: Optional[str] = None):
        if name is None:
            name = self._derive_name(core_cfg)
        if not getattr(core_cfg, "parser_name", ""):
            core_cfg.parser_name = name
        core_cfg.state_dir = self._compute_state_dir(core_cfg)

        mp = ManagedParser(name=name, core=core_cfg, refresh_tabs=False)
        self._parsers.append(mp)
        where = core_cfg.db_path if core_cfg.output_format == "db" else core_cfg.output_dir
        self.log.emit(f"Added parser '{mp.name}' → {core_cfg.output_format.upper()}: {where}")
        self._ensure_timer(mp)

    def update_parser(self, index: int, new_core: CoreConfig):
        if 0 <= index < len(self._parsers):
            mp = self._parsers[index]
            if mp.timer:
                mp.timer.stop()
                mp.timer.deleteLater()
                mp.timer = None
            if not getattr(new_core, "parser_name", ""):
                new_core.parser_name = self._derive_name(new_core)
            new_core.state_dir = self._compute_state_dir(new_core)
            mp.core = new_core
            mp.name = new_core.parser_name
            self._ensure_timer(mp)
            self.log.emit(f"Updated parser '{mp.name}'")

    def rename_parser(self, index: int, new_name: str):
        if not (0 <= index < len(self._parsers)):
            return
        mp = self._parsers[index]
        old_name = mp.name
        new_name = (new_name or "").strip()
        if not new_name or new_name == old_name:
            return

        state_base = mp.core.state_dir or self._compute_state_dir(mp.core)
        state_dir = state_base if os.path.basename(os.path.normpath(state_base)).lower() == "state" else os.path.join(state_base, "state")
        os.makedirs(state_dir, exist_ok=True)
        old_path = os.path.join(state_dir, f".email_parser_state_{self._slug(mp.core.parser_name)}.db")
        new_path = os.path.join(state_dir, f".email_parser_state_{self._slug(new_name)}.db")

        try:
            if os.path.isfile(old_path) and old_path != new_path:
                os.replace(old_path, new_path)
        except Exception as e:
            self.log.emit(f"Rename warning: could not move state DB '{old_path}' → '{new_path}': {e}")

        mp.core.parser_name = new_name
        mp.name = new_name
        self.log.emit(f"Renamed parser '{old_name}' → '{new_name}'")

    def set_global_interval(self, minutes: int):
        minutes = max(1, int(minutes))
        self._global_interval_min = minutes
        self._rebuild_all_timers()
        self.log.emit(f"Global interval set to {minutes} min")

    def parsers(self) -> List[ManagedParser]:
        return self._parsers

    def run_now(self, index: int, *, force_refresh: bool = True):
        if 0 <= index < len(self._parsers):
            self.log.emit(f"[{self._parsers[index].name}] Queued to run now…")
            self._pool.start(_RunTask(self, index, force_refresh))

    def run_all_now(self):
        for i in range(len(self._parsers)):
            self.run_now(i, force_refresh=True)

    # -------------------- Timers --------------------
    def _rebuild_all_timers(self):
        for p in self._parsers:
            self._ensure_timer(p)

    def _ensure_timer(self, p: ManagedParser):
        if p.timer:
            p.timer.stop()
            p.timer.deleteLater()
            p.timer = None

        if not p.core.auto_run:
            return

        p.timer = QTimer(self)
        p.timer.setInterval(max(1, self._global_interval_min) * 60 * 1000)
        p.timer.timeout.connect(lambda mp=p: self.run_now(self._parsers.index(mp), force_refresh=False))
        p.timer.start()
        self.log.emit(f"Auto-update ON for '{p.name}' every {self._global_interval_min} min")

    # -------------------- Runner --------------------
    def _run_one_index(self, idx: int, force_refresh: bool):
        mp = self._parsers[idx]
        try:
            self.log.emit(f"[{mp.name}] Running parser…")
            core_run_parser(mp.core, logger=lambda s: self.log.emit(f"[{mp.name}] {s}"))
            self.log.emit(f"[{mp.name}] Update complete.")
            if mp.refresh_tabs:
                self.request_refresh_tabs.emit(mp.core.db_path, False)
            return

        except Exception as e:
            self.log.emit(f"[{mp.name}] ERROR: {e}")

            if self._auto_restart_outlook and self._looks_like_outlook_error(e):
                self.log.emit(f"[{mp.name}] Outlook/COM error detected; restarting Outlook and retrying once…")
                self._restart_outlook()
                try:
                    core_run_parser(mp.core, logger=lambda s: self.log.emit(f"[{mp.name}] {s}"))
                    self.log.emit(f"[{mp.name}] Update complete after Outlook restart.")
                    if mp.refresh_tabs:
                        self.request_refresh_tabs.emit(mp.core.db_path, False)
                    return
                except Exception as e2:
                    self.log.emit(f"[{mp.name}] Retry failed after Outlook restart: {e2}")
                    # Optional: emit app_restart_requested if you want a full app restart hook

    # -------------------- Hooks from UI --------------------
    def set_auto_update(self, index: int, enabled: bool):
        if 0 <= index < len(self._parsers):
            self._parsers[index].core.auto_run = enabled
            self._ensure_timer(self._parsers[index])

    def set_refresh_tabs(self, index: int, enabled: bool):
        if 0 <= index < len(self._parsers):
            self._parsers[index].refresh_tabs = enabled

    # -------------------- Utils --------------------
    @staticmethod
    def _derive_name(core_cfg: CoreConfig) -> str:
        if core_cfg.output_format == "db" and core_cfg.db_path:
            base = os.path.splitext(os.path.basename(core_cfg.db_path))[0]
            if base:
                return base
        if core_cfg.output_format in ("csv", "txt") and core_cfg.output_dir:
            return f"{os.path.basename(core_cfg.output_dir)}_{core_cfg.output_format}"
        return core_cfg.mailbox or "Parser"


# =============================== Dock UI ===============================
class EmailParsersDock(QWidget):
    """
    Dock ui:
      - Global interval (numeric)
      - Table: Name | Auto-update | Refresh tabs | Run | Edit
      - Add / Remove / Run All Now
      - Inline rename (edit the Name cell)
    """

    def __init__(self, mgr: EmailParserManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr

        v = QVBoxLayout(self)

        # Header with title + global interval
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Email/Webhook Parsers"))
        hdr.addStretch(1)
        hdr.addWidget(QLabel("Global interval (min):"))
        self.spin_global = QSpinBox()
        self.spin_global.setRange(1, 10000)
        self.spin_global.setValue(self.mgr._global_interval_min)
        self.spin_global.valueChanged.connect(self.mgr.set_global_interval)
        hdr.addWidget(self.spin_global)
        v.addLayout(hdr)

        # Table (5 columns)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Name", "Auto-update", "Refresh tabs", "Run", "Edit"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        v.addWidget(self.table, 1)

        # Buttons
        btns = QHBoxLayout()
        self.btn_add = QPushButton("Add…")
        self.btn_remove = QPushButton("Remove Selected")
        self.btn_run_all = QPushButton("Run All Now")
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_remove)
        btns.addStretch(1)
        btns.addWidget(self.btn_run_all)
        v.addLayout(btns)

        # Events
        self.btn_add.clicked.connect(self._add_clicked)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_run_all.clicked.connect(self._run_all_clicked)

        # Inline rename
        self.table.itemChanged.connect(self._maybe_name_changed)

        # Initial fill
        self.reload()

    def reload(self):
        self.table.setRowCount(0)
        for idx, mp in enumerate(self.mgr.parsers()):
            self._append_row(idx, mp)

    def _append_row(self, index: int, mp: ManagedParser):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Name (editable)
        item_name = QTableWidgetItem(mp.name)
        self.table.setItem(row, 0, item_name)

        # Auto-update checkbox
        chk_auto = QCheckBox()
        chk_auto.setChecked(mp.core.auto_run)
        chk_auto.toggled.connect(lambda checked, i=index: self.mgr.set_auto_update(i, checked))
        self.table.setCellWidget(row, 1, chk_auto)

        # Refresh tabs checkbox
        chk_ref = QCheckBox()
        chk_ref.setChecked(mp.refresh_tabs)
        chk_ref.toggled.connect(lambda checked, i=index: self.mgr.set_refresh_tabs(i, checked))
        self.table.setCellWidget(row, 2, chk_ref)

        # Run Now button
        btn_run = QPushButton("Run")
        btn_run.clicked.connect(lambda _, i=index: self._run_one(i))
        self.table.setCellWidget(row, 3, btn_run)

        # Edit button
        btn_edit = QPushButton("Edit…")
        btn_edit.clicked.connect(lambda _, i=index: self._edit_one(i))
        self.table.setCellWidget(row, 4, btn_edit)

    def _maybe_name_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        row = item.row()
        new_name = (item.text() or "").strip()
        if not new_name:
            if 0 <= row < len(self.mgr.parsers()):
                item.setText(self.mgr.parsers()[row].name)
            return
        if 0 <= row < len(self.mgr.parsers()):
            self.mgr.rename_parser(row, new_name)
            item.setText(self.mgr.parsers()[row].name)

    def _selected_index(self) -> Optional[int]:
        rows = {r.row() for r in self.table.selectedIndexes()}
        if not rows:
            return 0 if self.table.rowCount() else None
        return sorted(rows)[0]

    # ----- Table row actions -----
    def _run_one(self, index: int):
        self.mgr.run_now(index, force_refresh=True)

    def _run_all_clicked(self):
        self.mgr.run_all_now()

    def _edit_one(self, index: int):
        if 0 <= index < len(self.mgr._parsers):
            mp = self.mgr._parsers[index]
            dlg = EmailParserDialog(self, initial=mp.core)
            if dlg.exec() == dlg.DialogCode.Accepted:
                new_core = dlg.get_config()
                self.mgr.update_parser(index, new_core)
                self.reload()

    def _add_clicked(self):
        dlg = EmailParserDialog(self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            core_cfg = dlg.get_config()
            name = EmailParserManager._derive_name(core_cfg)
            self.mgr.add_parser(core_cfg, name=name)
            self.reload()

    def _remove_selected(self):
        rows = sorted({r.row() for r in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            idx = row
            if 0 <= idx < len(self.mgr._parsers):
                mp = self.mgr._parsers[idx]
                if mp.timer:
                    mp.timer.stop()
                    mp.timer.deleteLater()
                self.mgr._parsers.pop(idx)
                self.mgr.log.emit(f"Removed parser '{mp.name}'")
        self.reload()
