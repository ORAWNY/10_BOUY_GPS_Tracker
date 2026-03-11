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
    QTableWidget, QTableWidgetItem, QCheckBox, QSpinBox, QHeaderView,
    QScrollArea, QFrame, QLineEdit, QMessageBox,
)
from utils.collapsible_section import CollapsibleSection

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
                self.request_refresh_tabs.emit(mp.core.db_path, force_refresh)
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
                        self.request_refresh_tabs.emit(mp.core.db_path, force_refresh)
                    return
                except Exception as e2:
                    self.log.emit(f"[{mp.name}] Retry failed after Outlook restart: {e2}")
                    if self._restart_app_on_outlook_fail:
                        self.log.emit(f"[{mp.name}] Requesting app restart due to persistent Outlook error.")
                        self.app_restart_requested.emit("outlook_error_persisted")
                    return  # prevent falling through

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

_PILL_SS = {
    # idle: semi-transparent so it adapts to light and dark backgrounds
    "idle":    "border-radius:8px; padding:1px 7px; font-size:10px; "
               "background: rgba(128,128,128,0.18); border: 1px solid rgba(128,128,128,0.3);",
    "running": "color:#ffffff; background:#2563eb; border-radius:8px; padding:1px 7px; font-size:10px;",
    "ok":      "color:#ffffff; background:#16a34a; border-radius:8px; padding:1px 7px; font-size:10px;",
    "error":   "color:#ffffff; background:#dc2626; border-radius:8px; padding:1px 7px; font-size:10px;",
}

_BTN_SS = (
    "QPushButton { font-size: 11px; border: 1px solid rgba(128,128,128,0.4); border-radius: 5px; "
    "  padding: 3px 10px; background: transparent; }"
    "QPushButton:hover   { background: rgba(128,128,128,0.15); }"
    "QPushButton:pressed { background: rgba(128,128,128,0.3); }"
)
_BTN_BLUE_SS = (
    "QPushButton { font-size: 11px; border: none; border-radius: 5px; "
    "  padding: 3px 10px; background: #2563eb; color: #ffffff; font-weight: 600; }"
    "QPushButton:hover   { background: #1d4ed8; }"
    "QPushButton:pressed { background: #1e40af; }"
)
_BTN_RED_SS = (
    "QPushButton { font-size: 11px; border: 1px solid rgba(220,38,38,0.5); border-radius: 5px; "
    "  padding: 3px 10px; background: transparent; color: #dc2626; }"
    "QPushButton:hover   { background: rgba(220,38,38,0.1); }"
    "QPushButton:pressed { background: rgba(220,38,38,0.2); }"
)


