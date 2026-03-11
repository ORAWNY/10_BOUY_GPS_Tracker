"""utils/collapsible_section.py — reusable collapsible panel section."""
from __future__ import annotations
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)


class CollapsibleSection(QWidget):
    """
    A titled panel with a ▼/▶ arrow that shows or hides its body.

    Colours are defined in resource/styles/dark.qss and light.qss via the
    object-name selectors QFrame#cs_hdr and QFrame#cs_body — no hardcoded
    hex colours here so dark mode works automatically.

    Usage::

        section = CollapsibleSection("Email Parser — buoy.db")
        section.set_body(my_widget)
        layout.addWidget(section)
    """

    def __init__(self, title: str, parent=None, *, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────────
        self._hdr = QFrame(self)
        self._hdr.setObjectName("cs_hdr")
        self._hdr.setFixedHeight(34)
        self._hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        # No inline setStyleSheet — colours come from dark.qss / light.qss

        self._hdr_lay = QHBoxLayout(self._hdr)
        self._hdr_lay.setContentsMargins(10, 0, 10, 0)
        self._hdr_lay.setSpacing(8)

        self._arrow = QLabel("▼")
        self._arrow.setFixedWidth(14)
        # Only structural / non-colour properties; colour inherits from QSS
        self._arrow.setStyleSheet(
            "font-size: 10px; background: transparent; border: none;"
        )
        self._hdr_lay.addWidget(self._arrow)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(
            "font-weight: 600; font-size: 12px; background: transparent; border: none;"
        )
        self._hdr_lay.addWidget(self._title_lbl, 1)

        root.addWidget(self._hdr)

        # ── Body ────────────────────────────────────────────────────────────
        self._body = QFrame(self)
        self._body.setObjectName("cs_body")
        # No inline setStyleSheet — colours come from dark.qss / light.qss

        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        root.addWidget(self._body)

        self._hdr.mousePressEvent = lambda _e: self.toggle()

        if collapsed:
            self._collapsed = True
            self._body.hide()
            self._arrow.setText("▶")

    # ── Public API ───────────────────────────────────────────────────────────

    def set_title(self, title: str):
        self._title_lbl.setText(title)

    def set_body(self, widget: QWidget):
        """Replace body content."""
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._body_lay.addWidget(widget)

    def add_header_widget(self, widget: QWidget):
        """Add a widget to the right side of the header (e.g. a status pill)."""
        self._hdr_lay.addWidget(widget)

    def toggle(self):
        self._collapsed = not self._collapsed
        self._body.setVisible(not self._collapsed)
        self._arrow.setText("▶" if self._collapsed else "▼")

    def set_collapsed(self, collapsed: bool):
        if collapsed != self._collapsed:
            self.toggle()

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed
