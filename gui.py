import sys, os, json, sqlite3, tempfile, importlib, time
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
import shutil
import logging, traceback
from logging.handlers import TimedRotatingFileHandler
from collections import deque
from datetime import datetime, timedelta

from PyQt6.QtCore import QSettings

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFileDialog, QLabel, QMessageBox,
    QMenuBar, QToolBar, QSplashScreen, QProgressDialog, QStyle, QStatusBar,
    QPlainTextEdit, QProgressBar, QDockWidget, QPushButton, QLineEdit, QCheckBox, QSizePolicy,
    QDialog, QFormLayout, QComboBox, QDialogButtonBox
)
from PyQt6.QtGui import QAction, QIcon, QPixmap, QGuiApplication, QTextCursor
from PyQt6.QtCore import Qt, QTimer, QRunnable, QThreadPool, QDateTime, QProcess, QFile, QTextStream, QCoreApplication

# === modular email parser imports ===
from utils.Email_parser.email_parser_dialog import EmailParserDialog
from utils.Email_parser.email_parsers_dock import EmailParsersDock, EmailParserManager
from utils.settings_dialog import GuardrailSettings, SettingsDialog

# ui dialogs
from ui.db_viewer_dialog import DBViewerDialog
from ui.header_editor_dialog import HeaderEditorDialog

from utils.time_settings import get_config, set_config, offset_label
from zoneinfo import available_timezones

PROJECT_EXT = "bouyproj.json"
PROJECT_SUBDIRS = ("data", "logs", "tmp", "exports", "state", "alerts")


def _project_root(project_path: str) -> str:
    return os.path.dirname(os.path.abspath(project_path))


def _ensure_project_dirs(base_dir: str) -> dict[str, str]:
    paths = {name: os.path.join(base_dir, name) for name in PROJECT_SUBDIRS}
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


def _asset_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base, "resources", *parts),
        os.path.join(base, "resource", *parts),
        os.path.join(base, *parts),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


# ---------- Splash warmup ----------
class _WarmupTask(QRunnable):
    def run(self):
        os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "mpl-cache"))
        try:
            importlib.import_module("numpy")
            importlib.import_module("pandas")
            importlib.import_module("matplotlib")
        except Exception as e:
            print("Warmup warning:", e)


# ---------- Project model ----------
@dataclass
class ProjectData:
    db_path: Optional[str] = None
    alerts: Dict[str, Any] = None
    charts: Dict[str, Any] = None
    email_parsers: Dict[str, Any] = None
    summary: Dict[str, Any] = None
    time: Dict[str, Any] = None

    def to_json_obj(self, base_dir: str) -> Dict[str, Any]:
        d = asdict(self)
        if self.db_path:
            try:
                d["db_path"] = os.path.relpath(self.db_path, base_dir)
            except Exception:
                pass
        if d.get("time") is None:
            d["time"] = {}
        return d

    @staticmethod
    def from_json_obj(d: Dict[str, Any], base_dir: str) -> "ProjectData":
        db_path = d.get("db_path")
        if db_path and not os.path.isabs(db_path):
            db_path = os.path.normpath(os.path.join(base_dir, db_path))
        ep = d.get("email_parsers")
        if ep is None and "parsers" in d:
            ep = {"parsers": d.get("parsers") or []}
        return ProjectData(
            db_path=db_path,
            alerts=d.get("alerts"),
            charts=d.get("charts"),
            email_parsers=ep or {},
            summary=d.get("summary") or {},
            time=d.get("time") or {"tz_name": "Europe/Dublin", "dayfirst": True, "assume_naive_is_local": True},
        )


# ---------- Stylesheet ----------
def load_stylesheet(file_path):
    file = QFile(file_path)
    if file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text):
        stream = QTextStream(file)
        return stream.readAll()
    return ""


# ---------- Activity bar (dockable log) ----------
class ActivityBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.led = QLabel()
        self.led.setFixedSize(12, 12)
        self.led.setStyleSheet("border-radius:6px; background:#2ecc71; border:1px solid #333;")
        self.led_label = QLabel("Logging: ON")
        self.led_label.setStyleSheet("color:#666;")


        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(120)
        self.spinner.hide()

        self.msg = QLabel("Idle")
        self.msg.setStyleSheet("color: #666;")

        row.addWidget(self.led)
        row.addWidget(self.led_label)
        row.addSpacing(10)
        row.addWidget(self.spinner)
        row.addWidget(self.msg, 1)
        outer.addLayout(row)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        outer.addWidget(self.console, 1)

        self._busy_count = 0
        self._enabled = True

        self._buffer = deque()  # (datetime, line)
        self._window = timedelta(hours=2)

        self._prune_timer = QTimer(self)
        self._prune_timer.timeout.connect(self._prune_old)
        self._prune_timer.start(60 * 1000)


    def _set_led(self, on: bool):
        color = "#2ecc71" if on else "#e74c3c"
        self.led.setStyleSheet(f"border-radius:6px; background:{color}; border:1px solid #333;")
        self.led.setToolTip("Logging ON" if on else "Logging OFF")
        self.led_label.setText("Logging: ON" if on else "Logging: OFF")

    def set_enabled(self, on: bool):
        self._enabled = on
        self._set_led(on)
        if not on:
            self.spinner.hide()

    def start(self, message: str):
        if not self._enabled:
            return
        self._busy_count += 1
        self.spinner.show()
        self.msg.setText(message)
        self.log(f"▶ {message}")

    def stop(self):
        if not self._enabled:
            return
        if self._busy_count > 0:
            self._busy_count -= 1
        if self._busy_count == 0:
            self.spinner.hide()
            self.msg.setText("Idle")
            self.log("■ Done.")

    def log(self, text: str):
        """Append a log line and prune/scroll safely."""
        logger = logging.getLogger("BuoyApp")
        try:
            logger.info(text)
        except Exception:
            pass  # never let logging break the UI

        if not self._enabled:
            return

        ts = datetime.now()
        line = f"[{ts.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
        self._buffer.append((ts, line))
        self.console.appendPlainText(line)
        # prune & scroll — wrapped so any UI hiccup can't crash the app
        try:
            self._prune_old()
        except Exception:
            pass

    # ActivityBar._prune_old(...) — use the PyQt6 enum correctly
    def _prune_old(self):
        """Keep only recent lines and move cursor to the end (PyQt6-safe)."""
        cutoff = datetime.now() - self._window
        changed = False
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
            changed = True

        if changed:
            self.console.setPlainText("\n".join(line for _, line in self._buffer))

        # Move caret to the end using the PyQt6 enum
        try:
            self.console.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass


