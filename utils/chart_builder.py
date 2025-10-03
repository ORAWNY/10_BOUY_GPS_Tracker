# utils/chart_builder.py
from __future__ import annotations
import uuid
from typing import Callable, List, Dict, Any, Optional, Type
import pandas as pd
import traceback

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog, QInputDialog

# ✅ Import the base *only* (do NOT redefine these locally)
from utils.charts.base import ChartSpec, TypeHandlerBase

# ✅ Import handlers (these must import ONLY base.py)
from utils.charts.xy_chart import XYHandler
from utils.charts.pie_chart import PieHandler
from utils.charts.gauge_chart import GaugeHandler
from utils.charts.gis_chart import GISHandler


# ---------- Type registry ----------
REGISTRY: Dict[str, Type[TypeHandlerBase]] = {
    XYHandler.KIND: XYHandler,
    PieHandler.KIND: PieHandler,
    GaugeHandler.KIND: GaugeHandler,
    GISHandler.KIND: GISHandler,
}


# ---------- ChartCard (defensive renderer creation) ----------
class ChartCard(QWidget):
    changed = pyqtSignal()
    removeRequested = pyqtSignal(str)
    moveUpRequested = pyqtSignal(str)
    moveDownRequested = pyqtSignal(str)

    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame], columns: List[str],
                 parent=None, get_df_full: Optional[Callable[[], pd.DataFrame]] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.get_df_full = get_df_full
        self.columns = columns

        root = QVBoxLayout(self)
        hdr = QHBoxLayout()
        self.title_lbl = QLabel(self.spec.title); self.title_lbl.setStyleSheet("font-weight:600;")
        hdr.addWidget(self.title_lbl); hdr.addStretch(1)
        self.btn_cfg = QPushButton("Configure…")
        self.btn_up = QPushButton("↑"); self.btn_down = QPushButton("↓")
        self.btn_remove = QPushButton("Remove")
        for b in (self.btn_cfg, self.btn_up, self.btn_down, self.btn_remove): hdr.addWidget(b)
        root.addLayout(hdr)

        handler = REGISTRY.get(self.spec.chart_kind)
        if handler is None:
            self.renderer = QLabel(f"Unknown chart type: {self.spec.chart_kind}")
        else:
            try:
                self.renderer = handler.create_renderer(self.spec, self.get_df, self.columns, self, self.get_df_full)
            except Exception as e:
                traceback.print_exc()
                self.renderer = QLabel(f"{self.spec.chart_kind} error: {e}")
        root.addWidget(self.renderer)

        self.btn_cfg.clicked.connect(self._open_editor)
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self.spec.id))
        self.btn_up.clicked.connect(lambda: self.moveUpRequested.emit(self.spec.id))
        self.btn_down.clicked.connect(lambda: self.moveDownRequested.emit(self.spec.id))

        self._refresh_title()

    def _refresh_title(self):
        disp = self.spec.title or REGISTRY.get(self.spec.chart_kind, TypeHandlerBase).display_name()
        self.title_lbl.setText(disp)

    def _open_editor(self):
        handler = REGISTRY.get(self.spec.chart_kind)
        if not handler:
            return
        try:
            dlg = handler.create_editor(self.spec, self.columns, self)
        except Exception as e:
            traceback.print_exc()
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Editor error", f"{self.spec.chart_kind} editor failed:\n{e}")
            return

        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Attempt to refresh renderer; if it fails, show label instead of crashing
            try:
                if hasattr(self.renderer, "refresh_data"):
                    self.renderer.refresh_data()
            except Exception as e:
                traceback.print_exc()
                repl = QLabel(f"{self.spec.chart_kind} refresh error: {e}")
                self.layout().replaceWidget(self.renderer, repl)
                self.renderer.setParent(None); self.renderer.deleteLater()
                self.renderer = repl
            self._refresh_title()
            self.changed.emit()

    def refresh_data(self):
        if hasattr(self.renderer, "refresh_data"):
            try:
                self.renderer.refresh_data()
            except Exception:
                traceback.print_exc()


# ---------- ChartManager (unchanged except add_item creation) ----------
class ChartManager(QWidget):
    changed = pyqtSignal()

    def __init__(self, get_df: Callable[[], pd.DataFrame], columns: List[str],
                 get_df_full: Optional[Callable[[], pd.DataFrame]] = None, parent=None):
        super().__init__(parent)
        self.get_df = get_df
        self.get_df_full = get_df_full
        self.columns = columns
        self.cards: List[ChartCard] = []

        self.root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.add_btn = QPushButton("➕ Add Chart")
        toolbar.addWidget(QLabel("Custom Charts")); toolbar.addStretch(1); toolbar.addWidget(self.add_btn)
        self.root.addLayout(toolbar)

        self.container = QVBoxLayout(); self.container.setSpacing(12)
        self.root.addLayout(self.container); self.root.addStretch(1)

        self.add_btn.clicked.connect(self._add_chart_interactive)

    def refresh_all(self):
        for c in self.cards: c.refresh_data()

    def export_settings(self) -> Dict[str, Any]:
        return {"charts": [c.spec.to_dict() for c in self.cards]}

    def import_settings(self, data: Dict[str, Any]):
        self._clear_all()
        charts = (data or {}).get("charts") or []
        for d in charts:
            spec = ChartSpec.from_dict(d)  # handled by base
            self._add_card(spec)
        self.changed.emit()

    def _add_chart_interactive(self):
        kinds = list(REGISTRY.keys())
        kind, ok = QInputDialog.getItem(self, "New Chart", "Chart type:", kinds, 0, False)
        if not ok or not kind:
            return
        spec = ChartSpec(id=str(uuid.uuid4()), chart_kind=kind, title=f"{kind} Chart")
        handler = REGISTRY.get(kind)
        if handler and hasattr(handler, "default_payload"):
            try:
                spec.payload = handler.default_payload(self.columns, self.get_df)
            except Exception:
                traceback.print_exc()
                spec.payload = {}
        self._add_card(spec); self.changed.emit()

    def _add_card(self, spec: ChartSpec):
        card = ChartCard(spec, self.get_df, self.columns, self, get_df_full=self.get_df_full)
        card.changed.connect(self.changed)
        card.removeRequested.connect(self._remove_card_by_id)
        card.moveUpRequested.connect(self._move_up)
        card.moveDownRequested.connect(self._move_down)
        self.cards.append(card); self.container.addWidget(card)

    def _remove_card_by_id(self, cid: str):
        for i, c in enumerate(self.cards):
            if c.spec.id == cid:
                c.setParent(None); c.deleteLater(); self.cards.pop(i); self.changed.emit(); return

    def _move_up(self, cid: str):
        idx = next((i for i, c in enumerate(self.cards) if c.spec.id == cid), -1)
        if idx > 0:
            self.cards[idx], self.cards[idx-1] = self.cards[idx-1], self.cards[idx]
            self._rebuild_order(); self.changed.emit()

    def _move_down(self, cid: str):
        idx = next((i for i, c in enumerate(self.cards) if c.spec.id == cid), -1)
        if 0 <= idx < len(self.cards)-1:
            self.cards[idx], self.cards[idx+1] = self.cards[idx+1], self.cards[idx]
            self._rebuild_order(); self.changed.emit()

    def _rebuild_order(self):
        for i in reversed(range(self.container.count())):
            item = self.container.itemAt(i); w = item.widget()
            if w: self.container.removeWidget(w)
        for c in self.cards: self.container.addWidget(c)

    def _clear_all(self):
        for c in self.cards: c.setParent(None); c.deleteLater()
        self.cards.clear()
