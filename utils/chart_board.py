# utils/chart_board.py
from __future__ import annotations
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Any

import pandas as pd

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QEvent, QPoint, QTimer, QMimeData
from PyQt6.QtGui import QAction, QDrag, QMouseEvent, QCursor, QGuiApplication, QPixmap, QPainter


from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton, QMenu,
    QSizePolicy, QFrame, QScrollArea, QSplitter
)

# Pull shared registry/types from your charts package
from utils.charts import ChartSpec, TypeHandlerBase, REGISTRY


# ---------------- Drag-and-drop MIME ----------------
_MIME_CARD = "application/x-chartcard-id"


# ---------------- DnD-enabled horizontal splitter (row) ----------------
class _DnDSplitter(QSplitter):
    """A row splitter that accepts drops of chart cards and computes an insert index."""
    def __init__(self, board: "ChartBoard", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.board = board
        self.setAcceptDrops(True)
        # Honor children size hints (important for GIS fixed-size)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(_MIME_CARD):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(_MIME_CARD):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        if not e.mimeData().hasFormat(_MIME_CARD):
            e.ignore()
            return
        card_id = bytes(e.mimeData().data(_MIME_CARD)).decode("utf-8", errors="ignore").strip()
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        insert_idx = self.count()
        for i in range(self.count()):
            w = self.widget(i)
            if not w:
                continue
            r = w.geometry()
            mid_x = r.left() + r.width() // 2
            if pos.x() < mid_x:
                insert_idx = i
                break

        # Accept now; do the heavy move after the drop returns to the event loop.
        e.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.board._dnd_move_card_to_row(card_id, self, insert_idx))


