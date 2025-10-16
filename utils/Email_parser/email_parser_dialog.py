# email_parser_dialog.py
from __future__ import annotations
from typing import Optional
import os
import re

from PyQt6.QtCore import Qt, QDateTime
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QFileDialog, QListWidget, QListWidgetItem, QMessageBox, QCheckBox, QComboBox,
    QStackedWidget, QWidget, QDateTimeEdit, QGroupBox, QToolButton, QFrame, QScrollArea
)

from utils.Email_parser.email_parser_core import (
    EmailParserConfig, DEFAULT_MAILBOX, list_outlook_folder_paths
)
from utils.Email_parser.email_parser_ftp import FTPSession

from datetime import datetime, timezone, timedelta
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo, available_timezones
except Exception:
    ZoneInfo = None
    def available_timezones():  # fallback; user can install tzdata to populate
        return {"UTC"}


DT_FORMAT = "yyyy-MM-dd HH:mm:ss"

def _hhmm_to_minutes(s: str) -> int:
    """
    Parse strings like '+01:15', '-00:30', '1:00', '90' (minutes), or '0'.
    Returns signed minutes. Invalid/blank -> 0.
    """
    raw = (s or "").strip()
    if not raw:
        return 0
    # Pure integer (minutes)
    if re.fullmatch(r"[+-]?\d+", raw):
        try:
            return int(raw)
        except Exception:
            return 0
    # HH:MM with optional sign
    m = re.fullmatch(r"\s*([+-])?\s*(\d{1,2})\s*:\s*(\d{1,2})\s*", raw)
    if not m:
        return 0
    sign = -1 if (m.group(1) == "-") else 1
    hh = int(m.group(2))
    mm = int(m.group(3))
    return sign * (hh * 60 + mm)

def _fmt_offset(td):
    # td can be None for odd zones; treat as zero
    total_min = int((td or timedelta(0)).total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    total_min = abs(total_min)
    hh, mm = divmod(total_min, 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}", (1 if sign == "+" else -1) * (hh * 60 + mm)

def _build_timezone_options(reference_dt=None):
    """
    Returns list[dict]: {
      "value": "Europe/Berlin",
      "label": "Europe/Berlin (UTC+02:00)",
      "offset_min": 120,
      "is_dst": True,
    }
    Sorted by offset then name. Use 'label' for display, 'value' to store.
    """
    if reference_dt is None:
        reference_dt = datetime.now(timezone.utc)
    if ZoneInfo is None:
        return [{"value": "UTC", "label": "UTC (UTC+00:00)", "offset_min": 0, "is_dst": False}]

    opts = []
    for tz in sorted(available_timezones()):
        zi = ZoneInfo(tz)
        local = reference_dt.astimezone(zi)
        offset_label, offset_min = _fmt_offset(local.utcoffset())
        is_dst = bool(local.dst() and local.dst().total_seconds() != 0)
        opts.append({
            "value": tz,
            "label": f"{tz} ({offset_label})",
            "offset_min": offset_min,
            "is_dst": is_dst,
        })
    opts.sort(key=lambda r: (r["offset_min"], r["value"]))
    return opts



class CollapsibleSection(QWidget):
    """Small helper: a collapsible titled section."""
    def __init__(self, title: str, content: QWidget, *, start_collapsed: bool = False, parent=None):
        super().__init__(parent)
        self._content = content

        # Header
        self.btn = QToolButton()
        self.btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.ArrowType.RightArrow if start_collapsed else Qt.ArrowType.DownArrow)
        self.btn.setText(title)
        self.btn.setCheckable(True)
        self.btn.setChecked(not start_collapsed)
        self.btn.clicked.connect(self._toggle)

        # Separator line (optional)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hdr = QHBoxLayout()
        hdr.addWidget(self.btn)
        hdr.addStretch(1)
        lay.addLayout(hdr)
        lay.addWidget(self._content)
        lay.addWidget(line)

        self._content.setVisible(not start_collapsed)

    def _toggle(self, checked: bool):
        self._content.setVisible(checked)
        self.btn.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)


