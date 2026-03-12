"""
utils/alerts/alerts_panel.py
============================
TableAlertsDialog — per-table alert management.

Layout:
  Toolbar: [+ Add Alert ▾]  [Configure]  [Inspect]  [Enable]  [Disable]  … [Remove]
  ─────────────────────────────────────────────────────────────────────────────────
  Left (list):  coloured left-bar + badge circle + name / type + STATUS pill
  ─────────────────────────────────────────────────────────────────────────────────
  Right (detail card): name header + status pill / type / thresholds /
                       recipients / cooldown / interval / last-email
  ─────────────────────────────────────────────────────────────────────────────────
  [Close]
"""
from __future__ import annotations

import uuid
from typing import List, Optional

import pandas as pd
from PyQt6.QtCore import Qt, QRect, QSize
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from utils.alerts import REGISTRY, AlertSpec, Status
from utils.alerts.evaluator import load_specs, save_specs
from utils.alerts.store import (
    ensure_alerts_tables,
    read_last_email,
    read_last_status,
)
from utils.constants import is_battery_column

# ── Kind metadata ──────────────────────────────────────────────────────────────
_KIND_META = {
    "Stale":       ("S", "Stale data"),
    "Threshold":   ("T", "Threshold"),
    "MissingData": ("M", "Missing data"),
    "Distance":    ("D", "Distance"),
}
_KIND_ORDER = ["Stale", "Threshold", "MissingData", "Distance"]

_COLORS = {
    "GREEN":   "#2f9e44",
    "AMBER":   "#f59f00",
    "RED":     "#e03131",
    "OFF":     "#94a3b8",
    "UNKNOWN": "#94a3b8",
    "PENDING": "#3b82f6",
}
_PILL_TEXT = {
    "GREEN":   "OK",
    "AMBER":   "AMBER",
    "RED":     "RED",
    "OFF":     "OFF",
    "UNKNOWN": "—",
    "PENDING": "PENDING",
}


def _kind_char(kind: str) -> str:
    return _KIND_META.get(kind, (kind[:1].upper() if kind else "?", ""))[0]


def _kind_label(kind: str) -> str:
    return _KIND_META.get(kind, ("?", kind))[1]


def _status_hex(status: str, enabled: bool) -> str:
    if not enabled:
        return _COLORS["OFF"]
    return _COLORS.get((status or "UNKNOWN").upper(), _COLORS["UNKNOWN"])


def _status_pill_text(status: str, enabled: bool) -> str:
    if not enabled:
        return "DISABLED"
    return _PILL_TEXT.get((status or "OFF").upper(), status or "—")


def _make_chart_icon(size: int = 22, color: str = "#ffffff"):
    """Return a QIcon with a mini trend-line chart for the Inspect button."""
    from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QPen
    from PyQt6.QtCore import Qt, QPointF
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = QColor(color)
    pad = 2
    # Axes
    ax_pen = QPen(col, 1.4)
    ax_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(ax_pen)
    p.drawLine(pad, pad, pad, size - pad)
    p.drawLine(pad, size - pad, size - pad, size - pad)
    # Trend line (generally rising left-to-right)
    tr_pen = QPen(col, 1.8)
    tr_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    tr_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(tr_pen)
    inner = size - pad * 2 - 2
    ox = pad + 2
    oy_base = size - pad - 1
    pts = [
        QPointF(ox,               oy_base - inner * 0.15),
        QPointF(ox + inner * 0.25, oy_base - inner * 0.40),
        QPointF(ox + inner * 0.50, oy_base - inner * 0.55),
        QPointF(ox + inner * 0.75, oy_base - inner * 0.75),
        QPointF(ox + inner,        oy_base - inner * 0.90),
    ]
    for i in range(len(pts) - 1):
        p.drawLine(pts[i], pts[i + 1])
    p.end()
    return QIcon(px)