# ---------------- Card ----------------
class ChartCardWidget(QWidget):
    """A shell around a chart renderer with a compact settings ('…') menu and drag handle."""
    changed = pyqtSignal()
    removeRequested = pyqtSignal(object)         # emits self
    moveRequested = pyqtSignal(object, str)      # (self, 'left'|'right'|'up'|'down')
    newRowRequested = pyqtSignal(object, str)    # (self, 'above'|'below')

    def __init__(self,
                 spec: ChartSpec,
                 get_df: Callable[[], pd.DataFrame],
                 columns: List[str],
                 get_df_full: Optional[Callable[[], pd.DataFrame]],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns
        self.get_df_full = get_df_full
        self._press_pos: Optional[QPoint] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Cards expand by default; GIS renderer inside fixes its own size.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 0)
        self.title_lbl = QLabel(self.spec.title or spec.chart_kind)
        self.title_lbl.setStyleSheet("font-weight:600;")
        header.addWidget(self.title_lbl)

        header.addStretch(1)

        # Full header is the drag handle (plus these buttons)
        self.drag_hint = QLabel("⇅ drag")
        self.drag_hint.setStyleSheet("color:#888; font-size:11px;")
        header.addWidget(self.drag_hint)

        # “…” button with menu
        self.menu_btn = QToolButton(self)
        self.menu_btn.setText("…")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)

        act_cfg = QAction("Configure…", self)
        self.menu.addAction(act_cfg)
        self.menu.addSeparator()

        act_left = QAction("Move Left", self)
        act_right = QAction("Move Right", self)
        act_up = QAction("Move Up (row above)", self)
        act_down = QAction("Move Down (row below)", self)
        for a in (act_left, act_right, act_up, act_down):
            self.menu.addAction(a)

        act_new_above = QAction("Move to New Row Above", self)
        act_new_below = QAction("Move to New Row Below", self)
        self.menu.addSeparator()
        self.menu.addAction(act_new_above)
        self.menu.addAction(act_new_below)

        self.menu.addSeparator()
        act_rm = QAction("Remove", self)
        self.menu.addAction(act_rm)

        self.menu_btn.setMenu(self.menu)
        header.addWidget(self.menu_btn)
        root.addLayout(header)

        # Renderer from handler
        handler = REGISTRY.get(self.spec.chart_kind)
        if handler is None:
            self.renderer = QLabel(f"Unknown chart type: {self.spec.chart_kind}")
        else:
            self.renderer = handler.create_renderer(self.spec, self.get_df, self.columns, self, self.get_df_full)

            # NEW: enforce sizing so non-GIS charts always fit the cell
            self._apply_resizing_rules()

        # If renderer is GIS, don't stretch; else let it expand with the tab.
        if getattr(self.renderer, "is_gis_renderer", False):
            self.renderer.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            root.addWidget(self.renderer, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        else:
            self.renderer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            root.addWidget(self.renderer)



        # Wire menu
        act_cfg.triggered.connect(self._configure)
        act_left.triggered.connect(lambda: self.moveRequested.emit(self, "left"))
        act_right.triggered.connect(lambda: self.moveRequested.emit(self, "right"))
        act_up.triggered.connect(lambda: self.moveRequested.emit(self, "up"))
        act_down.triggered.connect(lambda: self.moveRequested.emit(self, "down"))
        act_new_above.triggered.connect(lambda: self.newRowRequested.emit(self, "above"))
        act_new_below.triggered.connect(lambda: self.newRowRequested.emit(self, "below"))
        act_rm.triggered.connect(lambda: self.removeRequested.emit(self))

    # --- Drag support (grab the header area) ---
    def _apply_resizing_rules(self):
        """
        GIS stays fixed-size (poster). All other charts must fit the cell:
        - ignore their sizeHint so they never force scrollbars
        - expand/shrink with the splitter cell
        """
        is_gis = bool(getattr(self.renderer, "is_gis_renderer", False))

        if is_gis:
            # GIS renderer decides its own fixed size (already Fixed inside GISRenderer/LocalGISViewer)
            self.renderer.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            # If the renderer exposes a canvas, leave it alone (GIS manages it)
        else:
            # Fit-to-cell: ignore size hints so layout can shrink/grow freely
            self.renderer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            # Common case: matplotlib canvas nested inside the renderer
            try:
                if hasattr(self.renderer, "canvas") and hasattr(self.renderer.canvas, "setSizePolicy"):
                    self.renderer.canvas.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            except Exception:
                pass

            # The card itself should be willing to expand/shrink
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.pos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._press_pos is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(e)
        if (e.pos() - self._press_pos).manhattanLength() < 6:
            return
        # Start a drag with our spec id
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_MIME_CARD, self.spec.id.encode("utf-8"))
        drag.setMimeData(mime)

        # Thumbnail pixmap (lightweight)
        pm = QPixmap(self.width(), 28)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.fillRect(0, 0, pm.width(), pm.height(), self.palette().window().color())
        painter.drawText(8, 18, self.title_lbl.text())
        painter.end()
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())

        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def _configure(self):
        handler = REGISTRY.get(self.spec.chart_kind)
        if not handler:
            return
        dlg = handler.create_editor(self.spec, self.columns, self)
        if dlg.exec():
            self.title_lbl.setText(self.spec.title or self.spec.chart_kind)
            if hasattr(self.renderer, "refresh_data"):
                self.renderer.refresh_data()
            self.changed.emit()

    def refresh_data(self):
        if hasattr(self.renderer, "refresh_data"):
            self.renderer.refresh_data()


# ---------------- Row model ----------------
@dataclass
class _Item:
    card: ChartCardWidget

@dataclass
class _Row:
    splitter: _DnDSplitter
    items: List[_Item]


# ---------------- Focus tracker ----------------
class _FocusFilter(QObject):
    """Event filter that informs the board when a card (or its container) is interacted with."""
    def __init__(self, board: "ChartBoard", card: "ChartCardWidget"):
        super().__init__(board)
        self.board = board
        self.card = card

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.FocusIn):
            self.board._set_focused_card(self.card)
        return False