# ---------- Database Selector ----------
class DatabaseSelector(QWidget):
    def __init__(self, get_path, set_path_and_reload, parent=None):
        super().__init__(parent)
        self.get_path = get_path
        self.set_path_and_reload = set_path_and_reload

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.path_edit = QLineEdit(self.get_path() or "")
        self.path_edit.setReadOnly(True)

        row = QHBoxLayout()
        self.btn_browse = QPushButton("Browse…")
        self.btn_reload = QPushButton("Refresh Tabs (DB only)")
        row.addWidget(self.btn_browse)
        row.addWidget(self.btn_reload)

        layout.addWidget(QLabel("Active database:"))
        layout.addWidget(self.path_edit)
        layout.addLayout(row)
        layout.addStretch(1)

        self.btn_browse.clicked.connect(self._pick_db)
        self.btn_reload.clicked.connect(self._reload)

    def refresh(self):
        self.path_edit.setText(self.get_path() or "")

    def _pick_db(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, "Select SQLite DB File", self.get_path() or "",
            "SQLite DB Files (*.db *.sqlite);;All Files (*)"
        )
        if fn:
            self.set_path_and_reload(fn)
            self.refresh()

    def _reload(self):
        path = self.get_path()
        if path:
            self.set_path_and_reload(path)


# ---------- Toolbar Manager ----------
class ToolbarManagerDialog(QWidget):
    def __init__(self, action_keys: Dict[str, str], selected: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Toolbar Manager")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._keys = list(action_keys.keys())
        self._labels = action_keys
        self._selected = set(selected)
        self._checks: Dict[str, QCheckBox] = {}

        v = QVBoxLayout(self)

        for k in self._keys:
            cb = QCheckBox(self._labels[k])
            cb.setChecked(k in self._selected)
            self._checks[k] = cb
            v.addWidget(cb)

        v.addStretch(1)

        btns = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_cancel = QPushButton("Cancel")
        btns.addStretch(1)
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)
        v.addLayout(btns)

        btn_ok.clicked.connect(self._accept)
        btn_cancel.clicked.connect(self.close)

        self.on_accept = None

    def _accept(self):
        chosen = [k for k, cb in self._checks.items() if cb.isChecked()]
        if self.on_accept:
            self.on_accept(chosen)
        self.close()


# ---------- Time Settings dialog ----------
class TimeSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Time settings")
        f = QFormLayout(self)

        self.cmb_tz = QComboBox(self)
        tzs = sorted([tz for tz in available_timezones() if "/" in tz])
        prefer = ["Europe/Dublin", "Europe/London", "UTC"]
        for p in prefer:
            if p in tzs:
                tzs.remove(p)
        tzs = prefer + tzs
        self.cmb_tz.addItems(tzs)

        cfg = get_config()
        idx = self.cmb_tz.findText(cfg.tz_name)
        if idx >= 0:
            self.cmb_tz.setCurrentIndex(idx)

        self.lbl_offset = QLabel(f"Current offset: {offset_label()}")

        self.chk_dayfirst = QCheckBox("Day-first date strings (dd/mm/yyyy)", self)
        self.chk_dayfirst.setChecked(cfg.dayfirst)
        self.chk_naive_local = QCheckBox("Assume naive timestamps are local (Irish) time", self)
        self.chk_naive_local.setChecked(cfg.assume_naive_is_local)

        f.addRow("Timezone:", self.cmb_tz)
        f.addRow("", self.lbl_offset)
        f.addRow("", self.chk_dayfirst)
        f.addRow("", self.chk_naive_local)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        f.addRow(btns)

        self.cmb_tz.currentIndexChanged.connect(self._update_offset_preview)

    def _update_offset_preview(self):
        self.lbl_offset.setText("Current offset: (will update after Save)")

    def result_to_config(self):
        return {
            "tz_name": self.cmb_tz.currentText().strip(),
            "dayfirst": self.chk_dayfirst.isChecked(),
            "assume_naive_is_local": self.chk_naive_local.isChecked(),
        }


