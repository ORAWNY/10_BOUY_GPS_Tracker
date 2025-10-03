# utils/settings_dialog.py
from __future__ import annotations
from dataclasses import dataclass
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QLabel,
    QDialogButtonBox, QWidget, QGroupBox
)

ORG = "BuoyTools"
APP = "DBViewer"


def _get_bool(settings: QSettings, key: str, default: bool) -> bool:
    val = settings.value(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(val)


@dataclass
class GuardrailSettings:
    # App lifecycle
    auto_restart_on_crash: bool = True
    auto_restart_on_exit:  bool = False  # <-- NEW checkbox
    # Email/Outlook resilience
    restart_outlook_on_outlook_errors: bool = True
    restart_app_on_outlook_errors:     bool = True

    # keys in QSettings
    _K_CRASH   = "guard/auto_restart_on_crash"
    _K_EXIT    = "guard/auto_restart_on_exit"
    _K_RO_RE   = "guard/restart_outlook_on_outlook_errors"
    _K_RA_ROE  = "guard/restart_app_on_outlook_errors"

    @classmethod
    def load(cls) -> "GuardrailSettings":
        s = QSettings(ORG, APP)
        return cls(
            auto_restart_on_crash=_get_bool(s, cls._K_CRASH, True),
            auto_restart_on_exit=_get_bool(s, cls._K_EXIT, False),
            restart_outlook_on_outlook_errors=_get_bool(s, cls._K_RO_RE, True),
            restart_app_on_outlook_errors=_get_bool(s, cls._K_RA_ROE, True),
        )

    def save(self) -> None:
        s = QSettings(ORG, APP)
        s.setValue(self._K_CRASH, self.auto_restart_on_crash)
        s.setValue(self._K_EXIT, self.auto_restart_on_exit)
        s.setValue(self._K_RO_RE, self.restart_outlook_on_outlook_errors)
        s.setValue(self._K_RA_ROE, self.restart_app_on_outlook_errors)


class SettingsDialog(QDialog):
    """
    Pop-out user settings for guardrails.
    - Restart when I close the app (auto_restart_on_exit)  <-- NEW
    - Auto-restart on crash
    - Auto-restart Outlook on Outlook errors
    - Restart app if Outlook errors persist
    """
    def __init__(self, initial: GuardrailSettings | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("User Settings")
        self._result = (initial or GuardrailSettings.load())

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ---- App lifecycle group ----
        grp_app = QGroupBox("App resilience")
        vl_app = QVBoxLayout(grp_app)

        self.chk_restart_on_exit = QCheckBox("Restart when I close the app")
        self.chk_restart_on_exit.setToolTip("If enabled, closing the window will relaunch the app automatically.")
        self.chk_restart_on_exit.setChecked(self._result.auto_restart_on_exit)

        self.chk_restart_on_crash = QCheckBox("Auto-restart on crash")
        self.chk_restart_on_crash.setToolTip("If the app crashes unexpectedly, it will relaunch automatically (with throttling).")
        self.chk_restart_on_crash.setChecked(self._result.auto_restart_on_crash)

        vl_app.addWidget(self.chk_restart_on_exit)
        vl_app.addWidget(self.chk_restart_on_crash)

        # ---- Outlook group ----
        grp_outlook = QGroupBox("Email/Outlook guardrails")
        vl_out = QVBoxLayout(grp_outlook)

        self.chk_restart_outlook = QCheckBox("Auto-restart Outlook if Outlook/COM errors occur")
        self.chk_restart_outlook.setToolTip("When parsing hits MAPI/COM errors (e.g. 'object could not be found'), Outlook will be restarted and the parser retried once.")
        self.chk_restart_outlook.setChecked(self._result.restart_outlook_on_outlook_errors)

        self.chk_restart_app_on_outlook = QCheckBox("Restart this app if Outlook errors persist after retry")
        self.chk_restart_app_on_outlook.setToolTip("If restarting Outlook didn’t help, the app will restart to recover.")
        self.chk_restart_app_on_outlook.setChecked(self._result.restart_app_on_outlook_errors)

        vl_out.addWidget(self.chk_restart_outlook)
        vl_out.addWidget(self.chk_restart_app_on_outlook)

        # Info label
        info = QLabel("Note: Restarts are throttled to avoid loops.")
        info.setStyleSheet("color:#666;")
        info.setWordWrap(True)

        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)

        # Assemble
        root.addWidget(grp_app)
        root.addWidget(grp_outlook)
        root.addWidget(info)
        root.addWidget(btns)

    def _on_accept(self):
        self._result.auto_restart_on_exit  = self.chk_restart_on_exit.isChecked()
        self._result.auto_restart_on_crash = self.chk_restart_on_crash.isChecked()
        self._result.restart_outlook_on_outlook_errors = self.chk_restart_outlook.isChecked()
        self._result.restart_app_on_outlook_errors     = self.chk_restart_app_on_outlook.isChecked()
        self.accept()

    def result(self) -> GuardrailSettings:
        return self._result
