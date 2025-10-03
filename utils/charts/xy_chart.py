# utils/charts/xy_chart.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional

import numpy as np
import pandas as pd
import matplotlib.dates as mdates
from matplotlib.backends.backend_qtagg import (
    FigureCanvasQTAgg as FigureCanvas,
    NavigationToolbar2QT as NavigationToolbar,
)
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QThread, qInstallMessageHandler, QtMsgType
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QDialog, QHBoxLayout, QTabWidget,
    QLabel, QComboBox, QCheckBox, QLineEdit, QPushButton, QSpinBox,
    QDoubleSpinBox, QGroupBox, QColorDialog, QScrollArea, QDialogButtonBox,
    QSizePolicy, QFrame, QToolButton, QApplication, QMessageBox
)

# IMPORTANT: import from the package so @register writes into the shared REGISTRY
from utils.charts import register, TypeHandlerBase, ChartSpec

# --------------------- Logging ---------------------
import logging
logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
    _h.setFormatter(_fmt)
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# ---- CRASH VISIBILITY ----
import os, sys, faulthandler, traceback, tempfile, datetime
_CRASH_LOG = os.path.join(tempfile.gettempdir(), "xy_chart_crash.log")

def _append_crash_log(msg: str):
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass

try:
    if not faulthandler.is_enabled():
        faulthandler.enable(file=open(_CRASH_LOG, "a"))
        _append_crash_log("faulthandler enabled")
except Exception:
    pass

def _qt_message_handler(mode, context, message):
    level = {QtMsgType.QtInfoMsg: "INFO",
             QtMsgType.QtDebugMsg: "DEBUG",
             QtMsgType.QtWarningMsg: "WARNING",
             QtMsgType.QtCriticalMsg: "CRITICAL",
             QtMsgType.QtFatalMsg: "FATAL"}.get(mode, "UNKNOWN")
    line = f"[QT {level}] {message} (file={context.file}, line={context.line}, func={context.function})"
    _append_crash_log(line)
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass
    if mode == QtMsgType.QtFatalMsg:
        faulthandler.dump_traceback(file=open(_CRASH_LOG, "a"))
qInstallMessageHandler(_qt_message_handler)