# ========================= Main Window =========================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HydraParse_1.0")
        self.resize(1300, 850)

        self.db_path: Optional[str] = None
        self.project_path: Optional[str] = None
        self.projects_view = None

        self._proc: Optional[QProcess] = None  # (legacy; now unused)

        self._pending_alert_settings: Dict[str, Any] = {}
        self._pending_chart_settings: Dict[str, Any] = {}
        self._pending_summary_settings: Dict[str, Any] = {}

        # Logging state + handler refs
        self._logging_enabled = True
        self._temp_handler: Optional[TimedRotatingFileHandler] = None
        self._project_handler: Optional[TimedRotatingFileHandler] = None

        self.parser_mgr = EmailParserManager(self)
        self.parser_mgr.request_refresh_tabs.connect(self._on_parser_refresh_request)

        # ---- Guardrails settings (single source) ----
        self.guard = GuardrailSettings.load()
        # Backward-compat defaults
        self.guard.auto_restart_on_crash = bool(getattr(self.guard, "auto_restart_on_crash", False))
        self.guard.auto_restart_on_exit  = bool(getattr(self.guard, "auto_restart_on_exit",  False))
        self.guard.restart_outlook_on_outlook_errors = bool(getattr(self.guard, "restart_outlook_on_outlook_errors", True))
        self.guard.restart_app_on_outlook_errors     = bool(getattr(self.guard, "restart_app_on_outlook_errors", True))

        # Apply toggles to parser manager + listen for restart requests
        self.parser_mgr.set_auto_restart_outlook(self.guard.restart_outlook_on_outlook_errors)
        self.parser_mgr.set_restart_app_on_outlook_fail(self.guard.restart_app_on_outlook_errors)
        self.parser_mgr.app_restart_requested.connect(self._on_app_restart_requested)

        self.setCentralWidget(QWidget())

        self._build_docks()
        self._build_menus_and_toolbars()
        self._build_statusbar()

        # track last unhandled exception for closure reporting
        self._had_uncaught_exception = False
        self._last_uncaught_summary = ""
        qt_app = QApplication.instance()
        if qt_app is not None:
            qt_app.aboutToQuit.connect(self._on_about_to_quit)

        self.parser_mgr.log.connect(lambda s: self.activity.log(s))

        self._restarting = False  # prevents double-spawn
        self._install_exception_hook()
        self.activity.log("Application started.")

    # ---------- Logging helpers ----------
    def _setup_temp_logging(self):
        logger = logging.getLogger("BuoyApp")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if self._temp_handler:
            try:
                logger.removeHandler(self._temp_handler)
                self._temp_handler.close()
            except Exception:
                pass
            self._temp_handler = None
        temp_log = os.path.join(tempfile.gettempdir(), "buoyapp.log")
        h = TimedRotatingFileHandler(temp_log, when="midnight", backupCount=14, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        h._is_temp_handler = True
        logger.addHandler(h)
        self._temp_handler = h

    def _attach_project_logging(self, project_dir: str):
        try:
            log_dir = os.path.join(project_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            proj_log_path = os.path.join(log_dir, "app.log")
            logger = logging.getLogger("BuoyApp")

            if self._project_handler:
                try:
                    logger.removeHandler(self._project_handler)
                    self._project_handler.close()
                except Exception:
                    pass
                self._project_handler = None

            # remove any bootstrap handler
            for h in list(logger.handlers):
                if getattr(h, "_boot", False):
                    try:
                        logger.removeHandler(h)
                        h.close()
                    except Exception:
                        pass

            h = TimedRotatingFileHandler(proj_log_path, when="midnight", backupCount=30, encoding="utf-8")
            h._is_project_handler = True
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            logger.addHandler(h)
            self._project_handler = h

            logging.getLogger("BuoyApp").info("Project logging attached: %s", proj_log_path)
        except Exception as e:
            self.activity.log(f"WARN: cannot attach project logging: {e}")

    def _set_logging_enabled(self, enabled: bool):
        self._logging_enabled = enabled
        self.activity.set_enabled(enabled)
        logger = logging.getLogger("BuoyApp")
        logger.disabled = not enabled

        def _remove(h):
            try:
                logger.removeHandler(h)
                h.close()
            except Exception:
                pass

        if not enabled:
            if self._temp_handler:
                _remove(self._temp_handler); self._temp_handler = None
            if self._project_handler:
                _remove(self._project_handler); self._project_handler = None
            self._update_logging_action_ui()
            return

        if self.project_path:
            self._attach_project_logging(os.path.dirname(self.project_path))
        else:
            self._setup_temp_logging()

        self._update_logging_action_ui()

    # ---------- Crash guard / hooks ----------
    def _install_exception_hook(self):
        def _hook(exc_type, exc, tb):
            msg = "".join(traceback.format_exception(exc_type, exc, tb))
            self._had_uncaught_exception = True
            self._last_uncaught_summary = f"{exc_type.__name__}: {exc}"
            logging.getLogger("BuoyApp").error("UNCAUGHT EXCEPTION:\n%s", msg)
            try:
                self.activity.log(f"[ERROR] {self._last_uncaught_summary}")
            except Exception:
                pass

            # Auto-restart on crash if enabled
            try:
                if getattr(self, "guard", None) and self.guard.auto_restart_on_crash:
                    self.activity.log("Auto-restart on crash is ON; relaunching…")
                    self._restart_self("uncaught_exception")
                    QTimer.singleShot(150, QApplication.instance().quit)
            except Exception:
                pass

            try:
                sys.__excepthook__(exc_type, exc, tb)
            except Exception:
                pass

        sys.excepthook = _hook

        def _unraisable(hook_args):
            logging.getLogger("BuoyApp").error("UNRAISABLE EXCEPTION: %s", getattr(hook_args, "err_msg", ""))

        try:
            sys.unraisablehook = _unraisable
        except Exception:
            pass

    # ---------- Unified restart helper ----------

    def _restart_self(self, reason: str, throttle_sec: int = 10):
        """
        Relaunch this program as a detached process, carrying the --project argument.
        Throttled to avoid rapid loops.
        """
        if self._restarting:
            return
        self._restarting = True

        try:
            s = QSettings("BuoyTools", "DBViewer")
            last_ts = s.value("guard/last_restart_ts", "")
            now = QDateTime.currentDateTime()
            if last_ts:
                try:
                    prev = QDateTime.fromString(last_ts, "yyyy-MM-dd HH:mm:ss")
                    if prev.isValid() and prev.secsTo(now) < throttle_sec:
                        self.activity.log("Restart suppressed (throttle).")
                        return
                except Exception:
                    pass
            s.setValue("guard/last_restart_ts", now.toString("yyyy-MM-dd HH:mm:ss"))
            s.setValue("guard/last_restart_reason", reason)

            # Build command
            if getattr(sys, "frozen", False):
                program = sys.executable
                args = sys.argv[1:]
            else:
                program = sys.executable
                script = os.path.abspath(sys.argv[0])
                args = [script, *sys.argv[1:]]

            # Ensure we pass the current project explicitly (absolute path)
            if self.project_path:
                proj_abs = os.path.abspath(self.project_path)
                if "--project" in args:
                    try:
                        i = args.index("--project")
                        if i + 1 < len(args):
                            args[i + 1] = proj_abs
                        else:
                            args += [proj_abs]
                    except ValueError:
                        args += ["--project", proj_abs]
                else:
                    args += ["--project", proj_abs]

            # Optional marker
            if "--restarted" not in args:
                args.append("--restarted")

            ok = QProcess.startDetached(program, args)
            if ok:
                self.activity.log(f"Spawned new instance (reason: {reason}).")
            else:
                self.activity.log("ERROR: startDetached failed; could not restart.")
        except Exception as e:
            self.activity.log(f"ERROR during self-restart: {e}")

    def _on_app_restart_requested(self, reason: str):
        self.activity.log(f"Restart requested by guardrail: {reason}")
        self._restart_self(reason)
        QTimer.singleShot(100, QApplication.instance().quit)

    def _remember_last_project(self):
        try:
            if self.project_path:
                settings = QSettings("BuoyTools", "DBViewer")
                settings.setValue("last_project", self.project_path)
        except Exception:
            pass

    def try_restore_last_project(self, explicit_path: Optional[str] = None):
        path = explicit_path
        if not path:
            settings = QSettings("BuoyTools", "DBViewer")
            path = settings.value("last_project", None)
        if not path:
            return
        if isinstance(path, (list, tuple)):
            path = path[0]
        path = str(path)
        if os.path.isfile(path):
            self._load_project_from_path(path)

    def _maybe_sqlite_optimize(self):
        try:
            if not self.db_path or not os.path.isfile(self.db_path):
                return
            settings = QSettings("BuoyTools", "DBViewer")
            last = settings.value("optimize/last_ts", "")
            now = QDateTime.currentDateTime()
            run_it = True
            if last:
                try:
                    last_dt = QDateTime.fromString(last, "yyyy-MM-dd HH:mm:ss")
                    run_it = last_dt.secsTo(now) > 24 * 3600
                except Exception:
                    run_it = True
            if not run_it:
                return
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA optimize")
            conn.commit()
            conn.close()
            settings.setValue("optimize/last_ts", now.toString("yyyy-MM-dd HH:mm:ss"))
            self.activity.log("SQLite PRAGMA optimize executed.")
        except Exception as e:
            self.activity.log(f"WARN: SQLite optimize skipped: {e}")

    def _apply_project_environment(self, project_dir: str):
        os.environ["BUOY_PROJECT_DIR"] = project_dir
        os.environ["BUOY_TMP_DIR"] = os.path.join(project_dir, "tmp")
        os.environ["BUOY_EXPORTS_DIR"] = os.path.join(project_dir, "exports")
        os.environ["BUOY_STATE_DIR"] = os.path.join(project_dir, "state")
        os.environ["BUOY_ALERTS_DIR"] = os.path.join(project_dir, "alerts")

    # -------------- Docks --------------
    def _build_docks(self):
        self.tabs_dock = QDockWidget("Projects", self)
        self.tabs_dock.setObjectName("DataTabsDock")
        self.tabs_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        placeholder = QWidget()
        pl = QVBoxLayout(placeholder)
        pl.setContentsMargins(6, 6, 6, 6)
        lbl = QLabel("Load a database to see projects")
        lbl.setStyleSheet("color:#666;")
        pl.addWidget(lbl, 1, alignment=Qt.AlignmentFlag.AlignTop)
        self.tabs_dock.setWidget(placeholder)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tabs_dock)

        self.db_selector_widget = DatabaseSelector(
            get_path=lambda: self.db_path or "",
            set_path_and_reload=self._set_db_and_reload
        )
        self.dbsel_dock = QDockWidget("Database Selector", self)
        self.dbsel_dock.setObjectName("DatabaseSelectorDock")
        self.dbsel_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.dbsel_dock.setWidget(self.db_selector_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dbsel_dock)
        self.tabifyDockWidget(self.dbsel_dock, self.tabs_dock)
        self.dbsel_dock.raise_()

        self.parsers_dock = QDockWidget("Email Parsers", self)
        self.parsers_dock.setObjectName("EmailParsersDock")
        self.parsers_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        self.parsers_dock.setWidget(EmailParsersDock(self.parser_mgr, self))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.parsers_dock)

        self.activity = ActivityBar(self)
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("LogDock")
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.log_dock.setWidget(self.activity)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)

        for d in (self.tabs_dock, self.dbsel_dock, self.parsers_dock, self.log_dock):
            d.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable |
                QDockWidget.DockWidgetFeature.DockWidgetFloatable |
                QDockWidget.DockWidgetFeature.DockWidgetClosable
            )
            d.setMinimumSize(80, 60)
            w = d.widget()
            if w is not None:
                w.setMinimumSize(0, 0)
                w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        self.setDockOptions(self.dockOptions() | QMainWindow.DockOption.AllowTabbedDocks | QMainWindow.DockOption.AnimatedDocks)

        try:
            w = self.parsers_dock.widget()
            table = getattr(w, "table", None) or w
            if hasattr(table, "horizontalHeader"):
                table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
                table.setMinimumWidth(0)
                table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        except Exception:
            pass

    # -------------- Menus + toolbar --------------
    def _build_menus_and_toolbars(self):
        mb: QMenuBar = self.menuBar()

        # Create the toolbar first
        self.tb_main = QToolBar("Main", self)
        self.addToolBar(self.tb_main)

        # ----- Project menu -----
        m_proj = mb.addMenu("&Project")
        act_new = QAction("&New Project", self);
        act_new.triggered.connect(self.new_project)
        act_open = QAction("&Open Project…", self);
        act_open.triggered.connect(self.open_project)
        act_save = QAction("&Save Project", self);
        act_save.triggered.connect(self.save_project)
        act_save_as = QAction("Save Project &As…", self);
        act_save_as.triggered.connect(self.save_project_as)
        m_proj.addActions([act_new, act_open, act_save, act_save_as])

        act_launch_dash = QAction("Launch Dashboard (Streamlit)…", self)
        act_launch_dash.triggered.connect(self._launch_streamlit_dashboard)
        m_proj.addAction(act_launch_dash)

        # User/Time settings
        m_proj.addSeparator()
        act_user_settings = QAction("User Settings…", self)
        act_user_settings.triggered.connect(self._open_user_settings)
        m_proj.addAction(act_user_settings)

        act_time = QAction("Time settings…", self)

        def _open_time():
            dlg = TimeSettingsDialog(self)
            if dlg.exec() == dlg.DialogCode.Accepted:
                cfg = dlg.result_to_config()
                set_config(cfg["tz_name"], cfg["dayfirst"], cfg["assume_naive_is_local"])
                if self.projects_view:
                    self.projects_view.reload(self.db_path)
                QMessageBox.information(self, "Time settings",
                                        f"Applied timezone: {cfg['tz_name']}\nOffset: {offset_label()}")

        act_time.triggered.connect(_open_time)
        m_proj.addAction(act_time)

        # ----- Data menu -----
        m_data = mb.addMenu("&Data")
        act_load_sql = QAction("Load SQL.db…", self)
        act_load_sql.triggered.connect(self.load_db)
        m_data.addAction(act_load_sql)

        act_db_viewer = QAction("Open DB Viewer…", self)
        act_db_viewer.triggered.connect(self._open_db_viewer)
        act_edit_headers = QAction("Rename Columns for Current Tab…", self)
        act_edit_headers.triggered.connect(self._edit_headers_for_current_tab)
        m_data.addActions([act_db_viewer, act_edit_headers])

        act_refresh_tabs = QAction("Refresh Tabs (DB only)", self)
        act_refresh_tabs.setShortcut("F5")
        act_refresh_tabs.triggered.connect(self.refresh_tabs_only)
        m_data.addAction(act_refresh_tabs)

        # Keep a dict of common actions if you use it elsewhere
        self.actions = {
            "new": act_new,
            "open": act_open,
            "save": act_save,
            "save_as": act_save_as,
            "refresh_tabs": act_refresh_tabs,
            "db_viewer": act_db_viewer,
            "edit_headers": act_edit_headers,
        }

        # ----- Logging toggle shown in View menu and (optionally) toolbar -----
        self.act_logging = QAction("Logging: ON", self, checkable=True)
        self.act_logging.setChecked(True)
        self.act_logging.toggled.connect(self._set_logging_enabled)
        self._update_logging_action_ui()

        # Toolbar actions map (buttons that appear on the toolbar)
        icon_new = QIcon.fromTheme("document-new", self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        icon_open = QIcon.fromTheme("document-open",
                                    self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        icon_save = QIcon.fromTheme("document-save",
                                    self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        icon_save_as = QIcon.fromTheme("document-save-as",
                                       self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        icon_refresh = QIcon.fromTheme("view-refresh",
                                       self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))

        self.action_toolbar_map = {
            "new": (icon_new, "New", self.new_project),
            "open": (icon_open, "Open", self.open_project),
            "save": (icon_save, "Save", self.save_project),
            "save_as": (icon_save_as, "Save As", self.save_project_as),
            "refresh_tabs": (icon_refresh, "Refresh Tabs", self.refresh_tabs_only),
            "logging_toggle": self.act_logging,
        }

        # Build toolbar from preferences (adds right-aligned project label inside)
        self._rebuild_toolbar_from_prefs()

        # ----- View menu -----
        m_view = mb.addMenu("&View")
        m_view.addAction(self.act_logging)

        m_view.addSeparator()
        act_tb_mgr = QAction("Toolbar Manager…", self)
        act_tb_mgr.triggered.connect(self._open_toolbar_manager)
        m_view.addAction(act_tb_mgr)

        act_full = QAction("&Toggle Full Screen", self, checkable=True)
        act_full.triggered.connect(self._toggle_fullscreen)
        m_view.addAction(act_full)

        m_view.addSeparator()
        m_view.addAction(self.tabs_dock.toggleViewAction())
        m_view.addAction(self.dbsel_dock.toggleViewAction())
        m_view.addAction(self.parsers_dock.toggleViewAction())
        m_view.addAction(self.log_dock.toggleViewAction())

        # ----- File menu -----
        m_file = mb.addMenu("&File")
        act_exit = QAction("E&xit", self);
        act_exit.triggered.connect(self.close)
        m_file.addAction(act_exit)

    def _update_project_banner(self, path: Optional[str] = None):
        name = "—"
        if path:
            name = os.path.basename(path)
        elif self.project_path:
            name = os.path.basename(self.project_path)
        # Window title + toolbar label
        self.setWindowTitle(f"HydraParse — {name}")
        if hasattr(self, "lbl_project"):
            self.lbl_project.setText(f"Project: {name}")

    def _update_logging_action_ui(self):
        if self._logging_enabled:
            self.act_logging.setText("Logging: ON")
            self.act_logging.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
            self.act_logging.setToolTip("Turn logging OFF")
        else:
            self.act_logging.setText("Logging: OFF")
            self.act_logging.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
            self.act_logging.setToolTip("Turn logging ON")

    def _open_db_viewer(self):
        if not self.db_path:
            QMessageBox.information(self, "Database", "Please load or select a database first.")
            return
        DBViewerDialog(self.db_path, self).exec()

    def _edit_headers_for_current_tab(self):
        if not self.db_path:
            QMessageBox.information(self, "Database", "Please load or select a database first.")
            return
        if not self.projects_view:
            QMessageBox.information(self, "Rename Columns", "No project is visible.")
            return

        table = self.projects_view.current_table_name()
        if not table:
            QMessageBox.information(self, "Rename Columns", "Select a specific project (not Summary).")
            return

        dlg = HeaderEditorDialog(self.db_path, self, preselect_table=table)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self.activity.log(f"Columns renamed for table: {table}")

    def _open_toolbar_manager(self):
        labels = {
            "new": "New Project",
            "open": "Open Project",
            "save": "Save Project",
            "save_as": "Save Project As",
            "refresh_tabs": "Refresh Tabs (DB only)",
            "logging_toggle": "Enable/Disable Logging Button",
        }
        settings = QSettings("BuoyTools", "DBViewer")
        current = settings.value("toolbar/items", None)
        if not current:
            current = self._default_toolbar_items()

        dlg = ToolbarManagerDialog(labels, current, self)

        def _apply(chosen_keys: list[str]):
            settings.setValue("toolbar/items", chosen_keys)
            self._rebuild_toolbar_from_prefs()
            self.activity.log(f"Toolbar updated: {', '.join(chosen_keys)}")

        dlg.on_accept = _apply
        dlg.show()

    def _default_toolbar_items(self):
        return ["new", "open", "save", "save_as", "refresh_tabs", "logging_toggle"]

    def _rebuild_toolbar_from_prefs(self):
        self.tb_main.clear()
        settings = QSettings("BuoyTools", "DBViewer")
        items = settings.value("toolbar/items", None) or self._default_toolbar_items()
        items = [k for k in items if k in self.action_toolbar_map]

        for key in items:
            item = self.action_toolbar_map[key]
            if isinstance(item, QAction):
                self.tb_main.addAction(item)
            else:
                icon, text, slot = item
                self.tb_main.addAction(icon, text, slot)

        # right-align: spacer + project label
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.tb_main.addWidget(spacer)

        if not hasattr(self, "lbl_project"):
            self.lbl_project = QLabel("Project: —")
            self.lbl_project.setStyleSheet("font-weight: 600; padding: 0 8px;")
        self.tb_main.addWidget(self.lbl_project)

        self._update_project_banner()  # uses self.project_path when path is None

    def _build_statusbar(self):
        sb = QStatusBar(self)
        self.setStatusBar(sb)
        self.status_db = QLabel("DB: (none)")
        self.status_last = QLabel("Last refreshed: —")
        self.status_db.setStyleSheet("color:#666;")
        self.status_last.setStyleSheet("color:#666;")
        sb.addPermanentWidget(self.status_db)
        sb.addPermanentWidget(self.status_last)

    # -------------- Simple DB-only refresh --------------
    def refresh_tabs_only(self):
        if not self.db_path:
            QMessageBox.information(self, "No DB Selected",
                                    "Please select and import a .db file first (Data → Load SQL.db…).")
            return

        # Keep any in-memory edits safe
        self._stash_alerts_before_refresh()

        if self.projects_view:
            # ⚡ Light refresh path: no page rebuild, just data reload
            self.activity.start("Refreshing data…")
            try:
                self.projects_view.refresh_data_light()
            except Exception:
                # Fall back to the full rebuild if anything goes sideways
                self.activity.log("Light refresh failed; rebuilding tabs.")
                self._load_db_internal(show_progress=True)
            finally:
                now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
                self.status_last.setText(f"Last refreshed: {now}")
                self._maybe_sqlite_optimize()
                self.activity.stop()
        else:
            # First run or no view yet → full build
            self.activity.start("Refreshing tabs from database…")
            self._load_db_internal(show_progress=True)
            now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
            self.status_last.setText(f"Last refreshed: {now}")
            self._maybe_sqlite_optimize()
            self.activity.stop()

    # -------------- Alerts save/load helpers --------------
    def _save_alerts_files(self, base_dir: str, alerts: Dict[str, Any]):
        try:
            alerts_dir = os.path.join(base_dir, "alerts")
            os.makedirs(alerts_dir, exist_ok=True)
            path = os.path.join(alerts_dir, "alerts.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(alerts or {}, f, indent=2)
            self.activity.log(f"Alerts saved to {path}")
        except Exception as e:
            self.activity.log(f"WARN: could not save alerts.json: {e}")

    def _load_alerts_files(self, base_dir: str) -> Dict[str, Any]:
        try:
            path = os.path.join(base_dir, "alerts", "alerts.json")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.activity.log(f"Loaded alerts from {path}")
                return data or {}
        except Exception as e:
            self.activity.log(f"WARN: could not read alerts.json: {e}")
        return {}

    # -------------- Project I/O --------------
    def new_project(self):
        if not self._confirm_discard():
            return
        self.project_path = None
        self.db_path = None
        self._pending_alert_settings = {}
        self._pending_chart_settings = {}
        self._pending_summary_settings = {}
        self.parser_mgr.from_json({})
        self._update_status_db()
        self.db_selector_widget.refresh()

        placeholder = QWidget()
        pl = QVBoxLayout(placeholder)
        pl.setContentsMargins(6, 6, 6, 6)
        lbl = QLabel("Load a database to see projects")
        lbl.setStyleSheet("color:#666;")
        pl.addWidget(lbl, 1, alignment=Qt.AlignmentFlag.AlignTop)
        self.tabs_dock.setWidget(placeholder)
        self.projects_view = None

        self.activity.log("New project created.")
        self._update_project_banner(None)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", "", f"Project (*.{PROJECT_EXT});;All Files (*)")
        if not path:
            return
        self._load_project_from_path(path)

    def _load_project_from_path(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            proj = ProjectData.from_json_obj(obj, os.path.dirname(path))
            self.project_path = path
            proj_dir = _project_root(path)
            _ensure_project_dirs(proj_dir)
            self._attach_project_logging(proj_dir)
            self._apply_project_environment(proj_dir)
            self._remember_last_project()

            file_alerts = self._load_alerts_files(proj_dir)
            self._pending_alert_settings = dict(file_alerts or (proj.alerts or {}))
            self._pending_chart_settings = dict(proj.charts or {})
            self._pending_summary_settings = dict(proj.summary or {})
            self.parser_mgr.from_json(proj.email_parsers or {})

            self.db_path = proj.db_path
            self.activity.log(f"Project DB (resolved from JSON): {repr(self.db_path)}")

            t = proj.time or {}
            set_config(
                tz_name=t.get("tz_name", "Europe/Dublin"),
                dayfirst=t.get("dayfirst", True),
                assume_naive_is_local=t.get("assume_naive_is_local", True),
            )

            if hasattr(self, "parsers_dock") and self.parsers_dock.widget():
                w = self.parsers_dock.widget()
                if hasattr(w, "reload"):
                    w.reload()

            self._update_status_db()
            self.db_selector_widget.refresh()
            self.activity.log(f"Opened project: {os.path.basename(path)}")

            if self.db_path:
                self._load_db_internal(show_progress=True)
                self._maybe_sqlite_optimize()

        except Exception as e:
            QMessageBox.critical(self, "Open Project", f"Failed to open project:\n{e}")
            self.activity.log(f"ERROR opening project: {e}")

        self._update_project_banner(path)

    def save_project(self):
        if self.project_path is None:
            return self.save_project_as()
        self._save_project_to_path(self.project_path)

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", "", f"Project (*.{PROJECT_EXT});;All Files (*)")
        if not path:
            return
        if not path.endswith(PROJECT_EXT):
            path += f".{PROJECT_EXT}"
        self._save_project_to_path(path)
        self.project_path = path
        self._update_project_banner(path)

    def _save_project_to_path(self, path):
        alerts = self._collect_alert_settings_from_tabs()
        charts = self._collect_chart_settings_from_tabs()
        summary = self._collect_summary_settings()
        email_parsers_payload = self.parser_mgr.to_json()

        tc = get_config()
        time_payload = {"tz_name": tc.tz_name, "dayfirst": tc.dayfirst, "assume_naive_is_local": tc.assume_naive_is_local}

        proj = ProjectData(
            db_path=self.db_path,
            alerts=alerts,
            charts=charts,
            email_parsers=email_parsers_payload,
            summary=summary,
            time=time_payload,
        )
        base = os.path.dirname(path)
        try:
            _ensure_project_dirs(base)
            self._save_alerts_files(base, alerts)

            with open(path, "w", encoding="utf-8") as f:
                json.dump(proj.to_json_obj(base), f, indent=2)

            self.statusBar().showMessage(f"Saved project: {os.path.basename(path)}", 3000)
            self._attach_project_logging(base)
            self._apply_project_environment(base)
            self._remember_last_project()
        except Exception as e:
            QMessageBox.critical(self, "Save Project", f"Failed to save project:\n{e}")

    # -------------- Data loading --------------
    def load_db(self):
        filename = self.db_path
        if not filename:
            filename, _ = QFileDialog.getOpenFileName(
                self, "Select SQLite DB File", "",
                "SQLite DB Files (*.db *.sqlite);;All Files (*)"
            )
            if not filename:
                return
            self.db_path = filename
        self._update_status_db()
        self.db_selector_widget.refresh()
        self._load_db_internal(show_progress=True)

    def _set_db_and_reload(self, path: str):
        if not path:
            return
        self.db_path = path
        self._update_status_db()
        self._load_db_internal(show_progress=True)

    def _probe_sqlite_ready(self, abs_path: str, timeout_s: float = 6.0) -> None:
        """
        Wait briefly until the sqlite file is actually readable.
        Handles the self-restart race where the previous process still has handles.
        Raises the last OSError if the file never becomes ready.
        """
        deadline = time.time() + float(timeout_s)
        last_err = None
        while time.time() < deadline:
            try:
                # Just touch the file and read the SQLite magic if present
                with open(abs_path, "rb") as f:
                    magic = f.read(16)
                # 0-byte files are valid sqlite *targets* too (writer may create tables later)
                if not magic or magic.startswith(b"SQLite format 3"):
                    return
            except OSError as e:
                last_err = e
            time.sleep(0.25)
        raise last_err or OSError(22, f"Database not ready: {abs_path}")

    def _load_db_internal(self, show_progress=False):
        try:
            from Create_tabs import ProjectsView
        except Exception:
            from Create_tabs import ProjectsView

        if not self.db_path:
            QMessageBox.information(self, "Database",
                                    "Please choose a database (left: Database Selector) or Data → Load SQL.db…")
            return

        self.activity.start("Loading database tables…")

        progress = None
        tables = []
        if show_progress:
            progress = QProgressDialog("Reading table list…", None, 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setCancelButton(None)
            progress.show()
            QApplication.processEvents()

        try:
            # --- robust path check & logging before connecting ---
            path = self.db_path
            self.activity.log(f"DB open (raw): {repr(path)}")

            if not path or not isinstance(path, str):
                raise ValueError("Empty database path")

            if os.path.isdir(path):
                raise OSError(22, f"Invalid argument: path is a directory: {path}")

            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                raise FileNotFoundError(f"Database does not exist: {abs_path}")

            if sys.platform.startswith("win"):
                # Only apply extended-length prefix if truly needed; sqlite on Windows can be picky
                if len(abs_path) >= 240 and not abs_path.startswith("\\\\?\\"):
                    abs_path = "\\\\?\\" + abs_path

            # Light stat log to aid future forensics
            try:
                st = os.stat(abs_path)
                mt = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                self.activity.log(f"DB open (resolved): {repr(abs_path)} | size={st.st_size}B mtime={mt}")
            except Exception:
                self.activity.log(f"DB open (resolved): {repr(abs_path)}")

            # --- PROBE & RETRY: tolerate restart races and transient Windows errors ---
            self._probe_sqlite_ready(abs_path, timeout_s=6.0)

            conn = None
            last_err = None
            # Small exponential backoff to dodge locked/invalid-arg races during self-restart
            for attempt in range(1, 6):
                try:
                    conn = sqlite3.connect(abs_path, timeout=10)
                    break
                except (sqlite3.OperationalError, OSError) as e:
                    last_err = e
                    msg = str(e).lower()
                    # Retry on common transient cases: invalid arg (Win), locked, sharing violations
                    if (isinstance(e, OSError) and getattr(e, "errno", None) == 22) or \
                            ("locked" in msg) or ("busy" in msg) or ("unable to open" in msg):
                        time.sleep(0.2 * attempt)
                        continue
                    raise
            if conn is None:
                raise last_err or RuntimeError("SQLite connect failed")

            cur = conn.cursor()
            cur.execute("""
                SELECT name
                FROM sqlite_schema
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
            tables = [r[0] for r in cur.fetchall()]
            conn.close()

            if progress:
                progress.setRange(0, max(1, len(tables)))
                progress.setValue(0)
                progress.setLabelText("Building Projects view…")
                QApplication.processEvents()

            # Propagate the normalized/verified path downstream so every consumer uses the same string
            self.db_path = abs_path
            self._update_status_db()
            self.db_selector_widget.refresh()

            pv = ProjectsView(self.db_path)
            self.projects_view = pv
            self.tabs_dock.setWidget(pv)

            # Apply persisted Summary/Alerts/Charts settings (unchanged logic)
            if self._pending_summary_settings:
                self._apply_summary_settings(self._pending_summary_settings)
                self._pending_summary_settings = {}

            for i, (tname, page) in enumerate(pv.iter_tables(), start=1):
                payload_alerts = (self._pending_alert_settings or {}).get(tname)
                if payload_alerts:
                    alerts_tab = getattr(page, "alerts_tab", None)
                    if alerts_tab and hasattr(alerts_tab, "import_settings"):
                        try:
                            alerts_tab.import_settings(payload_alerts)
                            self._pending_alert_settings.pop(tname, None)
                        except Exception as e:
                            self.activity.log(f"WARN applying alerts to {tname}: {e}")
                if progress:
                    progress.setValue(i)
                    QApplication.processEvents()

            for tname, page in pv.iter_tables():
                payload_charts = (self._pending_chart_settings or {}).get(tname)
                if payload_charts and hasattr(page, "import_charts_settings"):
                    try:
                        page.import_charts_settings(payload_charts)
                        self._pending_chart_settings.pop(tname, None)
                    except Exception as e:
                        self.activity.log(f"WARN applying charts to {tname}: {e}")

            if not tables:
                QMessageBox.warning(self, "No Tables", "No tables found in the database.")
                self.activity.log("No tables found.")
                return

        except Exception as e:
            # include a traceback snippet to make the next mystery less mysterious
            tb = traceback.format_exc(limit=2)
            try:
                self.activity.log(
                    f"ERROR loading tables: {e} | db_path={repr(getattr(self, 'db_path', None))} | where={tb.strip()}")
            finally:
                QMessageBox.critical(self, "Error", f"Failed to load tables:\n{e}")
        finally:
            if progress:
                progress.close()
            self.activity.stop()

    # -------------- Email parser integration (dialog kept for dock use) --------------
    def _open_email_parser_dialog(self):
        base_dir = os.path.dirname(self.project_path) if self.project_path else os.path.dirname(__file__)
        dlg = EmailParserDialog(self, default_db_dir=os.path.join(base_dir, "Logger_Data"))
        if dlg.exec() == dlg.DialogCode.Accepted:
            cfg = dlg.get_config()
            proj_dir = _project_root(self.project_path) if self.project_path else os.path.dirname(__file__)
            paths = _ensure_project_dirs(proj_dir)
            cfg.state_dir = paths["state"]
            if (getattr(cfg, "output_format", "db") in ("csv", "txt")) and not getattr(cfg, "output_dir", ""):
                cfg.output_dir = paths["exports"]

            from utils.Email_parser.email_parsers_dock import EmailParserManager as _EPM
            name = _EPM._derive_name(cfg)

            if cfg.output_format == "db":
                if not cfg.db_path:
                    QMessageBox.warning(self, "Email Parser", "Please choose a DB path for this parser.")
                    return
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)), exist_ok=True)
                except Exception as e:
                    QMessageBox.critical(self, "Email Parser", f"Cannot create DB folder:\n{e}")
                    return
            if not (cfg.folder_paths or []):
                QMessageBox.warning(self, "Email Parser", "Please choose at least one Outlook folder.")
                return

            self.parser_mgr.add_parser(cfg, name=name)
            self.activity.log(f"Added parser → {cfg.output_format.upper()}: {cfg.db_path if cfg.output_format=='db' else cfg.output_dir} | mailbox: {cfg.mailbox}")

            dock = getattr(self, "parsers_dock", None)
            if dock:
                widget = dock.widget()
                if hasattr(widget, "reload"):
                    widget.reload()

    def _on_parser_refresh_request(self, db_path: str, force: bool):
        if force:
            return
        self.refresh_tabs_only()

    # -------------- Streamlit launcher --------------
    def _launch_streamlit_dashboard(self):
        import shutil
        from PyQt6.QtCore import QProcessEnvironment

        streamlit_exe = shutil.which("streamlit")
        if not streamlit_exe:
            QMessageBox.critical(self, "Streamlit", "Streamlit is not installed or not on PATH.\n\nInstall with:  pip install streamlit plotly pydeck")
            return

        base_dir = os.path.dirname(__file__)
        script_path = os.path.join(base_dir, "utils", "streamlit_dashboard", "streamlit_app.py")
        if not os.path.exists(script_path):
            QMessageBox.critical(self, "Dashboard", f"Dashboard script not found:\n{script_path}")
            return

        args = ["run", script_path, "--"]
        if self.db_path:
            args += ["--db", os.path.abspath(self.db_path)]
        if self.project_path:
            args += ["--project", os.path.abspath(self.project_path)]

        self.activity.log(f"Launching dashboard: {streamlit_exe} {' '.join(args)}")

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        qenv = QProcessEnvironment.systemEnvironment()
        if self.db_path:
            qenv.insert("BUOY_DB", os.path.abspath(self.db_path))
        if self.project_path:
            qenv.insert("BUOY_PROJECT", os.path.abspath(self.project_path))
        proc.setProcessEnvironment(qenv)

        proc.readyReadStandardOutput.connect(lambda:
            self.activity.log(bytes(proc.readAllStandardOutput()).decode("utf-8", errors="ignore").rstrip())
        )
        proc.start(streamlit_exe, args)
        if not proc.waitForStarted(4000):
            QMessageBox.critical(self, "Dashboard", "Failed to start Streamlit.")

    # -------------- Helpers --------------
    def _open_user_settings(self):
        """Open the guardrail/user settings dialog and apply/save changes."""
        try:
            dlg = SettingsDialog(self.guard, self)
            if dlg.exec() == dlg.DialogCode.Accepted:
                self.guard = dlg.result()
                # ensure defaults exist
                self.guard.auto_restart_on_crash = bool(getattr(self.guard, "auto_restart_on_crash", False))
                self.guard.auto_restart_on_exit  = bool(getattr(self.guard, "auto_restart_on_exit",  False))
                self.guard.restart_outlook_on_outlook_errors = bool(getattr(self.guard, "restart_outlook_on_outlook_errors", True))
                self.guard.restart_app_on_outlook_errors     = bool(getattr(self.guard, "restart_app_on_outlook_errors", True))
                self.guard.save()
                # push to manager
                self.parser_mgr.set_auto_restart_outlook(self.guard.restart_outlook_on_outlook_errors)
                self.parser_mgr.set_restart_app_on_outlook_fail(self.guard.restart_app_on_outlook_errors)
                self.activity.log(
                    "Settings saved: "
                    f"restart-on-exit={'ON' if self.guard.auto_restart_on_exit else 'OFF'}, "
                    f"restart-on-crash={'ON' if self.guard.auto_restart_on_crash else 'OFF'}, "
                    f"outlook-restart={'ON' if self.guard.restart_outlook_on_outlook_errors else 'OFF'}, "
                    f"restart-on-persistent-outlook={'ON' if self.guard.restart_app_on_outlook_errors else 'OFF'}"
                )
        except Exception as e:
            QMessageBox.warning(self, "Settings", f"Failed to open/apply settings:\n{e}")

    def _on_about_to_quit(self):
        try:
            if self._had_uncaught_exception:
                self.activity.log(f"About to quit (previous error): {self._last_uncaught_summary}")
            else:
                self.activity.log("About to quit (orderly).")
        except Exception:
            pass

    def _collect_summary_settings(self) -> Dict[str, Any]:
        try:
            if self.projects_view and getattr(self.projects_view, "summary_page", None):
                sp = self.projects_view.summary_page
                if hasattr(sp, "export_state"):
                    return sp.export_state() or {}
        except Exception:
            pass
        return {}

    def _apply_summary_settings(self, payload: Optional[Dict[str, Any]]):
        if not payload:
            return
        try:
            if self.projects_view and getattr(self.projects_view, "summary_page", None):
                sp = self.projects_view.summary_page
                if hasattr(sp, "import_state"):
                    sp.import_state(payload)
        except Exception:
            pass

    def _stash_alerts_before_refresh(self):
        current_alerts = self._collect_alert_settings_from_tabs()
        self._pending_alert_settings = {**(self._pending_alert_settings or {}), **current_alerts}
        current_charts = self._collect_chart_settings_from_tabs()
        self._pending_chart_settings = {**(self._pending_chart_settings or {}), **current_charts}
        current_summary = self._collect_summary_settings()
        if current_summary:
            self._pending_summary_settings = current_summary

    def _collect_alert_settings_from_tabs(self) -> Dict[str, Any]:
        result = {}
        if not self.projects_view:
            return result
        for tname, inner in self.projects_view.iter_tables():
            alerts_tab = getattr(inner, "alerts_tab", None)
            if alerts_tab and hasattr(alerts_tab, "export_settings"):
                try:
                    result[tname] = alerts_tab.export_settings()
                except Exception as e:
                    self.activity.log(f"WARN: exporting alerts for {tname}: {e}")
        return result

    def _collect_chart_settings_from_tabs(self) -> Dict[str, Any]:
        result = {}
        if not self.projects_view:
            return result
        for tname, page in self.projects_view.iter_tables():
            if hasattr(page, "export_charts_settings"):
                try:
                    result[tname] = page.export_charts_settings()
                except Exception:
                    pass
        return result

    def _apply_chart_settings_to_tabs(self, data: Optional[Dict[str, Any]]):
        if not data or not self.projects_view:
            return
        for tname, page in self.projects_view.iter_tables():
            payload = data.get(tname)
            if payload and hasattr(page, "import_charts_settings"):
                try:
                    page.import_charts_settings(payload)
                except Exception:
                    pass

    def _update_status_db(self):
        self.status_db.setText(f"DB: {os.path.basename(self.db_path) if self.db_path else '(none)'}")

    def _toggle_fullscreen(self, checked: bool):
        if checked or not self.isFullScreen():
            self.showFullScreen()
        else:
            self.showNormal()

    def _confirm_discard(self) -> bool:
        return True

    def closeEvent(self, event):
        try:
            reason = "user" if event.spontaneous() else "programmatic"
            if self._had_uncaught_exception and self._last_uncaught_summary:
                self.activity.log(f"Application closing after error ({reason}): {self._last_uncaught_summary}")
            else:
                self.activity.log(f"Application closing ({reason}).")

            # Auto-restart on user close
            if reason == "user" and getattr(self, "guard", None) and self.guard.auto_restart_on_exit:
                self.activity.log("Restart when I close the app: ON → relaunching…")
                self._restart_self("user_exit")

            # flush any handlers
            logger = logging.getLogger("BuoyApp")
            for h in list(logger.handlers):
                try:
                    h.flush()
                except Exception:
                    pass
        except Exception:
            pass
        super().closeEvent(event)


# ---- bootstrap temp logging early so startup hits the file
def _bootstrap_temp_logging():
    try:
        logger = logging.getLogger("BuoyApp")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not any(getattr(h, "_boot", False) for h in logger.handlers):
            temp_dir = tempfile.gettempdir()
            os.makedirs(temp_dir, exist_ok=True)
            path = os.path.join(temp_dir, "buoyapp.log")
            h = TimedRotatingFileHandler(path, when="midnight", backupCount=14, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            h._boot = True
            logger.addHandler(h)
            logger.info("Bootstrap temp logging attached at %s", path)
    except Exception:
        pass


# ========================= App bootstrap =========================
def main():
    QCoreApplication.setOrganizationName("BuoyTools")
    QCoreApplication.setApplicationName("DBViewer")

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BuoyTools.DBViewer")
        except Exception:
            pass

    _bootstrap_temp_logging()

    app = QApplication(sys.argv)

    icon_path = _asset_path("icons", "app_icon.ico" if sys.platform.startswith("win") else "app_icon.png")
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
    else:
        app_icon = QIcon()

    splash_pix_path = _asset_path("splash", "splash.png")
    pix = QPixmap(splash_pix_path)
    if pix.isNull():
        pix = QPixmap(50, 50); pix.fill(Qt.GlobalColor.black)

    splash = QSplashScreen(pix)
    splash.showMessage("Starting…", Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignBottom, Qt.GlobalColor.white)
    splash.show()
    app.processEvents()

    window = MainWindow()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()

    # Load stylesheet if present
    qss = load_stylesheet(_asset_path("style.qss"))
    QTimer.singleShot(0, lambda: app.setStyleSheet(qss))

    QTimer.singleShot(0, lambda: QThreadPool.globalInstance().start(_WarmupTask()))
    splash.finish(window)

    explicit_project = None
    try:
        argv = sys.argv[1:]
        if "--project" in argv:
            idx = argv.index("--project")
            if idx + 1 < len(argv):
                explicit_project = argv[idx + 1]
    except Exception:
        pass

    # If we just auto-restarted, give the OS a beat to release file handles
    restore_delay_ms = 0
    try:
        if "--restarted" in sys.argv:
            restore_delay_ms = 800
    except Exception:
        pass
    QTimer.singleShot(restore_delay_ms, lambda: window.try_restore_last_project(explicit_project))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
