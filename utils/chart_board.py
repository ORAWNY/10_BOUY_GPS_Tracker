"""utils/chart_board.py — robust chart dashboard with drag-and-drop reordering."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

import pandas as pd

from PyQt6.QtCore import Qt, QPoint, QByteArray, QMimeData, pyqtSignal
from PyQt6.QtGui import QAction, QDrag, QPainter, QColor, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QToolButton, QMenu, QSizePolicy, QFrame,
    QSplitter, QInputDialog, QToolBar, QApplication,
)

from utils.charts import ChartSpec, REGISTRY

# MIME type used for drag-and-drop between chart cards
_CHART_MIME = "application/x-chart-id"


# ── Chart card ────────────────────────────────────────────────────────────────

class ChartCardWidget(QFrame):
    """
    A styled frame containing a chart renderer with a compact header menu.

    Colours are defined in resource/styles/dark.qss and light.qss via the
    QFrame#chart_card and QFrame#card_hdr selectors — no hardcoded hex colours
    here so both themes work automatically.

    Drag-and-drop: grab the ⠿ handle in the header and drag onto any other
    card to reorder. A blue border highlights the drop target.
    """

    changed       = pyqtSignal()
    drop_requested = pyqtSignal(str, str)  # (dragged_spec_id, target_spec_id)

    def __init__(
        self,
        spec: ChartSpec,
        get_df: Callable,
        columns: List[str],
        get_df_full: Optional[Callable],
        parent=None,
    ):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns
        self.get_df_full = get_df_full

        self._is_drop_target = False
        self._drag_start: Optional[QPoint] = None

        self.setObjectName("chart_card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = QFrame(self)
        hdr.setObjectName("card_hdr")
        hdr.setFixedHeight(34)
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 0, 6, 0)
        hdr_lay.setSpacing(4)

        # Drag handle
        self._drag_hdl = QLabel("⠿")
        self._drag_hdl.setFixedWidth(16)
        self._drag_hdl.setCursor(Qt.CursorShape.SizeAllCursor)
        self._drag_hdl.setToolTip("Drag to reposition")
        self._drag_hdl.setStyleSheet(
            "font-size: 14px; background: transparent; border: none;"
        )
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
            "font-size: 10px; background: rgba(128,128,128,0.18); "
            "border-radius: 8px; padding: 1px 7px; border: none;"
        )
        hdr_lay.addWidget(self._kind_pill)

        self._menu_btn = QToolButton(hdr)
        self._menu_btn.setText("⋯")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; font-size: 16px; "
            "  border-radius: 5px; }"
            "QToolButton:hover { background: rgba(128,128,128,0.2); }"
        )
        self._menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu(self)
        self._act_cfg   = QAction("⚙  Configure…",          self)
        self._act_left  = QAction("←  Move Left",            self)
        self._act_right = QAction("→  Move Right",           self)
        self._act_up    = QAction("↑  Move to Row Above",    self)
        self._act_down  = QAction("↓  Move to Row Below",    self)
        self._act_above = QAction("⤒  New Row Above",        self)
        self._act_below = QAction("⤓  New Row Below",        self)
        self._act_rm    = QAction("✕  Remove",               self)

        menu.addAction(self._act_cfg)
        menu.addSeparator()
        menu.addActions([self._act_left, self._act_right,
                         self._act_up,   self._act_down])
        menu.addSeparator()
        menu.addActions([self._act_above, self._act_below])
        menu.addSeparator()
        menu.addAction(self._act_rm)

        self._menu_btn.setMenu(menu)
        hdr_lay.addWidget(self._menu_btn)
        root.addWidget(hdr)

        # ── Renderer ────────────────────────────────────────────────────────
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

        self._act_cfg.triggered.connect(self._configure)

    # ── Drag handle events ────────────────────────────────────────────────────

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
        # Scaled thumbnail as drag pixmap
        pix = self.grab().scaled(
            220, 130,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, 8))
        drag.exec(Qt.DropAction.MoveAction)

    # ── Drop target events ────────────────────────────────────────────────────

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
        """Remove hardcoded light-mode styles from embedded matplotlib toolbars."""
        try:
            for tb in self.findChildren(QToolBar):
                tb.setStyleSheet("")   # inherit from global QSS
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


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class _Item:
    spec: ChartSpec


@dataclass
class _Row:
    splitter: QSplitter
    items: List[_Item] = field(default_factory=list)


# ── Board ─────────────────────────────────────────────────────────────────────

class ChartBoard(QWidget):
    """
    Robust chart dashboard with drag-and-drop reordering.

    Layout
    ------
    Vertical QSplitter (self._vsplit)
      └─ Horizontal QSplitter per row  [objectName = "chart_row"]
           └─ ChartCardWidget  (sits directly in the row splitter)

    Drag-and-drop
    -------------
    Grab the ⠿ handle in any card header and drag it onto another card.
    The board swaps/moves positions using _rebuild_row() which is crash-safe:
    self._cards keeps live references so setParent(None) never GC's a card.

    Menu moves (←→↑↓ and new-row) remain available via the ⋯ card menu.
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
        self.get_df = get_df
        self.columns = columns
        self.get_df_full = get_df_full

        self.rows: List[_Row] = []
        self._cards: Dict[str, ChartCardWidget] = {}  # spec.id → live card

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar bar ───────────────────────────────────────────────────────
        bar_frame = QFrame(self)
        bar_frame.setObjectName("chart_board_bar")
        bar_frame.setFixedHeight(44)
        bar = QHBoxLayout(bar_frame)
        bar.setContentsMargins(12, 0, 12, 0)
        bar.setSpacing(8)

        bar_title = QLabel("Dashboard")
        bar_title.setStyleSheet(
            "font-weight: 600; font-size: 13px; background: transparent;"
        )
        bar.addWidget(bar_title)
        bar.addStretch(1)

        self._refresh_btn = self._mk_ghost_btn("⟳", "Refresh all charts")
        self._full_btn    = self._mk_ghost_btn("⛶", "Toggle full screen")
        self._add_btn     = QPushButton("＋  Add Chart")
        self._add_btn.setFixedHeight(30)
        self._add_btn.setToolTip("Add a new chart to the dashboard")
        self._add_btn.setStyleSheet(
            "QPushButton { font-size: 12px; font-weight: 600; color: #ffffff; "
            "  background: #2563eb; border: none; border-radius: 7px; padding: 4px 16px; }"
            "QPushButton:hover   { background: #1d4ed8; }"
            "QPushButton:pressed { background: #1e40af; }"
        )

        bar.addWidget(self._refresh_btn)
        bar.addWidget(self._add_btn)
        bar.addWidget(self._full_btn)
        root.addWidget(bar_frame)

        # ── Vertical splitter — fills all remaining space ──────────────────────
        # No QScrollArea wrapper: the splitter takes the full available height so
        # every row handle is freely draggable without fighting scroll geometry.
        self._vsplit = QSplitter(Qt.Orientation.Vertical)
        self._vsplit.setObjectName("chart_vsplit")
        self._vsplit.setChildrenCollapsible(False)
        self._vsplit.setHandleWidth(8)
        self._vsplit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self._vsplit, 1)

        # Empty-state placeholder (shown in place of vsplit when no charts)
        self._empty = QLabel(
            "No charts yet.\n\nClick  ＋ Add Chart  to get started."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            "font-size: 13px; padding: 60px; background: transparent;"
        )
        root.addWidget(self._empty, 1)

        # Hook toolbar buttons
        self._add_btn.clicked.connect(self._on_add)
        self._refresh_btn.clicked.connect(self.refresh_all)
        self._full_btn.clicked.connect(self._toggle_fullscreen)

        self._update_empty()

    # ── Styling helper ────────────────────────────────────────────────────────

    @staticmethod
    def _mk_ghost_btn(text: str, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(30)
        b.setToolTip(tip)
        b.setStyleSheet(
            "QPushButton { font-size: 14px; background: transparent; "
            "  border: 1px solid rgba(128,128,128,0.4); border-radius: 7px; padding: 2px 10px; }"
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
        """Compat shim — open the add-chart dialog."""
        self._on_add()

    def move_focused(self, direction: str):
        pass  # Moves are handled via the ⋯ card menu or drag-and-drop

    def export_state(self) -> Dict[str, Any]:
        return {
            "row_sizes": self._vsplit.sizes(),
            "rows": [
                {
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

        for rdata in data.get("rows") or []:
            row = self._add_row()
            for sd in rdata.get("charts") or []:
                try:
                    spec = ChartSpec.from_dict(sd)
                    self._attach(row, spec)
                except Exception:
                    pass
            sizes = rdata.get("col_sizes")
            if sizes and len(sizes) == row.splitter.count():
                row.splitter.setSizes(list(map(int, sizes)))

        rsz = data.get("row_sizes")
        if rsz and len(rsz) == self._vsplit.count():
            self._vsplit.setSizes(list(map(int, rsz)))

        self._update_empty()

    # ── Row management ────────────────────────────────────────────────────────

    def _add_row(self) -> _Row:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("chart_row")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        self._vsplit.addWidget(splitter)
        row = _Row(splitter=splitter)
        self.rows.append(row)
        self._equalise_rows()
        return row

    def _remove_row(self, ri: int):
        if not (0 <= ri < len(self.rows)):
            return
        row = self.rows.pop(ri)
        row.splitter.setParent(None)
        row.splitter.deleteLater()
        self._equalise_rows()
        self._update_empty()

    # ── Card management ───────────────────────────────────────────────────────

    def _attach(self, row: _Row, spec: ChartSpec):
        """Create a card for *spec*, append it to *row*."""
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
        """
        Detach every card from the splitter then re-add them in model order.

        self._cards keeps a live Python reference so setParent(None) does NOT
        destroy the widgets — it only un-parents them from the splitter.
        """
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

        new_splitter = QSplitter(Qt.Orientation.Horizontal)
        new_splitter.setObjectName("chart_row")
        new_splitter.setChildrenCollapsible(False)
        new_splitter.setHandleWidth(6)

        new_row = _Row(splitter=new_splitter)
        new_row.items.append(item)
        self.rows.insert(insert_at, new_row)
        self._vsplit.insertWidget(insert_at, new_splitter)

        card = self._cards.get(item.spec.id)
        if card is not None:
            new_splitter.addWidget(card)

        self._equalise_rows()
        self.changed.emit()

    def _on_drop(self, dragged_id: str, target_id: str):
        """Move dragged card to sit immediately before the target card."""
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
            # Same row — adjust for removal
            adj_ci = dst_ci if dst_ci < src_ci else dst_ci - 1
            src_row.items.insert(adj_ci, item)
            self._rebuild_row(src_row)
        else:
            # Different rows
            self._rebuild_row(src_row)
            if not src_row.items:
                self._remove_row(src_ri)
                if dst_ri > src_ri:
                    dst_ri -= 1
            dst_row = self.rows[dst_ri]
            # Re-locate target index after possible row removal
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
            row.splitter.setParent(None)
            row.splitter.deleteLater()
        self.rows.clear()

    def _update_empty(self):
        has_charts = any(r.items for r in self.rows)
        self._vsplit.setVisible(has_charts)
        self._empty.setVisible(not has_charts)

    def _equalise_cols(self, row: _Row):
        n = row.splitter.count()
        if n > 0:
            row.splitter.setSizes([10_000] * n)

    def _equalise_rows(self):
        n = self._vsplit.count()
        if n > 0:
            self._vsplit.setSizes([10_000] * n)

    def _toggle_fullscreen(self):
        w = self.window()
        if w.isFullScreen():
            w.showNormal()
            self._full_btn.setText("⛶")
        else:
            w.showFullScreen()
            self._full_btn.setText("🗗")