# ---------------- Board with single outer scroll area ----------------
class ChartBoard(QWidget):
    """
    Dashboard board with nested splitters and a SINGLE outer QScrollArea,
    so the page scrolls as one regardless of how big some charts (e.g. GIS) are.
    """
    changed = pyqtSignal()

    def __init__(self,
                 get_df: Callable[[], pd.DataFrame],
                 columns: List[str],
                 get_df_full: Optional[Callable[[], pd.DataFrame]] = None,
                 parent: Optional[Widget] = None,
                 initial_rows: int = 1):
        super().__init__(parent)
        self.get_df = get_df
        self.columns = columns
        self.get_df_full = get_df_full

        self.rows: List[_Row] = []
        self._focused_card: Optional[ChartCardWidget] = None
        self._empty_fill: Optional[QWidget] = None
        self._cards_by_id: Dict[str, ChartCardWidget] = {}

        # Root layout just holds one scroll area + toolbar
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Toolbar (you may hide this elsewhere); add Full Screen toggle
        toolbar = QHBoxLayout()
        self.add_chart_btn = QPushButton("➕ Add Chart")
        self.add_row_btn = QPushButton("➕ Add Row")
        self.full_btn = QPushButton("⛶ Full Screen")
        toolbar.addWidget(QLabel("Charts"))
        toolbar.addStretch(1)
        toolbar.addWidget(self.add_row_btn)
        toolbar.addWidget(self.add_chart_btn)
        toolbar.addWidget(self.full_btn)
        root.addLayout(toolbar)

        self.full_btn.clicked.connect(self._toggle_fullscreen)

        # Scroll area that owns the content widget
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self.scroll)

        # Content widget inside the scroll area
        self.content = QWidget(self.scroll)
        self.content.setObjectName("chart_board_content")
        self.scroll.setWidget(self.content)
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(6)

        # Vertical splitter holds rows; let it honor child size hints
        self.vsplit = QSplitter(Qt.Orientation.Vertical, self.content)
        self.vsplit.setChildrenCollapsible(False)
        self.vsplit.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
        content_layout.addWidget(self.vsplit, stretch=0, alignment=Qt.AlignmentFlag.AlignTop)

        # Empty state (below splitter)
        self.empty_frame = QFrame(self.content)
        self.empty_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.empty_frame.setStyleSheet("color:#666;")
        ef_layout = QVBoxLayout(self.empty_frame)
        lbl = QLabel("No charts exist, click Add Chart to create a chart.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ef_layout.addWidget(lbl)
        content_layout.addWidget(self.empty_frame)

        # Seed rows
        for _ in range(max(0, int(initial_rows))):
            self._add_row_internal()

        self._update_empty_state()

        # Hook buttons
        self.add_row_btn.clicked.connect(lambda: (self._add_row_internal(), self._update_empty_state(), self.changed.emit()))
        self.add_chart_btn.clicked.connect(self._add_chart_dialog)

    # ---------- Public API ----------
    def move_focused(self, direction: str):
        card = self._focused_card
        if not card:
            return
        self._move_card_dir(card, direction)
        self.changed.emit()

    def disable_builtin_empty_message(self):
        if hasattr(self, "empty_frame") and self.empty_frame:
            self.empty_frame.hide()

    def add_panel(self, direction: str):
        card = self._focused_card
        if card:
            self.add_panel_relative_to(card, direction)
            return

        # No focused card: fallback behavior
        if not self.rows:
            row = self._add_row_internal()
            self._add_chart_via_dialog_into_row(row)
            self._update_empty_state()
            self.changed.emit()
            return

        if direction in ("left", "right"):
            row = self.rows[-1]
            if direction == "left":
                self._add_chart_via_dialog_into_row(row, at_index=0)
            else:
                self._add_chart_via_dialog_into_row(row, at_index=None)
        elif direction == "up":
            hsplit = _DnDSplitter(self, Qt.Orientation.Horizontal, self.vsplit)
            self.vsplit.insertWidget(0, hsplit)
            self.rows.insert(0, _Row(splitter=hsplit, items=[]))
            self._add_chart_via_dialog_into_row(self.rows[0])
        else:  # "down"
            row = self._add_row_internal()
            self._add_chart_via_dialog_into_row(row)

        self._update_empty_state()
        self.changed.emit()

    def refresh_all(self):
        for row in self.rows:
            for it in row.items:
                it.card.refresh_data()

    def export_state(self) -> Dict[str, Any]:
        return {
            "row_sizes": self.vsplit.sizes(),
            "rows": [
                {
                    "col_sizes": row.splitter.sizes(),
                    "charts": [it.card.spec.to_dict() for it in row.items],
                }
                for row in self.rows
            ],
        }

    def import_state(self, data: Dict[str, Any]):
        self._clear_all()
        if not data:
            self._update_empty_state()
            return

        rows_data = data.get("rows") or []
        for rdata in rows_data:
            row = self._add_row_internal()
            for spec_dict in (rdata.get("charts") or []):
                spec = ChartSpec.from_dict(spec_dict)
                self._add_spec_into_row(row, spec)
            sizes = rdata.get("col_sizes")
            if sizes and len(sizes) == row.splitter.count():
                row.splitter.setSizes(list(map(int, sizes)))

        rsz = data.get("row_sizes")
        if rsz and len(rsz) == self.vsplit.count():
            self.vsplit.setSizes(list(map(int, rsz)))

        self._update_empty_state()
        self.changed.emit()

    # ---------- Internals ----------
    def _toggle_fullscreen(self):
        w = self.window()
        if not hasattr(self, "_saved_geom"):
            self._saved_geom = None
        if not w.isFullScreen():
            try:
                self._saved_geom = w.saveGeometry()
            except Exception:
                self._saved_geom = None
            w.showFullScreen()
            self.full_btn.setText("🗗 Exit Full Screen")
        else:
            w.showNormal()
            try:
                if self._saved_geom:
                    w.restoreGeometry(self._saved_geom)
            except Exception:
                pass
            self.full_btn.setText("⛶ Full Screen")

    def _add_chart_dialog(self):
        if not self.rows:
            row = self._add_row_internal()
        else:
            row = self.rows[-1]
        self._add_chart_via_dialog_into_row(row)
        self._update_empty_state()
        self.changed.emit()

    def _add_chart_via_dialog_into_row(self, row: _Row, at_index: Optional[int] = None):
        from PyQt6.QtWidgets import QInputDialog
        kinds = list(REGISTRY.keys())
        kind, ok = QInputDialog.getItem(self, "New Chart", "Chart type:", kinds, 0, False)
        if not ok or not kind:
            return

        spec = ChartSpec(id=str(uuid.uuid4()), chart_kind=kind, title=f"{kind} Chart", payload={})
        handler: Optional[TypeHandlerBase] = REGISTRY.get(kind)
        if handler and hasattr(handler, "default_payload"):
            try:
                spec.payload = handler.default_payload(self.columns, self.get_df)
            except Exception:
                spec.payload = {}

        if at_index is None or at_index >= len(row.items):
            self._add_spec_into_row(row, spec)
            new_card = row.items[-1].card
        else:
            self._insert_spec_into_row(row, max(0, int(at_index)), spec)
            new_card = row.items[at_index].card

        self._set_focused_card(new_card)

    def _add_row_internal(self) -> _Row:
        hsplit = QSplitter(Qt.Orientation.Horizontal, self.vsplit)
        hsplit.setChildrenCollapsible(False)
        self.vsplit.addWidget(hsplit)
        row = _Row(splitter=hsplit, items=[])
        self.rows.append(row)
        # NEW:
        self._apply_board_stretch()
        return row

    def _add_spec_into_row(self, row: _Row, spec: ChartSpec):
        """Append a card to the end of `row`."""
        card = ChartCardWidget(spec, self.get_df, self.columns, self.get_df_full, self)
        self._wire_card(row, card)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(card)

        is_gis = bool(getattr(card.renderer, "is_gis_renderer", False))
        if is_gis:
            # Keep GIS's fixed poster height so it doesn't stretch the board
            h = int(card.renderer.sizeHint().height())
            container.setMinimumHeight(h)
            card.setMinimumHeight(h)
            # let width be governed by the chart's own Fixed policy
        else:
            # No minimums; fit inside the cell
            container.setMinimumHeight(0)
            card.setMinimumHeight(0)
            container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        container.installEventFilter(card._focus_filter)
        row.splitter.addWidget(container)
        row.items.append(_Item(card=card))

        # NEW: make all panels in this row share space
        self._apply_row_stretch(row)
        # and rows share height
        self._apply_board_stretch()

    def _insert_spec_into_row(self, row: _Row, index: int, spec: ChartSpec):
        """Insert a card at position `index` in `row`."""
        index = max(0, min(index, len(row.items)))
        card = ChartCardWidget(spec, self.get_df, self.columns, self.get_df_full, self)
        self._wire_card(row, card)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(card)

        is_gis = bool(getattr(card.renderer, "is_gis_renderer", False))
        if is_gis:
            h = int(card.renderer.sizeHint().height())
            container.setMinimumHeight(h)
            card.setMinimumHeight(h)
        else:
            container.setMinimumHeight(0)
            card.setMinimumHeight(0)
            container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        container.installEventFilter(card._focus_filter)
        row.items.insert(index, _Item(card=card))
        self._rebuild_row_widgets(row, extra_container=container)

        # NEW
        self._apply_row_stretch(row)
        self._apply_board_stretch()

    def _apply_row_stretch(self, row: _Row):
        """Give equal stretch to each panel in the row so they share width nicely."""
        try:
            for i in range(row.splitter.count()):
                row.splitter.setStretchFactor(i, 1)
        except Exception:
            pass

    def _apply_board_stretch(self):
        """Give equal stretch to each row so they share height nicely."""
        try:
            for i in range(self.vsplit.count()):
                self.vsplit.setStretchFactor(i, 1)
        except Exception:
            pass

    def _wire_card(self, row: _Row, card: ChartCardWidget):
        card.changed.connect(self.changed)
        card.removeRequested.connect(self._remove_card)
        card.moveRequested.connect(self._move_card_dir)
        card.newRowRequested.connect(self._move_card_to_new_row)
        filt = _FocusFilter(self, card)
        card.installEventFilter(filt)
        card._focus_filter = filt  # keep reference

    def _find_card(self, card: ChartCardWidget) -> Tuple[int, int]:
        for ri, row in enumerate(self.rows):
            for ci, it in enumerate(row.items):
                if it.card is card:
                    return ri, ci
        return -1, -1

    def _remove_card(self, card: ChartCardWidget):
        if self._focused_card is card:
            self._focused_card = None

        ri, ci = self._find_card(card)
        if ri < 0:
            self._update_empty_state()
            self.changed.emit()
            return

        row = self.rows[ri]
        cont = row.splitter.widget(ci)

        row.splitter.hide()
        if cont:
            try:
                row.splitter.removeWidget(cont)
            except Exception:
                pass
            cont.setParent(None)
            cont.deleteLater()
        try:
            row.items.pop(ci)
        except Exception:
            row.items = [it for k, it in enumerate(row.items) if k != ci]
        row.splitter.show()

        # remove id mapping
        try:
            del self._cards_by_id[card.spec.id]
        except Exception:
            pass

        if not row.items:
            self._remove_row(ri)

        self._update_empty_state()
        self.changed.emit()

    def _remove_row(self, ri: int):
        if not (0 <= ri < len(self.rows)):
            return
        row = self.rows[ri]
        self.vsplit.hide()
        wid = self.vsplit.widget(ri)
        if wid:
            try:
                self.vsplit.removeWidget(wid)
            except Exception:
                pass
            wid.setParent(None)
            wid.deleteLater()
        # drop model entry
        try:
            self.rows.pop(ri)
        except Exception:
            self.rows = [r for k, r in enumerate(self.rows) if k != ri]
        self.vsplit.show()

        # If all rows are gone, ensure filler so the tab stays tall
        self._update_empty_state()
        self._apply_board_stretch()

    def _move_card_dir(self, card: ChartCardWidget, direction: str):
        ri, ci = self._find_card(card)
        if ri < 0:
            return
        row = self.rows[ri]

        if direction == "left" and ci > 0:
            self._swap_in_row(row, ci, ci - 1)
        elif direction == "right" and ci < len(row.items) - 1:
            self._swap_in_row(row, ci, ci + 1)
        elif direction == "up" and ri > 0:
            self._take_from_row_to_row(ri, ci, ri - 1, at_index=None)
        elif direction == "down" and ri < len(self.rows) - 1:
            self._take_from_row_to_row(ri, ci, ri + 1, at_index=None)

        self._set_focused_card(card)
        self.changed.emit()

    def _move_card_to_new_row(self, card: ChartCardWidget, where: str):
        ri, ci = self._find_card(card)
        if ri < 0:
            return
        insert_at = ri if where == "above" else (ri + 1)
        hsplit = _DnDSplitter(self, Qt.Orientation.Horizontal, self.vsplit)
        self.vsplit.insertWidget(insert_at, hsplit)
        self.rows.insert(insert_at, _Row(splitter=hsplit, items=[]))
        src_ri = ri + 1 if where == "above" else ri
        self._take_from_row_to_row(src_ri, ci, insert_at, at_index=None)
        self._set_focused_card(card)
        self.changed.emit()

    def _swap_in_row(self, row: _Row, i: int, j: int):
        if i == j or not (0 <= i < len(row.items)) or not (0 <= j < len(row.items)):
            return
        sizes = row.splitter.sizes()
        row.items[i], row.items[j] = row.items[j], row.items[i]
        self._rebuild_row_widgets(row)
        if sizes and len(sizes) == row.splitter.count():
            row.splitter.setSizes(sizes)

    def _take_from_row_to_row(self, src_ri: int, src_ci: int, dst_ri: int, at_index: Optional[int]):
        if not (0 <= src_ri < len(self.rows)) or not (0 <= dst_ri < len(self.rows)):
            return
        src = self.rows[src_ri]
        dst = self.rows[dst_ri]
        if not (0 <= src_ci < len(src.items)):
            return

        cont = src.splitter.widget(src_ci)
        card = src.items[src_ci].card
        if cont is None:
            cont = card.parentWidget()
            if cont is None:
                return

        src.splitter.hide()
        try:
            src.splitter.removeWidget(cont)
        except Exception:
            pass
        cont.setParent(None)
        src.items.pop(src_ci)
        src.splitter.show()

        if at_index is None or at_index >= dst.splitter.count():
            dst.splitter.addWidget(cont)
            dst.items.append(_Item(card=card))
        else:
            dst.items.insert(at_index, _Item(card=card))
            self._rebuild_row_widgets(dst, extra_container=cont)

        if not src.items:
            self._remove_row(src_ri)

    def _rebuild_row_widgets(self, row: _Row, extra_container: Optional[QWidget] = None):
        containers: List[QWidget] = [row.splitter.widget(i) for i in range(row.splitter.count())]
        for w in containers:
            if w is not None:
                row.splitter.removeWidget(w)

        existing = {id(w): w for w in containers if w is not None}

        for it in row.items:
            cont = it.card.parentWidget()
            if cont is None or id(cont) not in existing:
                if extra_container is not None:
                    cont = extra_container
                    extra_container = None
                else:
                    wrapper = QWidget()
                    wrapper.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
                    v = QVBoxLayout(wrapper)
                    v.setContentsMargins(4, 4, 4, 4)
                    if getattr(it.card.renderer, "is_gis_renderer", False):
                        v.addWidget(it.card, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                    else:
                        v.addWidget(it.card)
                    cont = wrapper
            row.splitter.addWidget(cont)

        if extra_container is not None:
            row.splitter.addWidget(extra_container)

            # NEW:
        self._apply_row_stretch(row)
        self._apply_board_stretch()

    def _set_focused_card(self, card: Optional[ChartCardWidget]):
        self._focused_card = card

    def _clear_all(self):
        while self.rows:
            self._remove_row(len(self.rows) - 1)
        self._update_empty_state()  # ensures filler is present when empty

    def _update_empty_state(self):
        has_any = any(r.items for r in self.rows)
        self.empty_frame.setVisible(not has_any)
        self._ensure_fill_when_empty()

    def _ensure_fill_when_empty(self):
        """Ensure the board area keeps some height when there are no charts."""
        has_any = any(r.items for r in self.rows)
        if not has_any:
            if getattr(self, "_empty_fill", None) is None:
                filler = QWidget(self.vsplit)
                filler.setObjectName("board_empty_filler")
                filler.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
                filler.setMinimumHeight(280)
                self.vsplit.addWidget(filler)
                self._empty_fill = filler
        else:
            if getattr(self, "_empty_fill", None) is not None:
                self.vsplit.hide()
                try:
                    self.vsplit.removeWidget(self._empty_fill)
                except Exception:
                    pass
                self._empty_fill.setParent(None)
                self._empty_fill.deleteLater()
                self._empty_fill = None
                self.vsplit.show()

    # ---------- DnD glue for _DnDSplitter ----------
    def _dnd_move_card_to_row(self, card_id: str, target_splitter: _DnDSplitter, insert_idx: int):
        card = self._cards_by_id.get(card_id)
        if not card:
            return
        src_ri, src_ci = self._find_card(card)
        if src_ri < 0:
            return

        # Find destination row index
        dst_ri = next((i for i, row in enumerate(self.rows) if row.splitter is target_splitter), -1)
        if dst_ri < 0:
            return

        # Snapshot sizes to minimize layout jumps
        vsz_before = self.vsplit.sizes()
        src_row = self.rows[src_ri]
        src_sizes = src_row.splitter.sizes()
        dst_row = self.rows[dst_ri]
        dst_sizes = dst_row.splitter.sizes()

        if dst_ri == src_ri:
            # Same row move
            dst_index = insert_idx
            if src_ci < dst_index:
                dst_index = max(0, dst_index - 1)
            it = src_row.items.pop(src_ci)
            src_row.items.insert(min(dst_index, len(src_row.items)), it)
            self._rebuild_row_widgets(src_row)
            if src_sizes and len(src_sizes) == src_row.splitter.count():
                src_row.splitter.setSizes(src_sizes)
        else:
            # Cross-row move
            self._take_from_row_to_row(src_ri, src_ci, dst_ri, at_index=insert_idx)
            # Restore dst row sizes if possible
            if dst_sizes and len(dst_sizes) == dst_row.splitter.count():
                dst_row.splitter.setSizes(dst_sizes)

        # Restore vertical splitter sizes if possible
        if vsz_before and len(vsz_before) == self.vsplit.count():
            self.vsplit.setSizes(vsz_before)

        self._set_focused_card(card)
        self.changed.emit()

    # ---------- Helper from earlier ----------
    def _ensure_fill_when_empty(self):
        has_any = any(r.items for r in self.rows)
        if not has_any:
            if getattr(self, "_empty_fill", None) is None:
                filler = QWidget(self.vsplit)
                filler.setObjectName("board_empty_filler")
                filler.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
                filler.setMinimumHeight(280)
                self.vsplit.addWidget(filler)
                self._empty_fill = filler
        else:
            if getattr(self, "_empty_fill", None) is not None:
                self.vsplit.hide()
                try:
                    self.vsplit.removeWidget(self._empty_fill)
                except Exception:
                    pass
                self._empty_fill.setParent(None)
                self._empty_fill.deleteLater()
                self._empty_fill = None
                self.vsplit.show()