def _global_excepthook(exc_type, exc, tb):
    _append_crash_log("Uncaught exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.__excepthook__(exc_type, exc, tb)
sys.excepthook = _global_excepthook

def _unraisablehook(unraisable):
    _append_crash_log("Unraisable exception:\n" + "".join(traceback.format_exception(unraisable.exc_type,
                                                                                     unraisable.exc_value,
                                                                                     unraisable.exc_traceback)))
    sys.__unraisablehook__(unraisable)
sys.unraisablehook = _unraisablehook

_LOG_LEVEL = os.getenv("XY_CHART_LOG", "INFO").upper()
logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
_append_crash_log(f"xy_chart logger level set to: {_LOG_LEVEL}")
# ---- END CRASH VISIBILITY ----

# Helper to open the editor safely from anywhere
def show_xy_editor_safely(spec: ChartSpec, columns: list[str], parent: QWidget | None = None) -> int:
    """
    Opens XYEditor safely on the GUI thread; returns QDialog result.
    Writes detailed errors to _CRASH_LOG instead of letting Qt hard-crash.
    """
    app = QApplication.instance()
    if app is None:
        _append_crash_log("ERROR: QApplication.instance() is None")
        return 0

    ret_container = {"ret": 0}

    def _open():
        try:
            dlg = XYEditor(spec, columns, parent)
            dlg.setObjectName("XYEditorDialog")
            ret_container["ret"] = dlg.exec()
        except Exception:
            _append_crash_log("Exception while showing XYEditor:\n" +
                              "".join(traceback.format_exc()))
            try:
                QMessageBox.critical(parent, "XY Editor Error",
                                     "An error occurred while opening the editor.\n"
                                     "Details were written to:\n" + _CRASH_LOG)
            except Exception:
                pass

    if QThread.currentThread() is app.thread():
        _open()
    else:
        _append_crash_log("show_xy_editor_safely: invoked off the GUI thread; bouncing via QTimer")
        QTimer.singleShot(0, _open)
        app.processEvents()
    return ret_container["ret"]

# --------------------- Utilities ---------------------
def _default_theme() -> Dict[str, Any]:
    return {
        "facecolor": "#ffffff",
        "axes_facecolor": "#ffffff",
        "grid": True,
        "grid_linestyle": "-",
        "grid_color": "#e5e7eb",
        "grid_alpha": 1.0,
        "spines_color": "#cccccc",
        "title_size": 12,
        "title_color": "#111111",
        "axis_label_color": "#111111",
        "label_size": 10,
        "tick_size": 9,
        "date_format": "auto",
        "tight_layout": True,

        # default paddings
        "x_pad_frac": 0.05,   # 10% of X range
        "y_pad_frac": 0.05,   # 10% of Y range
    }


def _ensure_payload_defaults(spec: ChartSpec, columns: List[str]) -> None:
    p = spec.payload
    # prefer __dt_iso if present
    preferred_x = "__dt_iso" if "__dt_iso" in columns else (columns[0] if columns else "")
    p.setdefault("x_col", preferred_x)
    default_y = next((c for c in columns if c != p["x_col"]), (columns[0] if columns else ""))
    p.setdefault("series", [{
        "y_col": default_y,
        "label": default_y,
        "style": "Line",
        "marker": "",
        "size": 24.0,
        "linewidth": 2.0,
        "alpha": 1.0,
        "linestyle": "-",
        "color": "",
        "symbol_color": "",
        "y_axis": "left",
        "fill": False,
    }])
    p.setdefault("legend", True)
    p.setdefault("title", spec.title or "XY Chart")
    p.setdefault("x_label", "")
    p.setdefault("y_left_label", "")
    p.setdefault("y_right_label", "")
    p.setdefault("style", _default_theme())

# --------------------- Small ui helper ---------------------
class _ColorButton(QWidget):
    """
    Compact color picker that shows a swatch and the hex text.
    Use .value() / .setValue(hex) for get/set.
    """
    def __init__(self, initial: str = "#000000", parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._btn = QPushButton(" ")
        self._btn.setFixedWidth(28)
        self._btn.setFixedHeight(22)
        self._edit = QLineEdit(initial)
        self._edit.setPlaceholderText("#rrggbb")
        self._edit.textChanged.connect(self._sync_btn)
        self._btn.clicked.connect(self._choose)
        lay.addWidget(self._btn)
        lay.addWidget(self._edit, 1)
        self._sync_btn()

    def _sync_btn(self):
        txt = self._edit.text().strip() or "#000000"
        col = QColor(txt)
        if not col.isValid():
            col = QColor("#000000")
        self._btn.setStyleSheet(f"background-color: {col.name()}; border: 1px solid #888;")

    def _choose(self):
        col = QColor(self._edit.text().strip() or "#000000")
        if not col.isValid():
            col = QColor("#000000")
        chosen = QColorDialog.getColor(col, self, "Pick color")
        if chosen.isValid():
            self._edit.setText(chosen.name())

    def value(self) -> str:
        return (self._edit.text().strip() or "").lower()

    def setValue(self, hexval: str):
        self._edit.setText(hexval or "")

def _scrollable(widget: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidget(widget)
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    sa.setFrameShape(QFrame.Shape.NoFrame)  # PyQt6 API
    return sa

# --------------------- Editor ---------------------
_MARKER_CHOICES = [
    ("None", ""),
    ("Circle (o)", "o"),
    ("Square (s)", "s"),
    ("Triangle Up (^)", "^"),
    ("Triangle Down (v)", "v"),
    ("Diamond (D)", "D"),
    ("Thin Diamond (d)", "d"),
    ("Plus (+)", "+"),
    ("Cross (x)", "x"),
    ("Star (*)", "*"),
    ("Pentagon (p)", "p"),
    ("Hexagon (h)", "h"),
    ("Pixel (,)", ","),
    ("Point (.)", "."),
    ("Triangle Left (<)", "<"),
    ("Triangle Right (>)", ">"),
    ("Tristar (1)", "1"),
    ("Tristar (2)", "2"),
    ("Tristar (3)", "3"),
    ("Tristar (4)", "4"),
]

class _SeriesRow(QWidget):
    def __init__(self, columns: List[str], initial: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self); outer.setContentsMargins(6, 6, 6, 6); outer.setSpacing(4)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        outer.addLayout(form)

        # Core selectors
        self.y_combo = QComboBox(self); self.y_combo.addItems(columns)
        self.y_combo.setCurrentText(initial.get("y_col", columns[0] if columns else ""))
        form.addRow("Y data column:", self.y_combo)

        self.label_edit = QLineEdit(initial.get("label", ""))
        form.addRow("Legend label (display name):", self.label_edit)

        # Style row
        row1 = QWidget(); r1 = QHBoxLayout(row1); r1.setContentsMargins(0,0,0,0); r1.setSpacing(6)
        self.style_combo = QComboBox(self); self.style_combo.addItems(["Line", "Scatter", "Bar"])
        self.style_combo.setCurrentText(initial.get("style", "Line"))
        self.marker_combo = QComboBox(self)
        for name, code in _MARKER_CHOICES: self.marker_combo.addItem(name, code)
        init_marker = initial.get("marker", "")
        found_idx = max(0, next((i for i in range(self.marker_combo.count())
                                 if self.marker_combo.itemData(i) == init_marker), 0))
        self.marker_combo.setCurrentIndex(found_idx)
        self.linestyle_combo = QComboBox(self); self.linestyle_combo.addItems(["-", "--", ":", "-.", "none"])
        self.linestyle_combo.setCurrentText(initial.get("linestyle", "-"))
        r1.addWidget(QLabel("Series style:")); r1.addWidget(self.style_combo)
        r1.addSpacing(8)
        r1.addWidget(QLabel("Marker shape:")); r1.addWidget(self.marker_combo)
        r1.addSpacing(8)
        r1.addWidget(QLabel("Line style:")); r1.addWidget(self.linestyle_combo)
        form.addRow("Appearance:", row1)

        # Sizes / opacity
        row2 = QWidget(); r2 = QHBoxLayout(row2); r2.setContentsMargins(0,0,0,0); r2.setSpacing(6)
        self.lw_spin = QDoubleSpinBox(self); self.lw_spin.setRange(0.1, 20.0); self.lw_spin.setSingleStep(0.1)
        self.lw_spin.setValue(float(initial.get("linewidth", 2.0)))
        self.sz_spin = QDoubleSpinBox(self); self.sz_spin.setRange(1.0, 200.0); self.sz_spin.setSingleStep(1.0)
        self.sz_spin.setValue(float(initial.get("size", 24.0)))
        self.alpha_spin = QDoubleSpinBox(self); self.alpha_spin.setRange(0.0, 1.0); self.alpha_spin.setSingleStep(0.05)
        self.alpha_spin.setValue(float(initial.get("alpha", 1.0)))
        r2.addWidget(QLabel("Line width:")); r2.addWidget(self.lw_spin)
        r2.addSpacing(8)
        r2.addWidget(QLabel("Marker size:")); r2.addWidget(self.sz_spin)
        r2.addSpacing(8)
        r2.addWidget(QLabel("Opacity (0–1):")); r2.addWidget(self.alpha_spin)
        form.addRow("Sizes & opacity:", row2)

        # Colors
        row3 = QWidget(); r3 = QHBoxLayout(row3); r3.setContentsMargins(0,0,0,0); r3.setSpacing(6)
        self.color_btn = _ColorButton(initial.get("color", ""))
        self.symbol_color_btn = _ColorButton(initial.get("symbol_color", ""))
        r3.addWidget(QLabel("Line color:")); r3.addWidget(self.color_btn)
        r3.addSpacing(8)
        r3.addWidget(QLabel("Marker color:")); r3.addWidget(self.symbol_color_btn)
        form.addRow("Colors:", row3)

        # Axis & fill
        row4 = QWidget(); r4 = QHBoxLayout(row4); r4.setContentsMargins(0,0,0,0); r4.setSpacing(6)
        self.axis_combo = QComboBox(self); self.axis_combo.addItems(["left", "right"])
        self.axis_combo.setCurrentText(initial.get("y_axis", "left"))
        self.fill_check = QCheckBox("Fill area under line")
        self.fill_check.setChecked(bool(initial.get("fill", False)))
        r4.addWidget(QLabel("Y axis:")); r4.addWidget(self.axis_combo)
        r4.addSpacing(12)
        r4.addWidget(self.fill_check)
        form.addRow("Axis & fill:", row4)

        # Row actions
        self.remove_btn = QPushButton("Remove series")
        self.remove_btn.setObjectName("remove_series_btn")
        outer.addWidget(self.remove_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "y_col": self.y_combo.currentText(),
            "label": self.label_edit.text().strip(),
            "style": self.style_combo.currentText(),
            "marker": (self.marker_combo.currentData() or ""),
            "size": float(self.sz_spin.value()),
            "linewidth": float(self.lw_spin.value()),
            "alpha": float(self.alpha_spin.value()),
            "linestyle": self.linestyle_combo.currentText(),
            "color": self.color_btn.value(),
            "symbol_color": self.symbol_color_btn.value(),
            "y_axis": self.axis_combo.currentText(),
            "fill": self.fill_check.isChecked(),
        }

class XYEditor(QDialog):
    def __init__(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("XY Chart")
        self.setModal(True)
        self.setSizeGripEnabled(True)

        self.spec = spec
        self.columns = columns
        _ensure_payload_defaults(self.spec, columns)

        p = self.spec.payload
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # ---- Series tab (scrollable) ----
        series_tab_inner = QWidget()
        series_lay = QVBoxLayout(series_tab_inner)
        series_lay.setContentsMargins(0, 0, 0, 0)
        series_lay.setSpacing(8)

        series_group = QGroupBox("Series")
        self.series_vbox = QVBoxLayout(series_group)
        self.series_vbox.setSpacing(6)

        self.series_rows: List[_SeriesRow] = []
        self.add_series_btn = QPushButton("＋ Add series")
        self.add_series_btn.setFixedWidth(110)

        for s in (p.get("series") or []):
            row = _SeriesRow(self.columns, s, self)
            row.remove_btn.clicked.connect(lambda _=None, r=row: self._remove_row(r))
            self.series_rows.append(row)
            self.series_vbox.addWidget(row)

        self.series_vbox.addWidget(self.add_series_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        series_lay.addWidget(series_group)

        # X column + legend
        form_series = QFormLayout()
        self.x_combo = QComboBox(); self.x_combo.addItems(self.columns); self.x_combo.setCurrentText(p.get("x_col", ""))
        self.legend_check = QCheckBox(); self.legend_check.setChecked(bool(p.get("legend", True)))
        form_series.addRow("X column:", self.x_combo)
        form_series.addRow("Show legend:", self.legend_check)
        series_lay.addLayout(form_series)

        # Scroll wrapper
        series_scroll = _scrollable(series_tab_inner)
        self.tabs.addTab(series_scroll, "Series")

        # ---- Titles tab ----
        titles_tab = QWidget();
        titles_lay = QFormLayout(titles_tab)
        theme = {**_default_theme(), **(p.get("style") or {})}

        self.title_edit = QLineEdit(p.get("title", self.spec.title or "XY Chart"))
        self.title_size = QSpinBox();
        self.title_size.setRange(6, 36);
        self.title_size.setValue(int(theme.get("title_size", 12)))
        self.title_color = _ColorButton(theme.get("title_color", "#111111"))
        self.x_label_edit = QLineEdit(p.get("x_label", ""))
        self.y_left_label_edit = QLineEdit(p.get("y_left_label", ""))
        self.y_right_label_edit = QLineEdit(p.get("y_right_label", ""))
        self.axis_label_color = _ColorButton(theme.get("axis_label_color", "#111111"))

        titles_lay.addRow("Chart title:", self.title_edit)
        titles_lay.addRow("Title size:", self.title_size)
        titles_lay.addRow("Title color:", self.title_color)
        titles_lay.addRow("X axis title:", self.x_label_edit)
        titles_lay.addRow("Left Y axis title:", self.y_left_label_edit)
        titles_lay.addRow("Right Y axis title:", self.y_right_label_edit)
        titles_lay.addRow("Axis title color:", self.axis_label_color)
        self.tabs.addTab(titles_tab, "Titles")

        # ---- Axes & Grid tab ----
        axes_tab = QWidget();
        axes_lay = QFormLayout(axes_tab)
        self.label_size = QSpinBox();
        self.label_size.setRange(6, 36);
        self.label_size.setValue(int(theme.get("label_size", 10)))
        self.tick_size = QSpinBox();
        self.tick_size.setRange(6, 36);
        self.tick_size.setValue(int(theme.get("tick_size", 9)))
        self.grid_check = QCheckBox();
        self.grid_check.setChecked(bool(theme.get("grid", True)))
        self.grid_style = QComboBox();
        self.grid_style.addItems(["-", "--", ":", "-."])
        self.grid_style.setCurrentText(theme.get("grid_linestyle", "-"))
        self.grid_color = _ColorButton(theme.get("grid_color", "#e5e7eb"))
        self.grid_alpha = QDoubleSpinBox();
        self.grid_alpha.setRange(0.0, 1.0);
        self.grid_alpha.setSingleStep(0.05)
        self.grid_alpha.setValue(float(theme.get("grid_alpha", 1.0)))
        self.date_fmt = QLineEdit(theme.get("date_format", "auto"))

        # NEW: padding controls (percent)
        self.x_pad_spin = QDoubleSpinBox();
        self.x_pad_spin.setRange(0.0, 50.0);
        self.x_pad_spin.setSingleStep(0.5)
        self.x_pad_spin.setSuffix(" %")
        self.x_pad_spin.setValue(float(theme.get("x_pad_frac", 0.10)) * 100.0)

        self.y_pad_spin = QDoubleSpinBox();
        self.y_pad_spin.setRange(0.0, 50.0);
        self.y_pad_spin.setSingleStep(0.5)
        self.y_pad_spin.setSuffix(" %")
        self.y_pad_spin.setValue(float(theme.get("y_pad_frac", 0.10)) * 100.0)

        axes_lay.addRow("Axis label size:", self.label_size)
        axes_lay.addRow("Tick label size:", self.tick_size)
        axes_lay.addRow("Show grid:", self.grid_check)
        axes_lay.addRow("Grid line style:", self.grid_style)
        axes_lay.addRow("Grid color:", self.grid_color)
        axes_lay.addRow("Grid alpha:", self.grid_alpha)
        axes_lay.addRow("Date format (auto or strftime):", self.date_fmt)

        # NEW rows
        axes_lay.addRow("X padding (% of range):", self.x_pad_spin)
        axes_lay.addRow("Y padding (% of range):", self.y_pad_spin)

        self.tabs.addTab(axes_tab, "Axes & Grid")

        self.nan_sentinels_edit = QLineEdit(", ".join(map(str, p.get("nan_sentinels", []))))
        self.nan_sentinels_edit.setPlaceholderText("e.g. NaN, 9999, -9999, 0")
        axes_lay.addRow("Treat these values as missing (comma-separated):", self.nan_sentinels_edit)
        p.setdefault("nan_sentinels", [])

        # ---- Frame & Legend tab ----
        frame_tab = QWidget(); frame_lay = QFormLayout(frame_tab)
        self.facecolor_btn = _ColorButton(theme.get("facecolor", "#ffffff"))
        self.axes_facecolor_btn = _ColorButton(theme.get("axes_facecolor", "#ffffff"))
        self.spines_color_btn = _ColorButton(theme.get("spines_color", "#cccccc"))
        self.tight_check = QCheckBox(); self.tight_check.setChecked(bool(theme.get("tight_layout", True)))
        frame_lay.addRow("Figure background:", self.facecolor_btn)
        frame_lay.addRow("Axes background:", self.axes_facecolor_btn)
        frame_lay.addRow("Spines color:", self.spines_color_btn)
        frame_lay.addRow("Tight layout:", self.tight_check)
        self.tabs.addTab(frame_tab, "Frame & Legend")

        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        # Wiring
        self.add_series_btn.clicked.connect(self._add_series)

    def _add_series(self):
        s = {
            "y_col": self.columns[0] if self.columns else "",
            "label": "",
            "style": "Line",
            "marker": "",
            "size": 24.0,
            "linewidth": 2.0,
            "alpha": 1.0,
            "linestyle": "-",
            "color": "",
            "symbol_color": "",
            "y_axis": "left",
            "fill": False,
        }
        row = _SeriesRow(self.columns, s, self)
        row.remove_btn.clicked.connect(lambda _=None, r=row: self._remove_row(r))
        self.series_rows.append(row)
        idx = self.series_vbox.indexOf(self.add_series_btn)
        if idx == -1:
            self.series_vbox.addWidget(row)
        else:
            self.series_vbox.insertWidget(idx, row)

    def _remove_row(self, row: _SeriesRow):
        try: self.series_rows.remove(row)
        except ValueError: pass
        row.setParent(None); row.deleteLater()

    def accept(self):
        p = self.spec.payload
        _ensure_payload_defaults(self.spec, self.columns)

        # Titles
        title = self.title_edit.text().strip() or "XY Chart"
        p["title"] = title;
        self.spec.title = title
        p["x_label"] = self.x_label_edit.text().strip()
        p["y_left_label"] = self.y_left_label_edit.text().strip()
        p["y_right_label"] = self.y_right_label_edit.text().strip()

        # Series / X / Legend
        p["x_col"] = self.x_combo.currentText()
        p["legend"] = self.legend_check.isChecked()
        p["series"] = [r.to_dict() for r in self.series_rows] or p["series"]

        # Theme (collected from tabs)  — includes NEW padding fields
        p["style"] = {
            "facecolor": self.facecolor_btn.value() or "#ffffff",
            "axes_facecolor": self.axes_facecolor_btn.value() or "#ffffff",
            "spines_color": self.spines_color_btn.value() or "#cccccc",
            "grid": self.grid_check.isChecked(),
            "grid_linestyle": self.grid_style.currentText(),
            "grid_color": self.grid_color.value() or "#e5e7eb",
            "grid_alpha": float(self.grid_alpha.value()),
            "title_size": int(self.title_size.value()),
            "title_color": self.title_color.value() or "#111111",
            "axis_label_color": self.axis_label_color.value() or "#111111",
            "label_size": int(self.label_size.value()),
            "tick_size": int(self.tick_size.value()),
            "date_format": (self.date_fmt.text().strip() or "auto"),
            "tight_layout": self.tight_check.isChecked(),
            # NEW: store as fractions (0–1)
            "x_pad_frac": float(self.x_pad_spin.value()) / 100.0,
            "y_pad_frac": float(self.y_pad_spin.value()) / 100.0,
        }
        super().accept()

        # Sentinels parsing unchanged
        raw = [t.strip() for chunk in (self.nan_sentinels_edit.text() or "").split(",") for t in chunk.split()]
        sentinels: list = []
        for t in raw:
            if not t:
                continue
            if t.lower() == "nan":
                sentinels.append("nan");
                continue
            try:
                sentinels.append(float(t))
            except Exception:
                sentinels.append(t)
        p["nan_sentinels"] = sentinels


# --------------------- Renderer with adaptive rules + interactivity ---------------------
class XYRenderer(QWidget):
    requestConfigure = pyqtSignal()  # consumers may connect this

    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame], columns: List[str],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns

        lay = QVBoxLayout(self)

        # Canvas / axes
        self.canvas = FigureCanvas(Figure(figsize=(7.6, 3.8), tight_layout=True))
        self.canvas.setMinimumHeight(240)
        self.ax_left = self.canvas.figure.add_subplot(111)
        self.ax_right = None

        # Matplotlib toolbar (zoom, pan, home, save)
        self.toolbar = NavigationToolbar(self.canvas, self)

        # Configure & fullscreen controls
        self.cfg_btn = QToolButton(self)
        self.cfg_btn.setText("⚙")
        self.cfg_btn.setToolTip("Configure chart…")
        self.cfg_btn.clicked.connect(self._on_config_clicked)

        self.full_btn = QToolButton(self)
        self.full_btn.setText("⛶")
        self.full_btn.setToolTip("Toggle fullscreen")
        self.full_btn.clicked.connect(self._toggle_fullscreen)

        # Layout for toolbar row
        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.setSpacing(6)
        tool_row.addWidget(self.toolbar)
        tool_row.addStretch(1)
        tool_row.addWidget(self.cfg_btn)
        tool_row.addWidget(self.full_btn)
        lay.addLayout(tool_row)
        lay.addWidget(self.canvas)

        # Hover readout label
        self.readout = QLabel(" ")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.readout)

        # Hover/crosshair artists (lazy init)
        self._vline = None
        self._hline_left = None
        self._hline_right = None
        self._hover_dot = None
        self._hover_annot = None
        self._series_data: list[dict] = []  # {ax, x(np.ndarray), y(np.ndarray), label(str)}

        # Mouse events
        self._cid_motion = self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self._cid_leave  = self.canvas.mpl_connect("figure_leave_event", self._on_leave)

        self.refresh_data()

    # --------- helpers ---------
    def _maybe_epoch_to_datetime(self, s: pd.Series) -> Optional[pd.Series]:
        """
        If the series looks like Unix epoch seconds or milliseconds, return a tz-naive datetime64 series.
        Otherwise return None.
        """
        s_num = pd.to_numeric(s, errors="coerce")
        valid = s_num.dropna()
        if valid.empty:
            return None
        p10 = valid.quantile(0.10)
        p90 = valid.quantile(0.90)

        # Epoch seconds rough range: 1970..now+50y ~ 1e9 .. 2e9
        if 8e8 <= p10 <= 2e9 and 8e8 <= p90 <= 5e9:
            dt = pd.to_datetime(s_num, unit="s", origin="unix", utc=True).dt.tz_localize(None)
            return dt

        # Epoch milliseconds: 1e12 .. 2e12+
        if 8e11 <= p10 <= 5e12 and 8e11 <= p90 <= 1e13:
            dt = pd.to_datetime(s_num, unit="ms", origin="unix", utc=True).dt.tz_localize(None)
            return dt

        return None

    def _coerce_x_datetime_or_numeric(self, s: pd.Series) -> tuple[pd.Series, bool]:
        """
        Returns (series, is_datetime). Handles tz, strings, epoch sec/ms, and numeric fallback.
        """
        # already datetime?
        if pd.api.types.is_datetime64_any_dtype(s):
            return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_localize(None), True

        # epoch sec/ms?
        maybe = self._maybe_epoch_to_datetime(s)
        if maybe is not None:
            return maybe, True

        # strings → datetime if they parse WELL (>=90% success)
        if s.dtype == "O":
            dt = pd.to_datetime(s, errors="coerce", utc=True)
            if dt.notna().mean() >= 0.90:
                return dt.dt.tz_localize(None), True

        # fallback: numeric, but try to strip commas if strings
        if s.dtype == "O" and hasattr(s, "str"):
            s2 = pd.to_numeric(s.str.replace(",", "", regex=False), errors="coerce")
        else:
            s2 = pd.to_numeric(s, errors="coerce")
        return s2, False

    def _on_config_clicked(self):
        # Always open our editor inline to guarantee UX
        try:
            show_xy_editor_safely(self.spec, self.columns, self)
            self.refresh_data()
        except Exception:
            logger.exception("Opening XYEditor from renderer failed")

        # Let containers listen if they want (non-blocking)
        try:
            self.requestConfigure.emit()
        except Exception:
            pass

    def _toggle_fullscreen(self):
        win = self.window()
        if hasattr(win, "isFullScreen") and win.isFullScreen():
            win.showNormal()
        else:
            win.showFullScreen()

    def _ensure_hover_artists(self):
        if self._vline is None or not self._vline.axes:
            self._vline = self.ax_left.axvline(color="#888888", alpha=0.6, linewidth=0.8, visible=False)
        if self._hline_left is None or not self._hline_left.axes:
            self._hline_left = self.ax_left.axhline(color="#888888", alpha=0.6, linewidth=0.8, visible=False)
        if self.ax_right:
            if self._hline_right is None or not getattr(self._hline_right, "axes", None):
                self._hline_right = self.ax_right.axhline(color="#888888", alpha=0.6, linewidth=0.8, visible=False)
        else:
            self._hline_right = None
        if self._hover_dot is None or not self._hover_dot.axes:
            (self._hover_dot,) = self.ax_left.plot([], [], "o", ms=6, alpha=0.9, color="#333333", visible=False)
        if self._hover_annot is None or not getattr(self._hover_annot, "axes", None):
            self._hover_annot = self.ax_left.annotate(
                "", xy=(0, 0), xytext=(10, 10), textcoords="offset points",
                bbox=dict(boxstyle="round,pad=0.2", fc="w", ec="#999", alpha=0.95),
                fontsize=9
            )
            self._hover_annot.set_visible(False)

    def _hide_hover(self):
        for art in (self._vline, self._hline_left, self._hline_right, self._hover_dot, self._hover_annot):
            if art is not None:
                art.set_visible(False)
        self.readout.setText(" ")
        self.canvas.draw_idle()

    def _format_x(self, x_val):
        # Robust date/numeric formatting for x
        try:
            return mdates.num2date(float(x_val)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(x_val)

    def _to_naive_datetime(self, s: pd.Series) -> pd.Series:
        # Ensure datetime64 and remove tz (convert to UTC first to avoid shifts)
        s2 = pd.to_datetime(s, errors="coerce", utc=True)
        return s2.dt.tz_localize(None)

    def _apply_nan_sentinels(self, s: pd.Series, sentinels: list) -> pd.Series:
        if not sentinels:
            return s
        out = s.copy()
        num_sents = {v for v in sentinels if isinstance(v, (int, float))}
        if len(num_sents) > 0:
            try:
                out = pd.to_numeric(out, errors="ignore")
                out = out.mask(out.isin(num_sents))
            except Exception:
                pass
        str_sents = {str(v).lower() for v in sentinels if not isinstance(v, (int, float))}
        if str_sents:
            def _str_mask(val) -> bool:
                try:
                    return str(val).strip().lower() in str_sents
                except Exception:
                    return False
            out = out.mask(out.map(_str_mask))
        return out

    def _is_datetime(self, s: pd.Series) -> bool:
        return pd.api.types.is_datetime64_any_dtype(s)

    def _apply_theme(self, theme: Dict[str, Any]):
        fig = self.canvas.figure
        axl = self.ax_left
        fig.set_facecolor(theme.get("facecolor", "#ffffff"))
        axl.set_facecolor(theme.get("axes_facecolor", "#ffffff"))
        for spine in axl.spines.values():
            spine.set_color(theme.get("spines_color", "#cccccc"))
        if self.ax_right:
            self.ax_right.set_facecolor(theme.get("axes_facecolor", "#ffffff"))
            for spine in self.ax_right.spines.values():
                spine.set_color(theme.get("spines_color", "#cccccc"))

    def _apply_adaptive_rules(self, x_is_dt: bool, theme: Dict[str, Any], want_legend: bool) -> bool:
        h = max(1, self.height())
        scale = min(1.0, max(0.75, (h - 160) / 260.0))
        title_sz = max(9, int(theme.get("title_size", 12) * scale))
        label_sz = max(8, int(theme.get("label_size", 10) * scale))
        tick_sz  = max(7, int(theme.get("tick_size", 9)  * scale))

        self.ax_left.set_title(self.spec.title or "", fontsize=title_sz, color=theme.get("title_color", "#111111"))

        axis_color = theme.get("axis_label_color", "#111111")
        p = self.spec.payload
        self.ax_left.set_xlabel(p.get("x_label", ""), fontsize=label_sz, color=axis_color)
        self.ax_left.set_ylabel(p.get("y_left_label", ""), fontsize=label_sz, color=axis_color)
        if self.ax_right:
            self.ax_right.set_ylabel(p.get("y_right_label", ""), fontsize=label_sz, color=axis_color)

        self.ax_left.tick_params(labelsize=tick_sz)
        self.ax_left.yaxis.set_major_locator(MaxNLocator(nbins=5, prune="both"))
        if self.ax_right:
            self.ax_right.tick_params(labelsize=tick_sz)
            self.ax_right.yaxis.set_major_locator(MaxNLocator(nbins=5, prune="both"))

        # Date axis / numeric axis tick density
        if x_is_dt:
            locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
            self.ax_left.xaxis.set_major_locator(locator)
            try:
                self.ax_left.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
            except Exception:
                pass
        else:
            self.ax_left.xaxis.set_major_locator(MaxNLocator(nbins=6, prune="both"))

        if theme.get("grid", True):
            self.ax_left.grid(True,
                              linestyle=theme.get("grid_linestyle", "-"),
                              color=theme.get("grid_color", "#e5e7eb"),
                              alpha=float(theme.get("grid_alpha", 1.0)))
        else:
            self.ax_left.grid(False)

        too_short = h < 200
        many_series = sum(1 for _ in self.spec.payload.get("series", [])) >= 4
        legend_inside = not (too_short or many_series)
        if h < 170:
            return False if want_legend else False
        return legend_inside

    # --------- main render ---------
    def refresh_data(self):
        df = self.get_df()
        p = self.spec.payload
        _ensure_payload_defaults(self.spec, self.columns)

        # Reset axes
        self.ax_left.clear()
        if self.ax_right is not None:
            try:
                self.ax_right.remove()
            except Exception:
                pass
        self.ax_right = None
        self._series_data.clear()

        if df is None or df.empty or not p.get("series"):
            self.ax_left.text(0.5, 0.5, "No data / configure chart…", ha="center", va="center")
            self.canvas.draw_idle()
            return

        x_col = p.get("x_col")
        if not x_col or x_col not in df.columns:
            self.ax_left.text(0.5, 0.5, "Pick X column", ha="center", va="center")
            self.canvas.draw_idle()
            return

        used_y = [s.get("y_col", "") for s in p["series"] if s.get("y_col", "") in df.columns]
        if not used_y:
            self.ax_left.text(0.5, 0.5, "Pick Y column(s)", ha="center", va="center")
            self.canvas.draw_idle()
            return

        d = df[[x_col] + used_y].copy()

        # Coerce X
        d[x_col], x_is_dt = self._coerce_x_datetime_or_numeric(d[x_col])

        # Clean Y
        for ycol in used_y:
            y = d[ycol]
            if y.dtype == "O" and hasattr(y, "str"):
                y = y.str.replace(",", "", regex=False).str.replace(r"[^0-9\.\-eE]+", "", regex=True)
            d[ycol] = pd.to_numeric(y, errors="coerce")

        # Sort, drop bad rows
        d = d.sort_values(by=x_col, kind="mergesort", na_position="last").dropna(subset=[x_col])
        d = d[d[used_y].notna().any(axis=1)]
        if d.empty:
            self.ax_left.text(0.5, 0.5, "No plottable rows", ha="center", va="center")
            self.canvas.draw_idle()
            return

        # Right axis if requested
        if any(s.get("y_axis", "left") == "right" for s in p["series"]):
            self.ax_right = self.ax_left.twinx()

        # Plot
        handles, labels = [], []
        for s in p["series"]:
            y_col = s.get("y_col", "")
            if not y_col or y_col not in d.columns:
                continue

            x_vals = d[x_col]
            y_vals = d[y_col]
            ax = self.ax_right if (self.ax_right and s.get("y_axis") == "right") else self.ax_left

            style = s.get("style", "Line")
            color = s.get("color") or None
            symbol_color = s.get("symbol_color") or None
            alpha = float(s.get("alpha", 1.0))
            lw = float(s.get("linewidth", 2.0))
            marker = (s.get("marker") or None)
            linestyle = s.get("linestyle", "-")
            label = s.get("label") or y_col

            handle = None
            if style == "Bar":
                xv = mdates.date2num(x_vals) if x_is_dt else x_vals
                bars = ax.bar(xv, y_vals, alpha=alpha, color=color)
                handle = bars[0] if len(bars) else None
            elif style == "Scatter":
                handle = ax.scatter(x_vals, y_vals, s=float(s.get("size", 24.0)),
                                    marker=(marker or "o"), c=(symbol_color or color), alpha=alpha)
            else:
                handle = ax.plot(
                    x_vals, y_vals,
                    linestyle=("none" if linestyle == "none" else linestyle),
                    linewidth=lw, marker=marker, color=color, alpha=alpha,
                    markerfacecolor=(symbol_color or color), markeredgecolor=(symbol_color or color),
                )[0]
                if s.get("fill", False):
                    try:
                        xv = x_vals if not x_is_dt else mdates.date2num(x_vals)
                        ax.fill_between(xv, y_vals, alpha=min(alpha, 0.35), color=color, linewidth=0)
                    except Exception:
                        pass

            # Hover store
            try:
                if x_is_dt:
                    xx = np.asarray(mdates.date2num(x_vals.to_numpy()), dtype=float)
                else:
                    xx = np.asarray(x_vals.to_numpy(), dtype=float)
                yy = np.asarray(y_vals.to_numpy(), dtype=float)
                self._series_data.append({"ax": ax, "x": xx, "y": yy, "label": label})
            except Exception:
                pass

            if handle is not None:
                handles.append(handle)
                labels.append(label)

        # Theme & cosmetics
        theme = {**_default_theme(), **(p.get("style") or {})}
        self._apply_theme(theme)
        legend_inside = self._apply_adaptive_rules(x_is_dt, theme, bool(p.get("legend", True)))

        if x_is_dt:
            fmt = str(theme.get("date_format", "auto")).strip().lower()
            if fmt != "auto":
                try:
                    self.ax_left.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
                except Exception:
                    pass

        if p.get("legend", True) and handles:
            if legend_inside:
                self.ax_left.legend(handles, labels, loc="best",
                                    fontsize=max(7, int(theme.get("tick_size", 9) - 1)))
            else:
                self.ax_left.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.01, 1.0),
                                    borderaxespad=0., fontsize=max(7, int(theme.get("tick_size", 9) - 1)),
                                    frameon=False)

        # --------- X LIMITS with padding ---------
        x_pad_frac = float(theme.get("x_pad_frac", 0.10))
        x_data = d[x_col].dropna()

        if x_is_dt:
            data_lo = pd.to_datetime(x_data.min())
            data_hi = pd.to_datetime(x_data.max())
            # If single timestamp, start with ±1 minute
            if data_lo == data_hi:
                pad_dt = pd.Timedelta(minutes=1)
                data_lo, data_hi = data_lo - pad_dt, data_hi + pad_dt
            # Apply padding
            span = (data_hi - data_lo)
            pad = pd.Timedelta(seconds=span.total_seconds() * x_pad_frac)
            lo, hi = data_lo - pad, data_hi + pad
            self.ax_left.set_xlim(lo, hi)
            if self.ax_right:
                self.ax_right.set_xlim(lo, hi)
        else:
            vals = pd.to_numeric(x_data, errors="coerce")
            data_lo = float(np.nanmin(vals))
            data_hi = float(np.nanmax(vals))
            if data_lo == data_hi:
                base = 1.0 if data_lo == 0 else abs(data_lo) * 0.02
                data_lo, data_hi = data_lo - base, data_hi + base
            span = data_hi - data_lo
            pad = span * x_pad_frac
            lo, hi = data_lo - pad, data_hi + pad
            self.ax_left.set_xlim(lo, hi)
            if self.ax_right:
                self.ax_right.set_xlim(lo, hi)

        # --------- Y autoscale, then add padding ---------
        try:
            self.ax_left.relim();
            self.ax_left.autoscale(enable=True, axis="y", tight=False)
            if self.ax_right:
                self.ax_right.relim();
                self.ax_right.autoscale(enable=True, axis="y", tight=False)
        except Exception:
            pass

        y_pad_frac = float(theme.get("y_pad_frac", 0.10))

        try:
            yl0, yl1 = self.ax_left.get_ylim()
            yspan = (yl1 - yl0)
            ypad = (yspan if yspan != 0 else max(1.0, abs(yl0) or 1.0)) * y_pad_frac
            self.ax_left.set_ylim(yl0 - ypad, yl1 + ypad)
        except Exception:
            pass

        if self.ax_right:
            try:
                yr0, yr1 = self.ax_right.get_ylim()
                yspan = (yr1 - yr0)
                ypad = (yspan if yspan != 0 else max(1.0, abs(yr0) or 1.0)) * y_pad_frac
                self.ax_right.set_ylim(yr0 - ypad, yr1 + ypad)
            except Exception:
                pass

        # Layout + hover
        try:
            if theme.get("tight_layout", True):
                self.canvas.figure.tight_layout()
        except Exception:
            pass
        self._ensure_hover_artists()
        self.canvas.draw_idle()

    # --------- hover handlers ---------
    def _on_leave(self, _evt):
        self._hide_hover()

    def _on_motion(self, event):
        if event.inaxes is None or event.x is None or event.y is None:
            self._hide_hover()
            return

        ax = event.inaxes
        ex, ey = event.x, event.y
        best = None  # (dist2, x, y, label, ax)

        for s in self._series_data:
            if s["ax"] is not ax:
                continue
            x = s["x"]; y = s["y"]
            if x.size == 0:
                continue
            try:
                pts = ax.transData.transform(np.column_stack([x, y]))
            except Exception:
                continue
            dx = pts[:, 0] - ex
            dy = pts[:, 1] - ey
            i = np.nanargmin(dx * dx + dy * dy)
            dist2 = dx[i] * dx[i] + dy[i] * dy[i]
            cand = (dist2, x[i], y[i], s["label"], ax)
            if best is None or dist2 < best[0]:
                best = cand

        if best is None or best[0] > 40 ** 2:  # ~40px radius
            self._hide_hover()
            return

        _, x0, y0, label, ax0 = best
        self._ensure_hover_artists()

        try:
            self._vline.set_xdata([x0])
            self._vline.set_visible(True)
        except Exception:
            pass

        if ax0 is self.ax_right and self._hline_right is not None:
            self._hline_right.set_ydata([y0])
            self._hline_right.set_visible(True)
            if self._hline_left is not None:
                self._hline_left.set_visible(False)
        else:
            self._hline_left.set_ydata([y0])
            self._hline_left.set_visible(True)
            if self._hline_right is not None:
                self._hline_right.set_visible(False)

        try:
            self._hover_dot.set_data([x0], [y0])
            self._hover_dot.set_visible(True)
            self._hover_annot.xy = (x0, y0)
            txt = f"{label}\n{self._format_x(x0)} • {y0:.6g}"
            self._hover_annot.set_text(txt)
            self._hover_annot.set_visible(True)
            self.readout.setText(txt.replace("\n", "  |  "))
        except Exception:
            pass

        self.canvas.draw_idle()

# --------------------- Handler ---------------------
@register
class XYHandler(TypeHandlerBase):
    kind = "XY"

    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        # prefer our canonical timeline column if present
        x = "__dt_iso" if "__dt_iso" in columns else (columns[0] if columns else "")
        y = next((c for c in columns if c != x), x)
        return {
            "title": "XY Chart",
            "x_col": x,
            "series": [{
                "y_col": y, "label": y, "style": "Line",
                "marker": "", "size": 24.0, "linewidth": 2.0,
                "alpha": 1.0, "linestyle": "-", "color": "", "symbol_color": "", "y_axis": "left",
                "fill": False,
            }],
            "legend": True,
            "x_label": "",
            "y_left_label": "",
            "y_right_label": "",
            "style": _default_theme(),
        }

    def create_editor(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None) -> QDialog:
        _ensure_payload_defaults(spec, columns)
        return XYEditor(spec, columns, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[QWidget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        _ensure_payload_defaults(spec, columns)
        return XYRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        y = flat.get("y_col", "")
        payload: Dict[str, Any] = {
            "x_col": flat.get("x_col", ""),
            "series": [{
                "y_col": y, "label": y or "", "style": flat.get("style", "Line"),
                "marker": flat.get("marker", ""), "size": float(flat.get("size", 24.0)),
                "linewidth": float(flat.get("linewidth", 2.0)),
                "alpha": float(flat.get("alpha", 1.0)), "linestyle": flat.get("linestyle", "-"),
                "color": flat.get("color", ""), "symbol_color": flat.get("symbol_color", ""),
                "y_axis": flat.get("y_axis", "left"),
                "fill": bool(flat.get("fill", False)),
            }],
            "legend": flat.get("legend", True),
            "title": flat.get("title", "XY Chart"),
            "x_label": flat.get("x_label", ""),
            "y_left_label": flat.get("y_left_label", ""),
            "y_right_label": flat.get("y_right_label", ""),
            "style": {**_default_theme()},
        }
        return payload