class EmailParserDialog(QDialog):
    def __init__(self, parent=None, default_db_dir: str = "", initial: Optional[EmailParserConfig] = None):
        super().__init__(parent)
        self.setWindowTitle("Configure Email/Webhook Parser")
        self.setModal(True)
        # Bigger, tidier defaults
        self.setMinimumSize(1100, 800)
        self.resize(1200, 900)
        self.setSizeGripEnabled(True)

        self._default_db_dir = default_db_dir
        self._initial = initial or EmailParserConfig()

        # ──────────────────────────────────────
        # Top-level: scrollable container
        # ──────────────────────────────────────
        root = QVBoxLayout(self)
        scroller = QScrollArea(self)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroller)

        host = QWidget()
        scroller.setWidget(host)
        page = QVBoxLayout(host)
        page.setContentsMargins(12, 12, 12, 12)
        page.setSpacing(12)

        # ──────────────────────────────────────
        # Section 1: Source selection + per-source settings (stacked)
        # ──────────────────────────────────────
        src_container = QWidget()
        src_layout = QVBoxLayout(src_container)

        # Source selector
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Outlook", "Webhook"])
        self.source_combo.setCurrentText("Webhook" if getattr(self._initial, "webhook_enabled", False) else "Outlook")
        self.source_combo.setMinimumWidth(220)
        src_row.addWidget(self.source_combo, 1)
        src_layout.addLayout(src_row)

        # Outlook/Webhook pages
        self.stack = QStackedWidget()
        self.page_outlook = self._build_page_outlook()
        self.page_webhook = self._build_page_webhook()
        self.stack.addWidget(self.page_outlook)
        self.stack.addWidget(self.page_webhook)
        src_layout.addWidget(self.stack)

        sec_source = CollapsibleSection("Source (Outlook/Webhook)", src_container, start_collapsed=False)
        page.addWidget(sec_source)

        # ──────────────────────────────────────
        # Section 2: Output & formatting
        # ──────────────────────────────────────
        out_container = QWidget()
        out_form = QFormLayout(out_container)
        out_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        out_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)

        # Parser name
        self.name_edit = QLineEdit(self._initial.parser_name or "")
        if not self.name_edit.text().strip():
            if (self._initial.output_format or "db").lower() == "db" and self._initial.db_path:
                self.name_edit.setText(os.path.splitext(os.path.basename(self._initial.db_path))[0])
            elif self._initial.output_dir:
                self.name_edit.setText(f"{os.path.basename(self._initial.output_dir)}_{(self._initial.output_format or 'db').lower()}")
            else:
                self.name_edit.setText("parser")

        # Mailbox label-ish
        self.mailbox_edit = QLineEdit(self._initial.mailbox or DEFAULT_MAILBOX)

        # Output format
        self.format_combo = QComboBox()
        self.format_combo.addItems(["db", "csv", "txt"])
        self.format_combo.setCurrentText((self._initial.output_format or "db").lower())
        self.format_combo.currentTextChanged.connect(self._refresh_visibility)

        # DB path row
        self.db_path_edit = QLineEdit(self._initial.db_path or "")
        btn_browse_db = QPushButton("Browse…")
        btn_browse_db.clicked.connect(self._pick_db_path)
        self.db_row = QHBoxLayout()
        self.db_row.addWidget(self.db_path_edit, 1)
        self.db_row.addWidget(btn_browse_db)

        # Output directory row (csv/txt)
        self.output_dir_edit = QLineEdit(self._initial.output_dir or "")
        btn_browse_dir = QPushButton("Choose…")
        btn_browse_dir.clicked.connect(self._pick_output_dir)
        self.dir_row = QHBoxLayout()
        self.dir_row.addWidget(self.output_dir_edit, 1)
        self.dir_row.addWidget(btn_browse_dir)

        # Destinations
        self.chk_write_local = QCheckBox("Write locally")
        self.chk_write_local.setChecked(getattr(self._initial, "use_local_output", True))
        self.chk_upload_ftp = QCheckBox("Upload via FTP/FTPS")
        self.chk_upload_ftp.setChecked(getattr(self._initial, "use_ftp_output", False))
        self.chk_write_local.toggled.connect(self._refresh_visibility)
        self.chk_upload_ftp.toggled.connect(self._refresh_visibility)

        # FTP settings group
        self.grp_ftp = QGroupBox("FTP / FTPS")
        ftp_form = QFormLayout(self.grp_ftp)

        self.ftp_host_edit = QLineEdit(getattr(self._initial, "ftp_host", ""))
        self.ftp_port_edit = QLineEdit(str(getattr(self._initial, "ftp_port", 21)))
        self.ftp_user_edit = QLineEdit(getattr(self._initial, "ftp_username", ""))
        self.ftp_pass_edit = QLineEdit(getattr(self._initial, "ftp_password", ""))
        self.ftp_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.ftp_dir_edit = QLineEdit(getattr(self._initial, "ftp_remote_dir", ""))
        self.ftp_tls_chk = QCheckBox("Use FTPS (TLS)")
        self.ftp_tls_chk.setChecked(getattr(self._initial, "ftp_use_tls", False))
        self.ftp_pasv_chk = QCheckBox("Passive mode")
        self.ftp_pasv_chk.setChecked(getattr(self._initial, "ftp_passive", True))
        self.ftp_timeout_edit = QLineEdit(str(getattr(self._initial, "ftp_timeout", 20)))
        self.ftp_check_chk = QCheckBox("Check connection on Start")
        self.ftp_check_chk.setChecked(getattr(self._initial, "ftp_check_on_start", True))
        self.ftp_delete_chk = QCheckBox("Delete local files after upload")
        self.ftp_delete_chk.setChecked(getattr(self._initial, "ftp_delete_local_after_upload", False))
        self.ftp_vrf_chk = QCheckBox("Create .vrf control file for TXT uploads")
        self.ftp_vrf_chk.setChecked(getattr(self._initial, "ftp_make_vrf_files", False))
        self.ftp_test_btn = QPushButton("Test FTP…")
        self.ftp_test_btn.clicked.connect(self._test_ftp)

        ftp_form.addRow("Host:", self.ftp_host_edit)
        ftp_form.addRow("Port:", self.ftp_port_edit)
        ftp_form.addRow("Username:", self.ftp_user_edit)
        ftp_form.addRow("Password:", self.ftp_pass_edit)
        ftp_form.addRow("Remote directory:", self.ftp_dir_edit)
        ftp_form.addRow(self.ftp_tls_chk)
        ftp_form.addRow(self.ftp_pasv_chk)
        ftp_form.addRow(self.ftp_vrf_chk)
        ftp_form.addRow("Timeout (s):", self.ftp_timeout_edit)
        ftp_form.addRow(self.ftp_check_chk)
        ftp_form.addRow(self.ftp_delete_chk)
        ftp_form.addRow(self.ftp_test_btn)

        # Granularity
        self.gran_combo = QComboBox()
        self.gran_combo.addItems(["email", "day", "week", "month"])
        self.gran_combo.setCurrentText((self._initial.file_granularity or "day").lower())

        # Lookup
        self.lookup_edit = QLineEdit(self._initial.lookup_path or "")
        btn_lookup = QPushButton("Lookup…")
        btn_lookup.clicked.connect(self._pick_lookup)
        self.lookup_row = QHBoxLayout()
        self.lookup_row.addWidget(self.lookup_edit, 1)
        self.lookup_row.addWidget(btn_lookup)

        # Filename pattern
        self.pattern_edit = QLineEdit(self._initial.filename_pattern or "(payload_datetime)")
        pattern_help = QLabel(
            "Filename tokens (case-insensitive): (payload_datetime), (date), (time), (datetime), "
            "(sender), (folder), (received_last10min) / (use_nearest_10_min), and TX tokens "
            "(transmit_time),(transmit_ts12),(transmit_iso) [also 'transit_*' aliases]. "
            "When granularity='email' and your pattern has no time-like token, the HHMMSS is appended.\n"
            "Examples: (C_S)(received_last10min) → S23251001104000; "
            "(TAG)_(transmit_time) → L3_251001104000"
        )
        pattern_help.setWordWrap(True)
        pattern_help.setStyleSheet("color: gray; font-style: italic;")

        # Missing value
        self.missing_combo = QComboBox()
        self.missing_combo.setEditable(True)
        self.missing_combo.addItems(["", "-9999", "N/A", "0"])
        self.missing_combo.setCurrentText(self._initial.missing_value or "")

        # ── NEW: TXT/D-line timestamp behavior & time shifts ─────────────────
        self.grp_time = QGroupBox("Timestamp & Time Shifts")
        time_form = QFormLayout(self.grp_time)

        # --- Time zone (DST-aware) shift controls ---
        self.chk_use_tz = QCheckBox("Convert timestamps from UTC to time zone")
        self.chk_use_tz.setChecked(getattr(self._initial, "use_timezone_shift", False))

        self.tz_combo = QComboBox()
        self.tz_combo.setEditable(True)  # type-to-search 600+ zones

        def _populate_tz_combo(init_value: str):
            self.tz_combo.clear()
            options = _build_timezone_options()  # labels reflect current DST
            for opt in options:
                # text shown to user
                self.tz_combo.addItem(opt["label"], userData=opt["value"])
            # try to select initial tz by value (IANA ID)
            iana = (init_value or "").strip() or "UTC"
            for i in range(self.tz_combo.count()):
                if self.tz_combo.itemData(i) == iana:
                    self.tz_combo.setCurrentIndex(i)
                    break

        init_tz = getattr(self._initial, "timezone_name", "") or "UTC"
        _populate_tz_combo(init_tz)

        # show TZ picker only if enabled
        def _toggle_tz_widgets(on: bool):
            self.tz_combo.setEnabled(on)
            btn_refresh_tz.setEnabled(on)

        btn_refresh_tz = QToolButton()
        btn_refresh_tz.setText("↻")
        btn_refresh_tz.setToolTip("Refresh timezone offsets (reflect DST now)")
        btn_refresh_tz.clicked.connect(lambda: _populate_tz_combo(self.tz_combo.currentData() or "UTC"))

        tz_row = QHBoxLayout()
        tz_row.addWidget(self.tz_combo, 1)
        tz_row.addWidget(btn_refresh_tz, 0)

        _toggle_tz_widgets(self.chk_use_tz.isChecked())
        self.chk_use_tz.toggled.connect(_toggle_tz_widgets)

        time_form.addRow(self.chk_use_tz, QWidget())  # place checkbox as label row
        time_form.addRow("", tz_row)

        txt = QLabel(
            "TXT timestamp override works for #S→#D and inbound #D lines.\n"
            "• Lookup 'timestamp_field' can be a column name, 'received_last10min' (or 'use_nearest_10_min'), "
            "'transmit_time'/'transmit_ts12', or 'transmit_last10min'. You may add an inline offset like '+01:10'.\n"
            "• After that, the global Payload shift below (if enabled) is applied.\n"
            "• Filename tokens can use the snapped/shifted values via the tokens listed above."
        )
        txt.setWordWrap(True)
        txt.setStyleSheet("color: gray;")
        time_form.addRow(txt)

        # Fallback TXT timestamp base (only used when lookup doesn't specify)
        self.txt_mode_combo = QComboBox()
        self.txt_mode_combo.addItems(["payload", "received_prev10"])
        self.txt_mode_combo.setCurrentText(getattr(self._initial, "txt_timestamp_mode", "payload"))
        time_form.addRow("TXT fallback timestamp base:", self.txt_mode_combo)

        # Payload time shift
        self.chk_shift_payload = QCheckBox("Shift payload timestamps by")
        self.chk_shift_payload.setChecked(bool(getattr(self._initial, "shift_payload_time", False)))
        self.shift_payload_edit = QLineEdit(getattr(self._initial, "payload_time_shift", "+00:00"))
        self.shift_payload_edit.setPlaceholderText("+HH:MM or minutes")
        time_row1 = QHBoxLayout()
        time_row1.addWidget(self.chk_shift_payload)
        time_row1.addWidget(self.shift_payload_edit, 1)
        time_form.addRow(time_row1)

        # Filename time shift
        self.chk_shift_filename = QCheckBox("Shift filename time tokens by")
        self.chk_shift_filename.setChecked(bool(getattr(self._initial, "shift_filename_time", False)))
        self.shift_filename_edit = QLineEdit(getattr(self._initial, "filename_time_shift", "+00:00"))
        self.shift_filename_edit.setPlaceholderText("+HH:MM or minutes")
        time_row2 = QHBoxLayout()
        time_row2.addWidget(self.chk_shift_filename)
        time_row2.addWidget(self.shift_filename_edit, 1)
        time_form.addRow(time_row2)
        # ────────────────────────────────────────────────────────────────────

        # Auto-run
        self.auto_run_check = QCheckBox("Enable auto-run for this parser")
        self.auto_run_check.setChecked(self._initial.auto_run)

        # Assemble Output section
        out_form.addRow("Parser name:", self.name_edit)
        out_form.addRow("Mailbox (label only):", self.mailbox_edit)
        out_form.addRow("Output format:", self.format_combo)
        out_form.addRow("Output .db path:", self.db_row)
        out_form.addRow(self.chk_write_local)
        out_form.addRow(self.chk_upload_ftp)
        out_form.addRow("Output folder (csv/txt):", self.dir_row)
        out_form.addRow(self.grp_ftp)
        out_form.addRow("Granularity:", self.gran_combo)
        out_form.addRow("Header lookup:", self.lookup_row)
        out_form.addRow("Filename pattern:", self.pattern_edit)
        out_form.addRow("Missing value:", self.missing_combo)
        out_form.addRow(pattern_help)
        out_form.addRow(self.grp_time)
        out_form.addRow("", self.auto_run_check)

        sec_output = CollapsibleSection("Output & Formatting", out_container, start_collapsed=False)
        page.addWidget(sec_output)

        # ──────────────────────────────────────
        # Section 3: Range & checkpoint (per parser)
        # ──────────────────────────────────────
        range_container = QWidget()
        r_lay = QFormLayout(range_container)

        self.chk_from = QCheckBox("Set a 'From' time (older bound)")
        self.dt_from = QDateTimeEdit()
        self.dt_from.setDisplayFormat(DT_FORMAT)
        self.dt_from.setCalendarPopup(True)
        self.dt_from.setEnabled(False)

        self.chk_to = QCheckBox("Set a 'To' time (newer bound)")
        self.dt_to = QDateTimeEdit()
        self.dt_to.setDisplayFormat(DT_FORMAT)
        self.dt_to.setCalendarPopup(True)
        self.dt_to.setEnabled(False)

        self.chk_from.toggled.connect(self.dt_from.setEnabled)
        self.chk_to.toggled.connect(self.dt_to.setEnabled)

        if (self._initial.manual_from or "").strip():
            self.chk_from.setChecked(True)
            self.dt_from.setEnabled(True)
            self.dt_from.setDateTime(QDateTime.fromString(self._initial.manual_from, DT_FORMAT))
        if (self._initial.manual_to or "").strip():
            self.chk_to.setChecked(True)
            self.dt_to.setEnabled(True)
            self.dt_to.setDateTime(QDateTime.fromString(self._initial.manual_to, DT_FORMAT))

        self.chk_respect = QCheckBox("Respect existing checkpoint when scanning (recommended)")
        self.chk_respect.setChecked(getattr(self._initial, "respect_checkpoint", True))

        self.chk_update = QCheckBox("Advance checkpoint after this run")
        self.chk_update.setChecked(getattr(self._initial, "update_checkpoint", True))

        self.chk_reset = QCheckBox("Reset this parser’s state **before** this run")
        self.chk_reset.setChecked(getattr(self._initial, "reset_state_before_run", False))
        self.chk_reset.setToolTip("Clears per-parser state DB (checkpoints, exports, processed ids) before running.")

        tip = QLabel(
            "Tips:\n"
            "• Leave 'To' unset to keep parsing up to now.\n"
            "• Set 'From' and uncheck “Respect checkpoint” to reparse older mail from that time.\n"
            "• Uncheck “Advance checkpoint” for a one-off historical run that doesn’t move the checkpoint."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")

        r_lay.addRow(self.chk_from, self.dt_from)
        r_lay.addRow(self.chk_to, self.dt_to)
        r_lay.addRow(self.chk_respect)
        r_lay.addRow(self.chk_update)
        r_lay.addRow(self.chk_reset)
        r_lay.addRow(tip)

        sec_range = CollapsibleSection("Range & Checkpoints", range_container, start_collapsed=True)
        page.addWidget(sec_range)

        # ──────────────────────────────────────
        # Section 4: Advanced (placeholder)
        # ──────────────────────────────────────
        adv_container = QWidget()
        adv_layout = QVBoxLayout(adv_container)
        note = QLabel("Advanced settings can be added here (e.g., lookback hours spinner, quiet logging toggle).")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        adv_layout.addWidget(note)
        sec_adv = CollapsibleSection("Advanced", adv_container, start_collapsed=True)
        page.addWidget(sec_adv)

        # ──────────────────────────────────────
        # Bottom buttons
        # ──────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_ok = QPushButton("OK")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        page.addLayout(btn_row)

        # Signals and initial state
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        self._on_source_changed(self.source_combo.currentText())
        self._refresh_visibility()

    # ──────────────────────────────────────
    # Per-source pages
    # ──────────────────────────────────────
    def _build_page_outlook(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        folders_row = QHBoxLayout()
        left_col = QVBoxLayout()
        right_col = QVBoxLayout()

        left_col.addWidget(QLabel("Available Outlook folders (relative to mailbox):"))
        self.available_list = QListWidget()
        left_col.addWidget(self.available_list, 1)

        btns = QVBoxLayout()
        self.btn_load = QPushButton("Load Folders")
        self.btn_add = QPushButton("→")
        self.btn_remove = QPushButton("←")
        self.btn_add.clicked.connect(self._add_selected)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_load.clicked.connect(self._load_folders)
        btns.addWidget(self.btn_load)
        btns.addStretch(1)
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_remove)
        btns.addStretch(2)

        right_col.addWidget(QLabel("Selected folder paths:"))
        self.selected_list = QListWidget()
        right_col.addWidget(self.selected_list, 1)

        folders_row.addLayout(left_col, 5)
        folders_row.addLayout(btns, 1)
        folders_row.addLayout(right_col, 5)

        v.addLayout(folders_row, 1)
        return w

    def _build_page_webhook(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        web_grid = QFormLayout()
        self.webhook_url_edit = QLineEdit(getattr(self._initial, "webhook_url", ""))
        self.webhook_auth_edit = QLineEdit(getattr(self._initial, "webhook_auth_header", ""))
        self.webhook_since_param_edit = QLineEdit(getattr(self._initial, "webhook_since_param", "since"))
        self.webhook_limit_param_edit = QLineEdit(getattr(self._initial, "webhook_limit_param", "limit"))
        self.webhook_limit_edit = QLineEdit(str(getattr(self._initial, "webhook_limit", 200)))

        web_grid.addRow(QLabel("Webhook settings"))
        web_grid.addRow("Feed URL:", self.webhook_url_edit)
        web_grid.addRow("Auth header (optional):", self.webhook_auth_edit)
        web_grid.addRow("Since param:", self.webhook_since_param_edit)
        web_grid.addRow("Limit param:", self.webhook_limit_param_edit)
        web_grid.addRow("Limit:", self.webhook_limit_edit)
        v.addLayout(web_grid)

        v.addWidget(QLabel("Note: Webhook polling ignores folders; a logical 'WEBHOOK' tag is used internally."))
        return w

    # ──────────────────────────────────────
    # UI helpers
    # ──────────────────────────────────────
    def _on_source_changed(self, name: str):
        idx = {"Outlook": 0, "Webhook": 1}.get(name, 0)
        self.stack.setCurrentIndex(idx)
        self.mailbox_edit.setEnabled(True)
        self._refresh_visibility()

        is_outlook = (idx == 0)
        if is_outlook and self._initial.folder_paths:
            if self.available_list.count() == 0 and self.selected_list.count() == 0:
                for p in self._initial.folder_paths:
                    it = QListWidgetItem(" > ".join(p))
                    it.setData(Qt.ItemDataRole.UserRole, p)
                    self.selected_list.addItem(it)

    def _refresh_visibility(self):
        fmt = (self.format_combo.currentText() or "db").lower()
        is_db = (fmt == "db")
        files_mode = not is_db

        # DB-only widgets
        for i in range(self.db_row.count()):
            w = self.db_row.itemAt(i).widget()
            if w:
                w.setVisible(is_db)

        # File outputs visibility
        self.chk_write_local.setVisible(files_mode)
        self.chk_upload_ftp.setVisible(files_mode)
        self.gran_combo.setVisible(files_mode)
        self.pattern_edit.setVisible(files_mode)

        # Lookup applies to all modes
        for i in range(self.lookup_row.count()):
            w = self.lookup_row.itemAt(i).widget()
            if w:
                w.setVisible(True)

        # Show local dir picker only if writing locally
        show_local_dir = files_mode and self.chk_write_local.isChecked()
        for i in range(self.dir_row.count()):
            w = self.dir_row.itemAt(i).widget()
            if w:
                w.setVisible(show_local_dir)

        # Show FTP group only if FTP is enabled
        self.grp_ftp.setVisible(files_mode and self.chk_upload_ftp.isChecked())
        # Show the .vrf option only when TXT + FTP
        show_vrf = files_mode and self.chk_upload_ftp.isChecked() and (self.format_combo.currentText().lower() == "txt")
        if hasattr(self, "ftp_vrf_chk"):
            self.ftp_vrf_chk.setVisible(show_vrf)

        # Time-shift group only for files (affects TXT and filenames)
        self.grp_time.setVisible(files_mode)

    # ──────────────────────────────────────
    # Outlook folder picker handlers
    # ──────────────────────────────────────
    def _load_folders(self):
        self.available_list.clear()
        mailbox = (self.mailbox_edit.text() or DEFAULT_MAILBOX).strip() or DEFAULT_MAILBOX
        try:
            paths = list_outlook_folder_paths(mailbox)
            for p in paths:
                txt = " > ".join(p)
                it = QListWidgetItem(txt)
                it.setData(Qt.ItemDataRole.UserRole, p)
                self.available_list.addItem(it)
        except Exception as e:
            QMessageBox.critical(self, "Outlook", f"Failed to list folders:\n{e}")

    def _add_selected(self):
        for it in self.available_list.selectedItems():
            p = list(it.data(Qt.ItemDataRole.UserRole) or [])
            if not p:
                continue
            exists = False
            for j in range(self.selected_list.count()):
                if self.selected_list.item(j).data(Qt.ItemDataRole.UserRole) == p:
                    exists = True
                    break
            if not exists:
                s = QListWidgetItem(" > ".join(p))
                s.setData(Qt.ItemDataRole.UserRole, p)
                self.selected_list.addItem(s)

    def _remove_selected(self):
        for it in self.selected_list.selectedItems():
            idx = self.selected_list.row(it)
            self.selected_list.takeItem(idx)

    # ──────────────────────────────────────
    # Common pickers
    # ──────────────────────────────────────
    def _pick_db_path(self):
        base = self._default_db_dir or os.getcwd()
        path, _ = QFileDialog.getSaveFileName(self, "Choose output .db", base, "SQLite DB (*.db)")
        if path:
            if not path.lower().endswith(".db"):
                path += ".db"
            self.db_path_edit.setText(path)

    def _pick_output_dir(self):
        base = self._default_db_dir or os.getcwd()
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", base)
        if path:
            self.output_dir_edit.setText(path)

    def _pick_lookup(self):
        base = self._default_db_dir or os.getcwd()
        file_path, _ = QFileDialog.getOpenFileName(self, "Choose lookup JSON (or Cancel to pick a folder)", base, "JSON (*.json)")
        if file_path:
            self.lookup_edit.setText(file_path)
            return
        dir_path = QFileDialog.getExistingDirectory(self, "Choose lookup folder (per-sender JSONs)", base)
        if dir_path:
            self.lookup_edit.setText(dir_path)

    # ──────────────────────────────────────
    # FTP test
    # ──────────────────────────────────────
    def _test_ftp(self):
        # Build a temporary config with current FTP fields
        try:
            port = int(self.ftp_port_edit.text().strip() or "21")
        except Exception:
            port = 21
        try:
            timeout = int(self.ftp_timeout_edit.text().strip() or "20")
        except Exception:
            timeout = 20

        tmp = EmailParserConfig(
            ftp_host=self.ftp_host_edit.text().strip(),
            ftp_port=port,
            ftp_username=self.ftp_user_edit.text().strip(),
            ftp_password=self.ftp_pass_edit.text(),
            ftp_remote_dir=self.ftp_dir_edit.text().strip(),
            ftp_use_tls=self.ftp_tls_chk.isChecked(),
            ftp_passive=self.ftp_pasv_chk.isChecked(),
            ftp_timeout=timeout,
            quiet=False,
        )
        try:
            with FTPSession(tmp) as sess:
                ok, msg = sess.test_connection()
            if ok:
                QMessageBox.information(self, "FTP", "Connection OK.")
            else:
                QMessageBox.critical(self, "FTP", f"Connection failed:\n{msg}")
        except Exception as e:
            QMessageBox.critical(self, "FTP", f"Connection error:\n{e}")

    # ──────────────────────────────────────
    # Export config
    # ──────────────────────────────────────
    def get_config(self) -> EmailParserConfig:
        cfg = EmailParserConfig()
        cfg.mailbox = (self.mailbox_edit.text() or DEFAULT_MAILBOX).strip() or DEFAULT_MAILBOX
        cfg.parser_name = (self.name_edit.text() or "").strip() or "parser"

        fmt = (self.format_combo.currentText() or "db").lower()
        cfg.output_format = fmt
        cfg.db_path = (self.db_path_edit.text() or "").strip()
        cfg.output_dir = (self.output_dir_edit.text() or "").strip()
        cfg.file_granularity = (self.gran_combo.currentText() or "day").lower()
        cfg.lookup_path = (self.lookup_edit.text() or "").strip()

        cfg.filename_pattern = (self.pattern_edit.text() or "(payload_datetime)").strip()
        cfg.filename_code = ""  # legacy, unused
        cfg.missing_value = self.missing_combo.currentText()
        cfg.auto_run = self.auto_run_check.isChecked()

        # TXT fallback (used when lookup doesn't specify timestamp_field)
        cfg.txt_timestamp_mode = (self.txt_mode_combo.currentText() or "payload").strip().lower()

        # Global shifts (payload & filename)
        cfg.shift_payload_time = self.chk_shift_payload.isChecked()
        cfg.payload_time_shift = (self.shift_payload_edit.text() or "").strip() or "+00:00"
        cfg.payload_time_shift_minutes = _hhmm_to_minutes(cfg.payload_time_shift)

        cfg.shift_filename_time = self.chk_shift_filename.isChecked()
        cfg.filename_time_shift = (self.shift_filename_edit.text() or "").strip() or "+00:00"
        cfg.filename_time_shift_minutes = _hhmm_to_minutes(cfg.filename_time_shift)
        cfg.use_timezone_shift = self.chk_use_tz.isChecked()
        # store the underlying zone ID (IANA), not the pretty label
        cfg.timezone_name = (self.tz_combo.currentData() or self.tz_combo.currentText() or "").strip()

        # Source flags
        src = self.source_combo.currentText()
        cfg.webhook_enabled = (src == "Webhook")

        # Outlook folders → folder_paths
        cfg.folder_paths = []
        if src == "Outlook":
            for i in range(self.selected_list.count()):
                p = list(self.selected_list.item(i).data(Qt.ItemDataRole.UserRole) or [])
                if p and p[0].strip().lower() == cfg.mailbox.strip().lower():
                    p = p[1:]
                cfg.folder_paths.append(p)
        else:
            cfg.folder_paths = [["WEBHOOK"]]

        # WEBHOOK settings
        cfg.webhook_url = getattr(self, "webhook_url_edit", QLineEdit()).text().strip()
        cfg.webhook_auth_header = getattr(self, "webhook_auth_edit", QLineEdit()).text().strip()
        cfg.webhook_since_param = getattr(self, "webhook_since_param_edit", QLineEdit("since")).text().strip() or "since"
        cfg.webhook_limit_param = getattr(self, "webhook_limit_param_edit", QLineEdit("limit")).text().strip() or "limit"
        try:
            cfg.webhook_limit = int(getattr(self, "webhook_limit_edit", QLineEdit("200")).text().strip() or "200")
        except Exception:
            cfg.webhook_limit = 200

        # Range/checkpoint → core config
        cfg.manual_from = self.dt_from.text().strip() if self.chk_from.isChecked() else ""
        cfg.manual_to = self.dt_to.text().strip() if self.chk_to.isChecked() else ""
        cfg.respect_checkpoint = self.chk_respect.isChecked()
        cfg.update_checkpoint = self.chk_update.isChecked()
        cfg.reset_state_before_run = self.chk_reset.isChecked()

        # Destinations
        cfg.use_local_output = self.chk_write_local.isChecked()
        cfg.use_ftp_output = self.chk_upload_ftp.isChecked()

        # FTP settings
        try:
            cfg.ftp_port = int(self.ftp_port_edit.text().strip() or "21")
        except Exception:
            cfg.ftp_port = 21
        try:
            cfg.ftp_timeout = int(self.ftp_timeout_edit.text().strip() or "20")
        except Exception:
            cfg.ftp_timeout = 20
        cfg.ftp_host = self.ftp_host_edit.text().strip()
        cfg.ftp_username = self.ftp_user_edit.text().strip()
        cfg.ftp_password = self.ftp_pass_edit.text()
        cfg.ftp_remote_dir = self.ftp_dir_edit.text().strip()
        cfg.ftp_use_tls = self.ftp_tls_chk.isChecked()
        cfg.ftp_passive = self.ftp_pasv_chk.isChecked()
        cfg.ftp_check_on_start = self.ftp_check_chk.isChecked()
        cfg.ftp_delete_local_after_upload = self.ftp_delete_chk.isChecked()
        cfg.ftp_make_vrf_files = getattr(self, "ftp_vrf_chk", None).isChecked() if hasattr(self, "ftp_vrf_chk") else False

        # Default name if needed
        if not cfg.parser_name:
            if cfg.output_format == "db" and cfg.db_path:
                base = os.path.splitext(os.path.basename(cfg.db_path))[0] or "Parser"
                cfg.parser_name = base
            elif cfg.output_dir:
                cfg.parser_name = f"{os.path.basename(cfg.output_dir)}_{cfg.output_format}"
            else:
                cfg.parser_name = f"{src}_parser"

        # Guard: in FILE modes, require at least one destination
        if fmt != "db" and not (cfg.use_local_output or cfg.use_ftp_output):
            QMessageBox.warning(self, "Output destination", "Choose at least one destination: local and/or FTP.")
            cfg.use_local_output = True

        return cfg