class _ParserCard(CollapsibleSection):
    """One collapsible card per ManagedParser."""

    def __init__(self, index: int, mp: ManagedParser, dock: "EmailParsersDock"):
        super().__init__(mp.name, dock, collapsed=True)
        self._index = index
        self._mp = mp
        self._dock = dock

        # ── Status pill in header ────────────────────────────────────────────
        self._status_pill = QLabel("idle")
        self._status_pill.setStyleSheet(_PILL_SS["idle"])
        self.add_header_widget(self._status_pill)

        # ── Auto-run toggle (always visible in header) ───────────────────────
        self._auto_chk = QCheckBox("Auto")
        self._auto_chk.setChecked(mp.core.auto_run)
        self._auto_chk.setToolTip("Run on the global timer interval")
        self._auto_chk.toggled.connect(self._on_auto_toggled)
        self.add_header_widget(self._auto_chk)

        # ── Body ─────────────────────────────────────────────────────────────
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(10, 8, 10, 8)
        body_lay.setSpacing(6)

        # Info row
        fmt  = (mp.core.output_format or "db").upper()
        dest = mp.core.db_path if fmt == "DB" else (mp.core.output_dir or "—")
        src  = "Webhook" if getattr(mp.core, "webhook_enabled", False) else (mp.core.mailbox or "Outlook")
        info = QLabel(f"<b>Source:</b> {src}   <b>Format:</b> {fmt}   <b>Output:</b> {dest}")
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 11px; background: transparent;")
        body_lay.addWidget(info)

        # Refresh-tabs toggle
        ref_row = QHBoxLayout()
        self._ref_chk = QCheckBox("Refresh tabs after run")
        self._ref_chk.setChecked(mp.refresh_tabs)
        self._ref_chk.toggled.connect(self._on_ref_toggled)
        ref_row.addWidget(self._ref_chk)
        ref_row.addStretch(1)
        body_lay.addLayout(ref_row)

        # Rename
        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit(mp.name)
        self._name_edit.setFixedHeight(26)
        rename_row.addWidget(self._name_edit, 1)
        btn_rename = QPushButton("Rename")
        btn_rename.setFixedHeight(26)
        btn_rename.setStyleSheet(_BTN_SS)
        btn_rename.clicked.connect(self._on_rename)
        rename_row.addWidget(btn_rename)
        body_lay.addLayout(rename_row)

        # Action buttons
        act_row = QHBoxLayout()
        act_row.setSpacing(6)
        btn_run = QPushButton("▶  Run Now")
        btn_run.setFixedHeight(28)
        btn_run.setStyleSheet(_BTN_BLUE_SS)
        btn_run.clicked.connect(self._on_run)
        act_row.addWidget(btn_run)

        btn_edit = QPushButton("⚙  Configure…")
        btn_edit.setFixedHeight(28)
        btn_edit.setStyleSheet(_BTN_SS)
        btn_edit.clicked.connect(self._on_edit)
        act_row.addWidget(btn_edit)

        act_row.addStretch(1)

        btn_remove = QPushButton("✕  Remove")
        btn_remove.setFixedHeight(28)
        btn_remove.setStyleSheet(_BTN_RED_SS)
        btn_remove.clicked.connect(self._on_remove)
        act_row.addWidget(btn_remove)
        body_lay.addLayout(act_row)

        self.set_body(body)

    # ── Actions ──────────────────────────────────────────────────────────────

    def set_status(self, state: str):
        """state: 'idle' | 'running' | 'ok' | 'error'"""
        labels = {"idle": "idle", "running": "running…", "ok": "✓ ok", "error": "✕ error"}
        self._status_pill.setText(labels.get(state, state))
        self._status_pill.setStyleSheet(_PILL_SS.get(state, _PILL_SS["idle"]))

    def _on_auto_toggled(self, checked: bool):
        self._dock.mgr.set_auto_update(self._index, checked)

    def _on_ref_toggled(self, checked: bool):
        self._dock.mgr.set_refresh_tabs(self._index, checked)

    def _on_rename(self):
        new_name = (self._name_edit.text() or "").strip()
        if not new_name:
            return
        self._dock.mgr.rename_parser(self._index, new_name)
        self.set_title(self._dock.mgr.parsers()[self._index].name)

    def _on_run(self):
        self.set_status("running")
        self._dock.mgr.run_now(self._index, force_refresh=True)

    def _on_edit(self):
        self._dock._edit_one(self._index)

    def _on_remove(self):
        if QMessageBox.question(
            self._dock, "Remove parser",
            f"Remove parser '{self._mp.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            mp = self._dock.mgr._parsers[self._index]
            if mp.timer:
                mp.timer.stop()
                mp.timer.deleteLater()
            self._dock.mgr._parsers.pop(self._index)
            self._dock.mgr.log.emit(f"Removed parser '{mp.name}'")
            self._dock.reload()


class EmailParsersDock(QWidget):
    """
    Dock UI: one collapsible card per parser.

    Each card is collapsed by default to save space.
    Expand a card to see its source, output, controls, and rename/remove options.
    """

    def __init__(self, mgr: EmailParserManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self._cards: List[_ParserCard] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ── Top bar ──────────────────────────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)

        title = QLabel("Email / Webhook Parsers")
        title.setStyleSheet("font-weight: 600; font-size: 12px;")
        top.addWidget(title, 1)

        top.addWidget(QLabel("Interval:"))
        self.spin_global = QSpinBox()
        self.spin_global.setRange(1, 10000)
        self.spin_global.setValue(self.mgr._global_interval_min)
        self.spin_global.setSuffix(" min")
        self.spin_global.setFixedWidth(80)
        self.spin_global.setToolTip("Global auto-run interval (minutes)")
        self.spin_global.valueChanged.connect(self.mgr.set_global_interval)
        top.addWidget(self.spin_global)

        self.btn_run_all = QPushButton("▶▶ Run All")
        self.btn_run_all.setFixedHeight(28)
        self.btn_run_all.setStyleSheet(_BTN_BLUE_SS)
        self.btn_run_all.clicked.connect(self._run_all_clicked)
        top.addWidget(self.btn_run_all)

        self.btn_add = QPushButton("＋ Add")
        self.btn_add.setFixedHeight(28)
        self.btn_add.setStyleSheet(_BTN_SS)
        self.btn_add.clicked.connect(self._add_clicked)
        top.addWidget(self.btn_add)

        root.addLayout(top)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: rgba(128,128,128,0.3);")
        root.addWidget(sep)

        # ── Scrollable cards area ─────────────────────────────────────────────
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll, 1)

        self._cards_widget = QWidget()
        self._cards_lay = QVBoxLayout(self._cards_widget)
        self._cards_lay.setContentsMargins(0, 0, 0, 0)
        self._cards_lay.setSpacing(4)
        self._cards_lay.addStretch(1)
        self._scroll.setWidget(self._cards_widget)

        # ── Empty placeholder ─────────────────────────────────────────────────
        self._empty_lbl = QLabel(
            "No parsers configured.\n\nClick  ＋ Add  to set one up."
        )
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(
            "color: #9ca3af; font-size: 12px; padding: 24px; background: transparent;"
        )
        self._cards_lay.insertWidget(0, self._empty_lbl)

        # Connect log signal so we can update card status
        self.mgr.log.connect(self._on_log)

        self.reload()

    # ── Rebuild ───────────────────────────────────────────────────────────────

    def reload(self):
        # Remove old cards (leave the stretch at the end)
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        parsers = self.mgr.parsers()
        self._empty_lbl.setVisible(len(parsers) == 0)

        for idx, mp in enumerate(parsers):
            card = _ParserCard(idx, mp, self)
            self._cards.append(card)
            self._cards_lay.insertWidget(idx, card)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _run_all_clicked(self):
        for card in self._cards:
            card.set_status("running")
        self.mgr.run_all_now()

    def _add_clicked(self):
        dlg = EmailParserDialog(self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            core_cfg = dlg.get_config()
            name = EmailParserManager._derive_name(core_cfg)
            self.mgr.add_parser(core_cfg, name=name)
            self.reload()

    def _edit_one(self, index: int):
        if 0 <= index < len(self.mgr._parsers):
            mp = self.mgr._parsers[index]
            dlg = EmailParserDialog(self, initial=mp.core)
            if dlg.exec() == dlg.DialogCode.Accepted:
                new_core = dlg.get_config()
                self.mgr.update_parser(index, new_core)
                self.reload()

    def _on_log(self, msg: str):
        """Update card status pill based on log messages."""
        for card in self._cards:
            name = card._mp.name
            if f"[{name}]" not in msg:
                continue
            if "running parser" in msg.lower():
                card.set_status("running")
            elif "complete" in msg.lower():
                card.set_status("ok")
            elif "error" in msg.lower() or "failed" in msg.lower():
                card.set_status("error")
