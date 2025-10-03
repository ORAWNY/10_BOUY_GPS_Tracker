# utils/charts/gauge_chart.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional

import numpy as np
import pandas as pd

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Wedge

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QDialog, QHBoxLayout,
    QComboBox, QDoubleSpinBox, QLineEdit, QPushButton, QLabel
)

from utils.charts import register, TypeHandlerBase, ChartSpec


class GaugeEditor(QDialog):
    def __init__(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Gauge")
        self.setModal(True)
        self.spec = spec
        self.columns = columns

        p = self.spec.payload
        p.setdefault("mode", "last")  # "last"|"mean"|"sum"|"min"|"max"|"availability"
        p.setdefault("value_col", next(iter(columns), ""))
        p.setdefault("min", 0.0)
        p.setdefault("max", 100.0)
        p.setdefault("units", "")
        p.setdefault("title", self.spec.title or "Gauge")

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_edit = QLineEdit(p.get("title", self.spec.title or "Gauge"))

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["last", "mean", "sum", "min", "max", "availability"])
        self.mode_combo.setCurrentText(p.get("mode", "last"))

        self.val_combo = QComboBox()
        self.val_combo.addItems([""] + columns)
        self.val_combo.setCurrentText(p.get("value_col", ""))

        self.gmin = QDoubleSpinBox(); self.gmin.setRange(-1e12, 1e12); self.gmin.setDecimals(6)
        self.gmin.setValue(float(p.get("min", 0.0)))
        self.gmax = QDoubleSpinBox(); self.gmax.setRange(-1e12, 1e12); self.gmax.setDecimals(6)
        self.gmax.setValue(float(p.get("max", 100.0)))

        self.units = QLineEdit(p.get("units", ""))

        form.addRow("Title:", self.title_edit)
        form.addRow("Calculation:", self.mode_combo)
        form.addRow("Value column:", self.val_combo)
        form.addRow("Min:", self.gmin)
        form.addRow("Max:", self.gmax)
        form.addRow("Units:", self.units)
        lay.addLayout(form)

        btns = QHBoxLayout(); btns.addStretch(1)
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        btns.addWidget(ok); btns.addWidget(cancel); lay.addLayout(btns)
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)

    def accept(self):
        p = self.spec.payload
        title = self.title_edit.text().strip() or "Gauge"
        p["title"] = title
        self.spec.title = title
        p["mode"] = self.mode_combo.currentText()
        p["value_col"] = self.val_combo.currentText()
        p["min"] = float(self.gmin.value())
        p["max"] = float(self.gmax.value())
        p["units"] = self.units.text().strip()
        super().accept()


class GaugeRenderer(QWidget):
    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                 columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns

        lay = QVBoxLayout(self)
        self.canvas = FigureCanvas(Figure(figsize=(7.4, 4.0), tight_layout=True))
        lay.addWidget(self.canvas)
        self.ax = self.canvas.figure.add_subplot(111)
        self.refresh_data()

    def _calc_value(self, df: pd.DataFrame, p: Dict[str, Any]) -> float:
        col = p.get("value_col", "")
        if not col or col not in df.columns:
            return float("nan")
        vals = pd.to_numeric(df[col], errors="coerce")
        vals = vals[vals.notna()]
        mode = p.get("mode", "last")
        if vals.empty:
            return float("nan")
        if mode == "last":
            return float(vals.iloc[-1])
        if mode == "mean":
            return float(vals.mean())
        if mode == "sum":
            return float(vals.sum())
        if mode == "min":
            return float(vals.min())
        if mode == "max":
            return float(vals.max())
        if mode == "availability":
            total = int(len(vals))
            valid = int((vals != 9999).sum() - (vals == -9999).sum())
            return float(valid / total * 100.0) if total else float("nan")
        return float(vals.iloc[-1])

    def refresh_data(self):
        df = self.get_df()
        p = self.spec.payload
        self.ax.clear()
        self.ax.set_aspect('equal')
        self.ax.axis('off')

        if df is None or df.empty:
            self.ax.text(0.5, 0.5, "No data", ha='center', va='center')
            self.canvas.draw_idle(); return

        val = self._calc_value(df, p)
        if np.isnan(val):
            self.ax.text(0.5, 0.5, "No value", ha='center', va='center')
            self.canvas.draw_idle(); return

        vmin = float(p.get("min", 0.0))
        vmax = float(p.get("max", 100.0))
        if vmax == vmin:
            vmax = vmin + 1.0
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))

        r, w = 1.0, 0.2
        track = Wedge((0, 0), r, 180, 0, width=w, facecolor="#e0e0e0", edgecolor="none")
        arc = Wedge((0, 0), r, 180, 180 * (1 - t), width=w, facecolor="#4caf50", edgecolor="none")
        self.ax.add_patch(track); self.ax.add_patch(arc)

        units = p.get("units", "")
        disp = f"{val:.2f}{units}" if units else f"{val:.2f}"
        self.ax.text(0, -0.05, disp, ha='center', va='center', fontsize=14, weight='bold')
        self.ax.set_xlim(-1.1, 1.1); self.ax.set_ylim(-0.1, 1.2)

        self.ax.set_title(self.spec.title or "")
        self.canvas.draw_idle()


@register
class GaugeHandler(TypeHandlerBase):
    kind = "Gauge"

    # instance methods (no @classmethod)
    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        return {
            "title": "Gauge",
            "mode": "last",
            "value_col": (columns[0] if columns else ""),
            "min": 0.0,
            "max": 100.0,
            "units": "",
        }

    def create_editor(self, spec: ChartSpec, columns: List[str],
                      parent: Optional[Widget] = None) -> QDialog:
        return GaugeEditor(spec, columns, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[Widget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        return GaugeRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        # Optional: map old keys to new payload
        return {
            "title": flat.get("title", ""),
            "mode": flat.get("calc_mode", "last"),
            "value_col": flat.get("calc_column", flat.get("gauge_value_col", "")),
            "min": float(flat.get("gauge_min", 0.0)),
            "max": float(flat.get("gauge_max", 100.0)),
            "units": flat.get("gauge_units", ""),
        }
