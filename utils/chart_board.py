"""utils/chart_board.py — chart dashboard with hover overlays, row-resize handles, and DnD."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

import pandas as pd

from PyQt6.QtCore import Qt, QPoint, QByteArray, QMimeData, pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import QAction, QDrag, QPainter, QColor, QPen, QCursor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QToolButton, QMenu, QSizePolicy, QFrame,
    QSplitter, QInputDialog, QToolBar, QApplication, QScrollArea,
)

from utils.charts import ChartSpec, REGISTRY

_CHART_MIME = "application/x-chart-id"


# ── Chart card ────────────────────────────────────────────────────────────────

class ChartCardWidget(QFrame):
    """
    Chart card: renderer fills the card edge-to-edge.

    Controls (drag handle, title, menu) appear as a semi-transparent overlay
    whenever the mouse is anywhere inside the card — including over the
    matplotlib canvas child. This works via WA_Hover which fires HoverEnter /
    HoverLeave events even when child widgets are under the cursor.
    """

    changed        = pyqtSignal()
    drop_requested = pyqtSignal(str, str)

    _HDR_H = 36

    def __init__(
        self,
        spec: ChartSpec,
        get_df: Callable,
        columns: List[str],
        get_df_full: Optional[Callable],
        parent=None,
    ):
        super().__init__(parent)
        self.spec        = spec
        self.get_df      = get_df
        self.columns     = columns
        self.get_df_full = get_df_full

        self._is_drop_target = False
        self._drag_start: Optional[QPoint] = None

        self.setObjectName("chart_card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)
        # WA_Hover: Qt sends HoverEnter/HoverLeave even when a child has the mouse
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Renderer fills the card ──────────────────────────────────────────
        handler = REGISTRY.get(spec.chart_kind)
        if handler:
            self.renderer = handler.create_renderer(
                spec, get_df, columns, self, get_df_full
            )
        else:
            self.renderer = QLabel(f"Unknown chart type: {spec.chart_kind}")

        is_gis = getattr(self.renderer, "is_gis_renderer", False)
        if is_gis:
            self.renderer.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            root.addWidget(
                self.renderer,
                alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
        else:
            self.renderer.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            root.addWidget(self.renderer, 1)
            self._clear_toolbar_inline_style()

        # ── Hover overlay (floating, not in layout) ──────────────────────────
        self._hdr = QFrame(self)
        self._hdr.setObjectName("card_hdr_overlay")
        self._hdr.setFixedHeight(self._HDR_H)
        self._hdr.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        hdr_lay = QHBoxLayout(self._hdr)
        hdr_lay.setContentsMargins(6, 0, 6, 0)
        hdr_lay.setSpacing(4)

        self._drag_hdl = QLabel("\u2807")
        self._drag_hdl.setFixedWidth(16)
        self._drag_hdl.setCursor(Qt.CursorShape.SizeAllCursor)
        self._drag_hdl.setToolTip("Drag to reposition")
        self._drag_hdl.setStyleSheet("font-size: 14px; background: transparent; border: none;")
        self._drag_hdl.mousePressEvent = self._hdl_press
        self._drag_hdl.mouseMoveEvent  = self._hdl_move
        hdr_lay.addWidget(self._drag_hdl)

        self._title = QLabel(spec.title or spec.chart_kind)
        self._title.setStyleSheet(
            "font-weight: 600; font-size: 12px; background: transparent; border: none;"
        )
        hdr_lay.addWidget(self._title, 1)

        self._kind_pill = QLabel(spec.chart_kind)
        self._kind_pill.setStyleSheet(
            "font-size: 10px; background: rgba(128,128,128,0.22); "
            "border-radius: 8px; padding: 1px 7px; border: none;"
        )
        hdr_lay.addWidget(self._kind_pill)

        self._menu_btn = QToolButton(self._hdr)
        self._menu_btn.setText("\u22ef")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; font-size: 16px;"
            "  border-radius: 5px; }"
            "QToolButton:hover { background: rgba(128,128,128,0.25); }"
        )
        self._menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu(self)
        self._act_cfg   = QAction("\u2699  Configure\u2026",    self)
        self._act_left  = QAction("\u2190  Move Left",          self)
        self._act_right = QAction("\u2192  Move Right",         self)
        self._act_up    = QAction("\u2191  Move to Row Above",  self)
        self._act_down  = QAction("\u2193  Move to Row Below",  self)
        self._act_above = QAction("\u2912  New Row Above",      self)
        self._act_below = QAction("\u2913  New Row Below",      self)
        self._act_rm    = QAction("\u2715  Remove",             self)

        menu.addAction(self._act_cfg)
        menu.addSeparator()
        menu.addActions([self._act_left, self._act_right,
                         self._act_up,   self._act_down])
        menu.addSeparator()
        menu.addActions([self._act_above, self._act_below])

        if hasattr(self.renderer, "_toggle_fullscreen"):
            self._act_fs = QAction("\u26f6  Fullscreen", self)
            self._act_fs.triggered.connect(self.renderer._toggle_fullscreen)
            menu.addSeparator()
            menu.addAction(self._act_fs)

        if hasattr(self.renderer, "_toolbar_widget"):
            self._act_toolbar = QAction("\u2630  Toggle Controls", self)
            self._act_toolbar.triggered.connect(
                lambda: self.renderer._toolbar_widget.setVisible(
                    not self.renderer._toolbar_widget.isVisible()
                )
            )
            menu.addSeparator()
            menu.addAction(self._act_toolbar)

        menu.addSeparator()
        menu.addAction(self._act_rm)
        self._menu_btn.setMenu(menu)
        hdr_lay.addWidget(self._menu_btn)

        self._hdr.hide()

        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.timeout.connect(self._check_hide_hdr)

        self._act_cfg.triggered.connect(self._configure)

    # ── Hover via WA_Hover (fires even when child widgets have the mouse) ─────

    def event(self, ev: QEvent) -> bool:
        t = ev.type()
        if t == QEvent.Type.HoverEnter:
            self._leave_timer.stop()
            self._hdr.show()
            self._hdr.raise_()
        elif t == QEvent.Type.HoverLeave:
            self._leave_timer.start(350)
        return super().event(ev)

    def _check_hide_hdr(self):
        if self._menu_btn.menu() and self._menu_btn.menu().isVisible():
            self._leave_timer.start(200)
            return
        pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(pos):
            self._hdr.hide()

    # ── Overlay geometry ──────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._hdr.setGeometry(0, 0, self.width(), self._HDR_H)
        if self._hdr.isVisible():
            self._hdr.raise_()

    # ── Drag handle ───────────────────────────────────────────────────────────

    def _hdl_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()

    def _hdl_move(self, event):
        if self._drag_start is None:
            return
        if (event.pos() - self._drag_start).manhattanLength() < QApplication.startDragDistance():
            return
        self._drag_start = None
        self._start_drag()

    def _start_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_CHART_MIME, QByteArray(self.spec.id.encode()))
        drag.setMimeData(mime)
        pix = self.grab().scaled(
            220, 130,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, 8))
        drag.exec(Qt.DropAction.MoveAction)

    # ── Drop target ───────────────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_CHART_MIME):
            src_id = bytes(event.mimeData().data(_CHART_MIME)).decode()
            if src_id != self.spec.id:
                event.acceptProposedAction()
                self._is_drop_target = True
                self.update()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._is_drop_target = False
        self.update()

    def dropEvent(self, event):
        src_id = bytes(event.mimeData().data(_CHART_MIME)).decode()
        self._is_drop_target = False
        self.update()
        event.acceptProposedAction()
        self.drop_requested.emit(src_id, self.spec.id)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._is_drop_target:
            p = QPainter(self)
            pen = QPen(QColor("#2563eb"), 3)
            p.setPen(pen)
            p.drawRect(self.rect().adjusted(1, 1, -2, -2))
            p.end()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _clear_toolbar_inline_style(self):
        try:
            for tb in self.findChildren(QToolBar):
                tb.setStyleSheet("")
        except Exception:
            pass

    def refresh_data(self):
        if hasattr(self.renderer, "refresh_data"):
            self.renderer.refresh_data()

    def _configure(self):
        handler = REGISTRY.get(self.spec.chart_kind)
        if not handler:
            return
        dlg = handler.create_editor(self.spec, self.columns, self)
        if dlg.exec():
            self._title.setText(self.spec.title or self.spec.chart_kind)
            self.refresh_data()
            self.changed.emit()


# ── Row widget ────────────────────────────────────────────────────────────────

class _RowWidget(QFrame):
    """
    One dashboard row: a horizontal QSplitter holding chart cards, with a
    draggable resize handle at the bottom so users can set the row height
    independently.  Rows are stacked in a QScrollArea so the dashboard
    can grow beyond the window height.
    """

    DEFAULT_H = 340
    MIN_H     = 180
    HANDLE_H  = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chart_row_frame")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.DEFAULT_H)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("chart_row")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        lay.addWidget(self.splitter, 1)

        # Resize handle
        self._rh = QFrame(self)
        self._rh.setObjectName("row_resize_handle")
        self._rh.setFixedHeight(self.HANDLE_H)
        self._rh.setCursor(Qt.CursorShape.SizeVerCursor)
        self._rh.setToolTip("Drag to resize row")
        lay.addWidget(self._rh)

        self._drag_y: Optional[float] = None
        self._rh.mousePressEvent   = self._rh_press
        self._rh.mouseMoveEvent    = self._rh_move
        self._rh.mouseReleaseEvent = self._rh_release

    def _rh_press(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_y = ev.globalPosition().y()

    def _rh_move(self, ev):
        if self._drag_y is None:
            return
        dy = int(ev.globalPosition().y() - self._drag_y)
        self._drag_y = ev.globalPosition().y()
        self.setFixedHeight(max(self.MIN_H, self.height() + dy))

    def _rh_release(self, ev):
        self._drag_y = None


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class _Item:
    spec: ChartSpec


@dataclass
class _Row:
    widget: _RowWidget
    items: List[_Item] = field(default_factory=list)

    @property
    def splitter(self) -> QSplitter:
        return self.widget.splitter


# ── Board ─────────────────────────────────────────────────────────────────────

class ChartBoard(QWidget):
    """
    Dashboard of chart cards.

    Layout
    ------
    Fixed top bar  (38 px)
    QScrollArea
      └─ QVBoxLayout of _RowWidget instances
           └─ each row: horizontal QSplitter of ChartCardWidget + resize handle

    Scrolling
    ---------
    Rows have a fixed default height (340 px) that the user can change by
    dragging the handle at the bottom of each row.  The scroll area ensures
    the dashboard is usable no matter how many rows are added.

    Hover overlays
    --------------
    ChartCardWidget uses WA_Hover so its overlay fires even when the matplotlib
    canvas child has the cursor — no event-filter gymnastics needed.

    Drag-and-drop
    -------------
    Hover any card → the ⠿ handle appears → drag onto another card.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        get_df: Callable[[], pd.DataFrame],
        columns: List[str],
        get_df_full: Optional[Callable[[], pd.DataFrame]] = None,
        parent: Optional[QWidget] = None,
        initial_rows: int = 1,
    ):
        super().__init__(parent)
        self.get_df      = get_df
        self.columns     = columns
        self.get_df_full = get_df_full

        self.rows: List[_Row] = []
        self._cards: Dict[str, ChartCardWidget] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Compact top bar ───────────────────────────────────────────────────
        bar_frame = QFrame(self)
        bar_frame.setObjectName("chart_board_bar")
        bar_frame.setFixedHeight(38)
        bar = QHBoxLayout(bar_frame)
        bar.setContentsMargins(10, 0, 10, 0)
        bar.setSpacing(6)

        bar_title = QLabel("Dashboard")
        bar_title.setStyleSheet("font-weight: 600; font-size: 12px; background: transparent;")
        bar.addWidget(bar_title)
        bar.addStretch(1)

        self._refresh_btn = self._mk_ghost_btn("\u27f3", "Refresh all charts")
        self._full_btn    = self._mk_ghost_btn("\u26f6", "Toggle full screen")
        self._add_btn     = QPushButton("\uff0b  Add Chart")
        self._add_btn.setFixedHeight(26)
        self._add_btn.setToolTip("Add a new chart to the dashboard")
        self._add_btn.setStyleSheet(
            "QPushButton { font-size: 11px; font-weight: 600; color: #ffffff;"
            "  background: #2563eb; border: none; border-radius: 6px; padding: 3px 14px; }"
            "QPushButton:hover   { background: #1d4ed8; }"
            "QPushButton:pressed { background: #1e40af; }"
        )
        bar.addWidget(self._refresh_btn)
        bar.addWidget(self._add_btn)
        bar.addWidget(self._full_btn)
        root.addWidget(bar_frame)

        # ── Scroll area containing all rows ──────────────────────────────────
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._content = QWidget()
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(4)
        self._content_lay.addStretch(1)   # rows pile up from top; stretch fills remaining space

        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # Empty-state placeholder
        self._empty = QLabel("No charts yet.\n\nClick  \uff0b Add Chart  to get started.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("font-size: 13px; color: inherit; background: transparent;")
        # Overlay empty label over the scroll area
        self._empty.setParent(self._scroll)
        self._empty.hide()

        self._add_btn.clicked.connect(self._on_add)
        self._refresh_btn.clicked.connect(self.refresh_all)
        self._full_btn.clicked.connect(self._toggle_fullscreen)

        self._update_empty()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep empty label centred in the scroll area viewport
        if self._empty.isVisible():
            self._empty.setGeometry(0, 0,
                                    self._scroll.width(),
                                    self._scroll.height())

    # ── Styling helper ────────────────────────────────────────────────────────

    @staticmethod
    def _mk_ghost_btn(text: str, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(26)
        b.setToolTip(tip)
        b.setStyleSheet(
            "QPushButton { font-size: 13px; background: transparent;"
            "  border: 1px solid rgba(128,128,128,0.4); border-radius: 6px; padding: 2px 8px; }"
            "QPushButton:hover   { background: rgba(128,128,128,0.15); }"
            "QPushButton:pressed { background: rgba(128,128,128,0.3); }"
        )
        return b

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh_all(self):
        for card in self._cards.values():
            card.refresh_data()

    def disable_builtin_empty_message(self):
        self._empty.hide()

    def add_panel(self, direction: str):
        self._on_add()

    def move_focused(self, direction: str):
        pass

    def export_state(self) -> Dict[str, Any]:
        return {
            "rows": [
                {
                    "height": row.widget.height(),
                    "col_sizes": row.splitter.sizes(),
                    "charts": [it.spec.to_dict() for it in row.items],
                }
                for row in self.rows
            ],
        }

    def import_state(self, data: Dict[str, Any]):
        self._clear_all()
        if not data:
            self._update_empty()
            return

        # Back-compat: old format stored row_sizes at top level
        rows_data = data.get("rows") or []
        old_row_sizes = data.get("row_sizes") or []

        for i, rdata in enumerate(rows_data):
            row = self._add_row()
            for sd in rdata.get("charts") or []:
                try:
                    spec = ChartSpec.from_dict(sd)
                    self._attach(row, spec)
                except Exception:
                    pass
            col_sizes = rdata.get("col_sizes")
            if col_sizes and len(col_sizes) == row.splitter.count():
                row.splitter.setSizes(list(map(int, col_sizes)))
            # Restore saved row height
            h = rdata.get("height")
            if h:
                row.widget.setFixedHeight(max(_RowWidget.MIN_H, int(h)))
            elif old_row_sizes and i < len(old_row_sizes):
                row.widget.setFixedHeight(max(_RowWidget.MIN_H, int(old_row_sizes[i])))

        self._update_empty()

    # ── Row management ────────────────────────────────────────────────────────

    def _add_row(self) -> _Row:
        rw = _RowWidget(self._content)
        # Insert before the trailing stretch item
        self._content_lay.insertWidget(self._content_lay.count() - 1, rw)
        row = _Row(widget=rw)
        self.rows.append(row)
        return row

    def _insert_row_at(self, index: int) -> _Row:
        rw = _RowWidget(self._content)
        self._content_lay.insertWidget(index, rw)
        row = _Row(widget=rw)
        self.rows.insert(index, row)
        return row

    def _remove_row(self, ri: int):
        if not (0 <= ri < len(self.rows)):
            return
        row = self.rows.pop(ri)
        row.widget.setParent(None)
        row.widget.deleteLater()
        self._update_empty()

    # ── Card management ───────────────────────────────────────────────────────

    def _attach(self, row: _Row, spec: ChartSpec):
        card = self._make_card(spec)
        row.splitter.addWidget(card)
        row.items.append(_Item(spec=spec))
        self._equalise_cols(row)
        self._update_empty()

    def _make_card(self, spec: ChartSpec) -> ChartCardWidget:
        card = ChartCardWidget(
            spec, self.get_df, self.columns, self.get_df_full, self
        )
        card.changed.connect(self.changed)
        card.drop_requested.connect(self._on_drop)
        sid = spec.id
        card._act_rm.triggered.connect(
            lambda _=False, s=sid: self._remove(s))
        card._act_left.triggered.connect(
            lambda _=False, s=sid: self._move(s, "left"))
        card._act_right.triggered.connect(
            lambda _=False, s=sid: self._move(s, "right"))
        card._act_up.triggered.connect(
            lambda _=False, s=sid: self._move(s, "up"))
        card._act_down.triggered.connect(
            lambda _=False, s=sid: self._move(s, "down"))
        card._act_above.triggered.connect(
            lambda _=False, s=sid: self._move_new_row(s, "above"))
        card._act_below.triggered.connect(
            lambda _=False, s=sid: self._move_new_row(s, "below"))
        self._cards[spec.id] = card
        return card

    def _find(self, spec_id: str):
        for ri, row in enumerate(self.rows):
            for ci, it in enumerate(row.items):
                if it.spec.id == spec_id:
                    return ri, ci
        return -1, -1

    # ── Core rebuild ──────────────────────────────────────────────────────────

    def _rebuild_row(self, row: _Row):
        sizes = row.splitter.sizes()
        for i in range(row.splitter.count()):
            w = row.splitter.widget(i)
            if w is not None:
                w.setParent(None)
        for it in row.items:
            card = self._cards.get(it.spec.id)
            if card is not None:
                row.splitter.addWidget(card)
        if sizes and len(sizes) == row.splitter.count():
            row.splitter.setSizes(sizes)
        else:
            self._equalise_cols(row)

    # ── Operations ────────────────────────────────────────────────────────────

    def _remove(self, spec_id: str):
        ri, ci = self._find(spec_id)
        if ri < 0:
            return
        row = self.rows[ri]
        card = self._cards.pop(spec_id, None)
        if card:
            card.setParent(None)
            card.deleteLater()
        row.items.pop(ci)
        self._rebuild_row(row)
        if not row.items:
            self._remove_row(ri)
        self._update_empty()
        self.changed.emit()

    def _move(self, spec_id: str, direction: str):
        ri, ci = self._find(spec_id)
        if ri < 0:
            return
        row = self.rows[ri]

        if direction == "left" and ci > 0:
            row.items[ci], row.items[ci - 1] = row.items[ci - 1], row.items[ci]
            self._rebuild_row(row)

        elif direction == "right" and ci < len(row.items) - 1:
            row.items[ci], row.items[ci + 1] = row.items[ci + 1], row.items[ci]
            self._rebuild_row(row)

        elif direction in ("up", "down"):
            dst_ri = ri - 1 if direction == "up" else ri + 1
            if not (0 <= dst_ri < len(self.rows)):
                return
            item = row.items.pop(ci)
            self._rebuild_row(row)
            if not row.items:
                self._remove_row(ri)
                if dst_ri > ri:
                    dst_ri -= 1
            dst = self.rows[dst_ri]
            dst.items.append(item)
            card = self._cards.get(item.spec.id)
            if card is not None:
                dst.splitter.addWidget(card)
            self._equalise_cols(dst)

        self.changed.emit()

    def _move_new_row(self, spec_id: str, where: str):
        ri, ci = self._find(spec_id)
        if ri < 0:
            return
        row = self.rows[ri]
        item = row.items.pop(ci)
        self._rebuild_row(row)

        if not row.items:
            self._remove_row(ri)
            insert_at = ri
        else:
            insert_at = ri if where == "above" else ri + 1

        new_row = self._insert_row_at(insert_at)
        new_row.items.append(item)
        card = self._cards.get(item.spec.id)
        if card is not None:
            new_row.splitter.addWidget(card)

        self.changed.emit()

    def _on_drop(self, dragged_id: str, target_id: str):
        if dragged_id == target_id:
            return
        src_ri, src_ci = self._find(dragged_id)
        dst_ri, dst_ci = self._find(target_id)
        if src_ri < 0 or dst_ri < 0:
            return

        src_row = self.rows[src_ri]
        dst_row = self.rows[dst_ri]
        item    = src_row.items.pop(src_ci)

        if src_ri == dst_ri:
            adj_ci = dst_ci if dst_ci < src_ci else dst_ci - 1
            src_row.items.insert(adj_ci, item)
            self._rebuild_row(src_row)
        else:
            self._rebuild_row(src_row)
            if not src_row.items:
                self._remove_row(src_ri)
                if dst_ri > src_ri:
                    dst_ri -= 1
            dst_row = self.rows[dst_ri]
            actual_dst_ci = next(
                (ci for ci, it in enumerate(dst_row.items) if it.spec.id == target_id),
                len(dst_row.items),
            )
            dst_row.items.insert(actual_dst_ci, item)
            self._rebuild_row(dst_row)

        self._update_empty()
        self.changed.emit()

    # ── Add chart dialog ──────────────────────────────────────────────────────

    def _on_add(self):
        kinds = list(REGISTRY.keys())
        if not kinds:
            return
        kind, ok = QInputDialog.getItem(
            self, "New Chart", "Chart type:", kinds, 0, False
        )
        if not ok or not kind:
            return

        spec = ChartSpec(
            id=str(uuid.uuid4()),
            chart_kind=kind,
            title=f"{kind} Chart",
            payload={},
        )
        handler = REGISTRY.get(kind)
        if handler and hasattr(handler, "default_payload"):
            try:
                spec.payload = handler.default_payload(self.columns, self.get_df)
            except Exception:
                spec.payload = {}

        if not self.rows:
            self._add_row()
        self._attach(self.rows[-1], spec)
        self.changed.emit()

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def _clear_all(self):
        for card in list(self._cards.values()):
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        for row in list(self.rows):
            row.widget.setParent(None)
            row.widget.deleteLater()
        self.rows.clear()

    def _update_empty(self):
        has_charts = any(r.items for r in self.rows)
        self._scroll.setVisible(has_charts)
        self._empty.setVisible(not has_charts)
        if not has_charts:
            self._empty.setGeometry(0, 0, self._scroll.width(), self._scroll.height())
            self._empty.raise_()

    def _equalise_cols(self, row: _Row):
        n = row.splitter.count()
        if n > 0:
            row.splitter.setSizes([10_000] * n)

    def _toggle_fullscreen(self):
        w = self.window()
        if w.isFullScreen():
            w.showNormal()
            self._full_btn.setText("\u26f6")
        else:
            w.showFullScreen()
            self._full_btn.setText("\U0001f5d7")