# ── Custom item roles ──────────────────────────────────────────────────────────
_ROLE_ID       = Qt.ItemDataRole.UserRole        # spec.id
_ROLE_HEX      = Qt.ItemDataRole.UserRole + 10   # status colour hex string
_ROLE_CHAR     = Qt.ItemDataRole.UserRole + 11   # kind badge letter
_ROLE_NAME     = Qt.ItemDataRole.UserRole + 12   # display name
_ROLE_KIND     = Qt.ItemDataRole.UserRole + 13   # kind label string
_ROLE_STATLBL  = Qt.ItemDataRole.UserRole + 14   # pill text
_ROLE_ENABLED  = Qt.ItemDataRole.UserRole + 15   # bool
_ROLE_ISBATT   = Qt.ItemDataRole.UserRole + 16   # bool — is a battery threshold alert


# ── Alert list delegate ────────────────────────────────────────────────────────
_ROW_H   = 56
_BAR_W   = 5
_BADGE_D = 30
_PAD     = 10


class _AlertDelegate(QStyledItemDelegate):
    """
    Paints each alert row as:
      ▌ [badge] Name (bold)          [STATUS pill]
      ▌         Kind label
    """

    def paint(self, painter: QPainter, option, index):
        painter.save()
        try:
            self._paint_inner(painter, option, index)
        except Exception:
            pass
        finally:
            painter.restore()

    def _paint_inner(self, painter: QPainter, option, index):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = option.rect
        # PyQt6: option.state is QStyle.State (QFlags), supports & with StateFlag directly
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered  = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # Row background
        if selected:
            bg = QColor("#dbeafe")
        elif hovered:
            bg = QColor("#f0f9ff")
        else:
            bg = QColor("#ffffff")
        painter.fillRect(r, bg)

        # Read custom roles
        hex_    = str(index.data(_ROLE_HEX)     or "#94a3b8")
        char_   = str(index.data(_ROLE_CHAR)    or "?")
        name_   = str(index.data(_ROLE_NAME)    or "")
        kind_   = str(index.data(_ROLE_KIND)    or "")
        stat_   = str(index.data(_ROLE_STATLBL) or "—")
        enab_   = bool(index.data(_ROLE_ENABLED))
        isbatt_ = bool(index.data(_ROLE_ISBATT))
        if isbatt_:
            kind_ = f"\u26a1 Battery  \u00b7  {kind_}"

        col = QColor(hex_)

        # 1) Left colour bar
        painter.fillRect(QRect(r.left(), r.top(), _BAR_W, r.height()), col)

        # 2) Kind badge circle
        bx = r.left() + _BAR_W + _PAD
        by = r.top() + (r.height() - _BADGE_D) // 2
        painter.setBrush(QBrush(col))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(bx, by, _BADGE_D, _BADGE_D)

        fnt = painter.font()
        fnt.setPixelSize(11)
        fnt.setBold(True)
        painter.setFont(fnt)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            QRect(bx, by, _BADGE_D, _BADGE_D),
            int(Qt.AlignmentFlag.AlignCenter),
            char_,
        )

        # 3) Name + kind label
        pill_w = 70
        tx = bx + _BADGE_D + _PAD
        tw = r.right() - tx - pill_w - _PAD * 2

        # Name (bold, dark)
        fn = painter.font()
        fn.setPixelSize(12)
        fn.setBold(True)
        painter.setFont(fn)
        painter.setPen(QPen(QColor("#111827") if enab_ else QColor("#9ca3af")))
        fm = painter.fontMetrics()
        painter.drawText(
            QRect(tx, r.top() + 8, tw, 18),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            fm.elidedText(name_, Qt.TextElideMode.ElideRight, tw),
        )

        # Kind label (secondary)
        fk = painter.font()
        fk.setPixelSize(10)
        fk.setBold(False)
        painter.setFont(fk)
        painter.setPen(QPen(QColor("#6b7280")))
        painter.drawText(
            QRect(tx, r.top() + 28, tw, 16),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            kind_,
        )

        # 4) Status pill (right side)
        pill_h = 22
        pill_x = r.right() - pill_w - _PAD
        pill_y = r.top() + (r.height() - pill_h) // 2
        pill_r = QRect(pill_x, pill_y, pill_w, pill_h)

        pill_bg = QColor(hex_)
        pill_bg.setAlpha(30)
        painter.setBrush(QBrush(pill_bg))
        painter.setPen(QPen(QColor(hex_), 1))
        painter.drawRoundedRect(pill_r, pill_h / 2, pill_h / 2)

        fp = painter.font()
        fp.setPixelSize(9)
        fp.setBold(True)
        painter.setFont(fp)
        painter.setPen(QPen(QColor(hex_)))
        painter.drawText(pill_r, int(Qt.AlignmentFlag.AlignCenter), stat_)

        # Bottom divider
        painter.setPen(QPen(QColor("#f3f4f6"), 1))
        painter.drawLine(r.left() + _BAR_W + 4, r.bottom(), r.right(), r.bottom())

    def sizeHint(self, option, index):
        w = option.rect.width() if option.rect.width() > 0 else 260
        return QSize(w, _ROW_H)


# ── Minimal host ───────────────────────────────────────────────────────────────
class _LiteHost:
    def __init__(self, db_path, table_name, df, datetime_col):
        self.db_path = db_path
        self.table_name = table_name
        self.df = df
        self.datetime_col = datetime_col


def _build_host(db_path: str, table: str) -> Optional[_LiteHost]:
    import sqlite3
    from utils.time_settings import parse_series_to_local_naive
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            cols = [r[1] for r in rows]
            pref = ["timestamp", "received_time", "datetime", "time", "date"]
            lower = {c.lower(): c for c in cols}
            dt_col = next((lower[n] for n in pref if n in lower),
                          cols[0] if cols else None)
            if not dt_col:
                return None
            df = pd.read_sql_query(
                f'SELECT * FROM "{table}" ORDER BY "{dt_col}" DESC LIMIT 5000',
                conn,
            )
            df = df.iloc[::-1].reset_index(drop=True)
            df[dt_col] = parse_series_to_local_naive(df[dt_col])
            return _LiteHost(db_path, table, df, dt_col)
    except Exception:
        return None


# ── Detail panel helpers ───────────────────────────────────────────────────────
def _divider() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("QFrame { border: none; border-top: 1px solid palette(mid); "
                    "background: transparent; border-radius: 0; }")
    return f


def _key_lbl(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "QLabel { font-weight: 600; font-size: 11px; "
        "background: transparent; border: none; border-radius: 0; }"
    )
    lbl.setFixedWidth(88)
    return lbl


def _val_lbl(text: str = "—") -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        "QLabel { font-weight: 400; font-size: 12px; "
        "background: transparent; border: none; border-radius: 0; }"
    )
    return lbl


# ── Main dialog ────────────────────────────────────────────────────────────────
class TableAlertsDialog(QDialog):
    """Manage all alerts for a single table."""

    def __init__(self, db_path: str, table_name: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.table_name = table_name
        ensure_alerts_tables(db_path)

        self.setWindowTitle(f"Alerts — {table_name}")
        self.setMinimumSize(860, 520)
        self.resize(940, 580)

        self._host: Optional[_LiteHost] = None
        self._specs: List[AlertSpec] = load_specs(db_path, table_name)
        self._timer_min = 5
        self._dismissed_battery_cols: set = set()
        from utils.alerts.store import read_current_settings
        saved = read_current_settings(db_path, table_name)
        if isinstance(saved, dict):
            self._timer_min = int(saved.get("timer_min", 5))
            dismissed = saved.get("dismissed_battery_cols", [])
            if isinstance(dismissed, list):
                self._dismissed_battery_cols = set(dismissed)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(12, 12, 12, 12)

        # ── Toolbar ───────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(6)

        _icon_sz = 42

        def _btn_ss(bg, bg_h, bg_p, fg, border, border_h):
            return (
                f"QPushButton {{ font-size: 18px; font-weight: 700; color: {fg}; "
                f"border: 1.5px solid {border}; border-radius: 8px; "
                f"background: {bg}; padding: 0px; }}"
                f"QPushButton:hover   {{ background: {bg_h}; border-color: {border_h}; }}"
                f"QPushButton:pressed {{ background: {bg_p}; }}"
            )

        # + Add  — blue
        self._add_btn = QPushButton("\u002B", self)
        self._add_btn.setFixedSize(_icon_sz, _icon_sz)
        self._add_btn.setToolTip("Add Alert")
        self._add_btn.setStyleSheet(_btn_ss("#2563eb","#1d4ed8","#1e40af","#ffffff","#1d4ed8","#1e40af"))
        add_menu = QMenu(self._add_btn)
        for kind in _KIND_ORDER:
            if kind in REGISTRY:
                add_menu.addAction(
                    f"  {_kind_char(kind)}  —  {_kind_label(kind)}",
                    lambda k=kind: self._add_alert(k),
                )
        self._add_btn.setMenu(add_menu)

        # ⚙ Configure — slate
        self._cfg_btn = QPushButton("\u2699", self)
        self._cfg_btn.setFixedSize(_icon_sz, _icon_sz)
        self._cfg_btn.setFlat(True)
        self._cfg_btn.setToolTip("Configure selected")
        self._cfg_btn.setStyleSheet(_btn_ss("#475569","#334155","#1e293b","#ffffff","#334155","#1e293b"))

        # 📈 Inspect — purple  (custom trend-chart icon)
        self._view_btn = QPushButton("", self)
        self._view_btn.setFixedSize(_icon_sz, _icon_sz)
        self._view_btn.setFlat(True)
        self._view_btn.setToolTip("Inspect / chart")
        self._view_btn.setIcon(_make_chart_icon(22, "#ffffff"))
        self._view_btn.setIconSize(QSize(22, 22))
        self._view_btn.setStyleSheet(_btn_ss("#7c3aed","#6d28d9","#5b21b6","#ffffff","#6d28d9","#5b21b6"))

        # ✓ Enable — green
        self._enable_btn = QPushButton("\u2713", self)
        self._enable_btn.setFixedSize(_icon_sz, _icon_sz)
        self._enable_btn.setFlat(True)
        self._enable_btn.setToolTip("Enable selected")
        self._enable_btn.setStyleSheet(_btn_ss("#16a34a","#15803d","#166534","#ffffff","#15803d","#166534"))

        # ✕ Disable — amber
        self._disable_btn = QPushButton("\u2715", self)
        self._disable_btn.setFixedSize(_icon_sz, _icon_sz)
        self._disable_btn.setFlat(True)
        self._disable_btn.setToolTip("Disable selected")
        self._disable_btn.setStyleSheet(_btn_ss("#d97706","#b45309","#92400e","#ffffff","#b45309","#92400e"))

        # − Remove — red
        self._remove_btn = QPushButton("\u2212", self)
        self._remove_btn.setFixedSize(_icon_sz, _icon_sz)
        self._remove_btn.setToolTip("Remove selected")
        self._remove_btn.setStyleSheet(_btn_ss("#dc2626","#b91c1c","#991b1b","#ffffff","#b91c1c","#991b1b"))

        self._remove_btn.clicked.connect(self._remove_selected)
        self._cfg_btn.clicked.connect(self._configure_selected)
        self._view_btn.clicked.connect(self._inspect_selected)
        self._enable_btn.clicked.connect(self._enable_selected)
        self._disable_btn.clicked.connect(self._disable_selected)

        bar.addWidget(self._add_btn)
        bar.addWidget(self._cfg_btn)
        bar.addWidget(self._view_btn)
        bar.addWidget(self._enable_btn)
        bar.addWidget(self._disable_btn)
        bar.addStretch(1)
        bar.addWidget(self._remove_btn)
        outer.addLayout(bar)

        # ── Splitter ──────────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background: #e5e7eb; }")
        outer.addWidget(splitter, 1)

        # ── Left: alert list ──────────────────────────────────────────────────
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)

        self._count_lbl = QLabel("", self)
        self._count_lbl.setStyleSheet(
            "QLabel { color: #6b7280; font-weight: 400; font-size: 11px; "
            "background: transparent; border: none; border-radius: 0; }"
        )
        lv.addWidget(self._count_lbl)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setItemDelegate(_AlertDelegate(self._list))
        self._list.setSpacing(0)
        self._list.setMouseTracking(True)
        self._list.setStyleSheet("""
            QListWidget {
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: #ffffff;
                outline: none;
            }
            QListWidget::item {
                border: none;
                padding: 0px;
                background: transparent;
            }
            QListWidget::item:selected { background: #dbeafe; }
            QListWidget::item:hover    { background: #f0f9ff; }
        """)
        self._list.currentRowChanged.connect(self._on_row_changed)
        lv.addWidget(self._list, 1)
        splitter.addWidget(left)

        # ── Right: detail card ────────────────────────────────────────────────
        right = QFrame(self)   # QFrame gets white bg + border from global QSS
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 16, 16, 16)
        rv.setSpacing(10)

        # Header: name + status pill
        hdr = QHBoxLayout()
        self._det_title = QLabel("Select an alert", self)
        self._det_title.setStyleSheet(
            "QLabel { font-size: 14px; font-weight: 700; "
            "background: transparent; border: none; border-radius: 0; }"
        )
        hdr.addWidget(self._det_title, 1)

        self._status_pill = QLabel("", self)
        self._status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_pill.setFixedHeight(24)
        self._status_pill.setContentsMargins(10, 0, 10, 0)
        self._status_pill.setMinimumWidth(64)
        self._status_pill.setStyleSheet(
            "QLabel { font-size: 10px; font-weight: 700; border-radius: 12px; "
            "background: transparent; "
            "padding: 0 10px; border: none; }"
        )
        hdr.addWidget(self._status_pill)
        rv.addLayout(hdr)

        rv.addWidget(_divider())

        # Detail rows (key + value pairs)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setSpacing(8)
        form.setContentsMargins(0, 4, 0, 4)

        self._det_kind       = _val_lbl()
        self._det_summary    = _val_lbl()
        self._det_rcpts      = _val_lbl()
        self._det_cooldown   = _val_lbl()
        self._det_interval   = _val_lbl()
        self._det_last_email = _val_lbl()

        form.addRow(_key_lbl("Type:"),        self._det_kind)
        form.addRow(_key_lbl("Thresholds:"),  self._det_summary)
        form.addRow(_key_lbl("Recipients:"),  self._det_rcpts)
        form.addRow(_key_lbl("Cooldown:"),    self._det_cooldown)
        form.addRow(_key_lbl("Check every:"), self._det_interval)
        form.addRow(_key_lbl("Last email:"),  self._det_last_email)
        rv.addLayout(form)
        rv.addStretch(1)

        # Empty-state hint shown when no alert is selected
        self._hint_lbl = QLabel(
            "No alert selected.\n\nClick an alert in the list to view its details, "
            "or use  ＋ Add Alert  to create one.",
            self,
        )
        self._hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_lbl.setWordWrap(True)
        self._hint_lbl.setStyleSheet(
            "QLabel { color: #9ca3af; font-size: 12px; font-weight: 400; "
            "background: transparent; border: none; border-radius: 0; }"
        )
        rv.addWidget(self._hint_lbl)

        splitter.addWidget(right)
        splitter.setSizes([300, 540])

        # ── Close ──
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._refresh_list()
        self._clear_details()

    # ── Host (lazy) ────────────────────────────────────────────────────────────
    def _get_host(self) -> Optional[_LiteHost]:
        if self._host is None:
            self._host = _build_host(self.db_path, self.table_name)
        return self._host

    # ── List management ────────────────────────────────────────────────────────
    def _refresh_list(self):
        current_row = self._list.currentRow()
        self._list.clear()

        for spec in self._specs:
            sym  = _kind_char(spec.kind)
            _raw = read_last_status(self.db_path, self.table_name, spec.id or spec.name)
            last = _raw or ("PENDING" if spec.enabled else "OFF")
            hex_  = _status_hex(last, spec.enabled)
            label = _status_pill_text(last, spec.enabled)
            name  = spec.name or spec.kind or "Alert"

            p_tmp = spec.payload or {}
            is_batt = (spec.kind == "Threshold") and (
                bool(p_tmp.get("is_battery", False)) or is_battery_column(str(p_tmp.get("column", "")))
            )

            item = QListWidgetItem()
            item.setData(_ROLE_ID,      spec.id)
            item.setData(_ROLE_HEX,     hex_)
            item.setData(_ROLE_CHAR,    sym)
            item.setData(_ROLE_NAME,    name)
            item.setData(_ROLE_KIND,    _kind_label(spec.kind))
            item.setData(_ROLE_STATLBL, label)
            item.setData(_ROLE_ENABLED, spec.enabled)
            item.setData(_ROLE_ISBATT,  is_batt)
            # Keep DisplayRole for accessibility / screen readers
            item.setText(f"{name} — {label}")
            self._list.addItem(item)

        n = len(self._specs)
        self._count_lbl.setText(
            f"{n} alert{'s' if n != 1 else ''}" if n else "No alerts configured"
        )

        if 0 <= current_row < self._list.count():
            self._list.setCurrentRow(current_row)

    def _selected_spec(self) -> Optional[AlertSpec]:
        row = self._list.currentRow()
        if 0 <= row < len(self._specs):
            return self._specs[row]
        return None

    def _save(self):
        save_specs(self.db_path, self.table_name, self._specs, self._timer_min,
                   dismissed_battery_cols=list(self._dismissed_battery_cols))
        self._sync_alerts_tab()

    def _sync_alerts_tab(self):
        """Push updated specs back to the in-memory AlertsTab (if running)."""
        try:
            p = self.parent()
            while p is not None:
                at = getattr(p, "alerts_tab", None)
                if at is not None and hasattr(at, "import_settings"):
                    from utils.alerts.store import read_current_settings
                    data = read_current_settings(self.db_path, self.table_name)
                    if data:
                        at.import_settings(data)
                    return
                p = p.parent() if callable(getattr(p, "parent", None)) else None
        except Exception:
            pass

    # ── Toolbar actions ────────────────────────────────────────────────────────
    def _add_alert(self, kind: str):
        handler = REGISTRY.get(kind)
        if not handler:
            return
        host = self._get_host()
        if host is None:
            QMessageBox.warning(self, "Add alert", "Could not load table data.")
            return
        spec = handler.default_spec(host)
        spec.id = str(uuid.uuid4())
        try:
            dlg = handler.create_editor(spec, host, self)
            if dlg.exec():
                spec.enabled = True
                self._specs.append(spec)
                self._save()
                self._refresh_list()
                self._list.setCurrentRow(len(self._specs) - 1)
        except Exception as e:
            QMessageBox.critical(self, "Add alert", str(e))

    def _remove_selected(self):
        spec = self._selected_spec()
        if spec is None:
            return
        if QMessageBox.question(
            self, "Remove alert",
            f"Remove '{spec.name or spec.kind}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            # Track dismissed battery columns so auto-registration skips them
            p = spec.payload or {}
            if spec.kind == "Threshold" and (
                bool(p.get("is_battery")) or is_battery_column(str(p.get("column", "")))
            ):
                col = str(p.get("column", ""))
                if col:
                    self._dismissed_battery_cols.add(col)
            self._specs.remove(spec)
            self._save()
            self._refresh_list()
            self._clear_details()

    def _configure_selected(self):
        spec = self._selected_spec()
        if spec is None:
            return
        handler = REGISTRY.get(spec.kind)
        if not handler:
            return
        host = self._get_host()
        if host is None:
            QMessageBox.warning(self, "Configure", "Could not load table data.")
            return
        try:
            dlg = handler.create_editor(spec, host, self)
            if dlg.exec():
                self._save()
                self._refresh_list()
                self._on_row_changed(self._list.currentRow())
        except Exception as e:
            QMessageBox.critical(self, "Configure", str(e))

    def _inspect_selected(self):
        spec = self._selected_spec()
        if spec is None:
            return
        handler = REGISTRY.get(spec.kind)
        if not handler or not hasattr(handler, "create_viewer"):
            QMessageBox.information(
                self, "Inspect", "No viewer available for this alert type."
            )
            return
        host = self._get_host()
        if host is None:
            QMessageBox.warning(self, "Inspect", "Could not load table data.")
            return

        self.configure_spec = lambda s: self._editor_bridge(s, host)
        try:
            dlg = handler.create_viewer(spec, host, self)
            if hasattr(dlg, "exec"):
                dlg.exec()
        finally:
            try:
                delattr(self, "configure_spec")
            except AttributeError:
                pass
        self._save()
        self._refresh_list()
        self._on_row_changed(self._list.currentRow())

    def _editor_bridge(self, spec: AlertSpec, host):
        handler = REGISTRY.get(spec.kind)
        if handler:
            try:
                handler.create_editor(spec, host, self).exec()
            except Exception:
                pass

    def _enable_selected(self):
        spec = self._selected_spec()
        if spec:
            spec.enabled = True
            self._save()
            self._refresh_list()
            self._on_row_changed(self._list.currentRow())

    def _disable_selected(self):
        spec = self._selected_spec()
        if spec:
            spec.enabled = False
            self._save()
            self._refresh_list()
            self._on_row_changed(self._list.currentRow())

    # ── Detail panel ───────────────────────────────────────────────────────────
    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self._specs):
            self._clear_details()
            return

        spec = self._specs[row]
        p    = spec.payload or {}

        self._det_title.setText(spec.name or spec.kind or "Alert")
        p_det = spec.payload or {}
        is_batt = (spec.kind == "Threshold") and (
            bool(p_det.get("is_battery", False)) or is_battery_column(str(p_det.get("column", "")))
        )
        kind_display = _kind_label(spec.kind)
        if is_batt:
            kind_display = f"\u26a1 Battery Threshold"
        self._det_kind.setText(kind_display)

        # Status pill with colour
        _raw = read_last_status(self.db_path, self.table_name, spec.id or spec.name)
        last  = _raw or ("PENDING" if spec.enabled else "OFF")
        hex_  = _status_hex(last, spec.enabled)
        label = _status_pill_text(last, spec.enabled)
        c = QColor(hex_)
        r, g, b = c.red(), c.green(), c.blue()
        self._status_pill.setStyleSheet(
            f"QLabel {{ font-size: 10px; font-weight: 700; border-radius: 12px; "
            f"background: rgba({r},{g},{b},35); color: {hex_}; "
            f"padding: 0 10px; min-width: 64px; "
            f"border: 1px solid rgba({r},{g},{b},80); }}"
        )
        self._status_pill.setText(label)

        # Threshold summary per type
        if spec.kind == "Stale":
            mode = "max historical gap" if p.get("scope_all") else "since last message"
            summary = (
                f"Amber ≥ {p.get('amber_min', 30)} min  ·  "
                f"Red ≥ {p.get('red_min', 60)} min\n"
                f"Mode: {mode}"
            )
        elif spec.kind == "Threshold":
            summary = (
                f"Column: {p.get('column', '?')}  ·  {p.get('mode', 'greater')}\n"
                f"Amber ≥ {p.get('amber', '?')}  ·  Red ≥ {p.get('red', '?')}"
            )
        elif spec.kind == "MissingData":
            summary = (
                f"Window: {p.get('window_minutes', 1440)} min\n"
                f"Amber < {p.get('amber_pct', 95)}%  ·  "
                f"Red < {p.get('red_pct', 80)}%"
            )
        else:
            summary = str(p)[:120]
        self._det_summary.setText(summary)

        rcpts = spec.recipients or p.get("recipients", [])
        self._det_rcpts.setText(", ".join(rcpts) if rcpts else "(none)")
        self._det_cooldown.setText(f"{p.get('email_cooldown_min', 240)} min")
        self._det_interval.setText(f"{p.get('interval_min', 15)} min")

        _, last_email_utc = read_last_email(
            self.db_path, self.table_name, spec.id or spec.name
        )
        self._det_last_email.setText(last_email_utc or "(never)")

        self._hint_lbl.hide()

    def _clear_details(self):
        self._det_title.setText("Select an alert")
        self._status_pill.setStyleSheet(
            "QLabel { font-size: 10px; font-weight: 700; border-radius: 12px; "
            "background: transparent; padding: 0 10px; border: none; }"
        )
        self._status_pill.setText("")
        for lbl in (self._det_kind, self._det_summary, self._det_rcpts,
                    self._det_cooldown, self._det_interval, self._det_last_email):
            lbl.setText("—")
        self._hint_lbl.show()

    # ── Uniquify helper (used by alert editors via parent._uniquify_name) ──────
    def _uniquify_name(self, desired: str, *, exclude_id: Optional[str] = None) -> str:
        base     = (desired or "Alert").strip()
        existing = {s.name for s in self._specs if s.name and s.id != exclude_id}
        name = base
        k = 2
        while name in existing:
            name = f"{base} ({k})"
            k += 1
        return name
