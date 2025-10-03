# utils/charts/pie_chart.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional

import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QDialog, QHBoxLayout,
    QLabel, QComboBox, QCheckBox, QDoubleSpinBox, QLineEdit, QPushButton
)

# Import from package so @register updates the shared REGISTRY
from utils.charts import register, TypeHandlerBase, ChartSpec


class PieEditor(QDialog):
    def __init__(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Pie Chart")
        self.setModal(True)
        self.spec = spec
        self.columns = columns

        p = self.spec.payload
        p.setdefault("mode", "none")  # "none" | "availability" | "group_count" | "group_sum" | "group_mean"
        p.setdefault("value_col", next(iter(columns), ""))
        p.setdefault("group_col", "")
        p.setdefault("donut", 0.4)
        p.setdefault("autopct", True)
        p.setdefault("title", self.spec.title or "Pie Chart")

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.title_edit = QLineEdit(p.get("title", self.spec.title or "Pie Chart"))
        self.mode_combo = QComboBox(); self.mode_combo.addItems(
            ["none", "availability", "group_count", "group_sum", "group_mean"]
        )
        self.mode_combo.setCurrentText(p.get("mode", "none"))

        self.val_combo = QComboBox(); self.val_combo.addItems([""] + columns)
        self.val_combo.setCurrentText(p.get("value_col", ""))

        self.grp_combo = QComboBox(); self.grp_combo.addItems([""] + columns)
        self.grp_combo.setCurrentText(p.get("group_col", ""))

        self.donut = QDoubleSpinBox(); self.donut.setRange(0.0, 0.95)
        self.donut.setSingleStep(0.05)
        self.donut.setValue(float(p.get("donut", 0.4)))

        self.autopct = QCheckBox(); self.autopct.setChecked(bool(p.get("autopct", True)))

        form.addRow("Title:", self.title_edit)
        form.addRow("Calculation:", self.mode_combo)
        form.addRow("Value column:", self.val_combo)
        form.addRow("Group column:", self.grp_combo)
        form.addRow("Donut width:", self.donut)
        form.addRow("Show % labels:", self.autopct)
        lay.addLayout(form)

        btns = QHBoxLayout(); btns.addStretch(1)
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        btns.addWidget(ok); btns.addWidget(cancel); lay.addLayout(btns)
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)

    def accept(self):
        p = self.spec.payload
        title = self.title_edit.text().strip() or "Pie Chart"
        p["title"] = title
        self.spec.title = title
        p["mode"] = self.mode_combo.currentText()
        p["value_col"] = self.val_combo.currentText()
        p["group_col"] = self.grp_combo.currentText()
        p["donut"] = float(self.donut.value())
        p["autopct"] = self.autopct.isChecked()
        super().accept()


class PieRenderer(QWidget):
    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame], columns: List[str],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns

        lay = QVBoxLayout(self)
        self.canvas = FigureCanvas(Figure(figsize=(7.4, 4.0), tight_layout=True))
        lay.addWidget(self.canvas)
        self.ax = self.canvas.figure.add_subplot(111)
        self.refresh_data()

    def refresh_data(self):
        df = self.get_df()
        p = self.spec.payload
        self.ax.clear()

        if df is None or df.empty:
            self.ax.text(0.5, 0.5, "No data", ha='center', va='center')
            self.canvas.draw_idle(); return

        mode = p.get("mode", "none")
        donut = float(p.get("donut", 0.4))
        autopct = "%1.1f%%" if p.get("autopct", True) else None
        wedgeprops = {"width": donut} if donut else {}

        def _safe_values(series_name: str):
            s = pd.to_numeric(df[series_name], errors="coerce").dropna()
            return s

        if mode == "availability":
            col = p.get("value_col", "")
            if not col or col not in df.columns:
                self.ax.text(0.5, 0.5, "Pick a value column", ha='center', va='center')
                self.canvas.draw_idle(); return
            vals = pd.to_numeric(df[col], errors="coerce")
            mask = vals.notna() & (vals != 9999) & (vals != -9999)
            valid = int(mask.sum())
            total = int(mask.size)
            invalid = total - valid
            self.ax.pie([valid, invalid], labels=["Available", "Missing"], autopct=autopct,
                        startangle=90, counterclock=False, wedgeprops=wedgeprops, normalize=True)

        elif mode in ("group_count", "group_sum", "group_mean"):
            grp = p.get("group_col", "")
            if not grp or grp not in df.columns:
                self.ax.text(0.5, 0.5, "Pick a group column", ha='center', va='center')
                self.canvas.draw_idle(); return

            if mode == "group_count":
                agg = df.groupby(grp).size()
            else:
                val = p.get("value_col", "")
                if not val or val not in df.columns:
                    self.ax.text(0.5, 0.5, "Pick a value column", ha='center', va='center')
                    self.canvas.draw_idle(); return
                s = _safe_values(val)
                if s.empty:
                    self.ax.text(0.5, 0.5, "No numeric values", ha='center', va='center')
                    self.canvas.draw_idle(); return
                g = df.loc[s.index, grp]
                if mode == "group_sum":
                    agg = s.groupby(g).sum()
                else:  # group_mean
                    agg = s.groupby(g).mean()

            if len(agg) == 0:
                self.ax.text(0.5, 0.5, "No groups to plot", ha='center', va='center')
                self.canvas.draw_idle(); return

            self.ax.pie(agg.values, labels=[str(x) for x in agg.index], autopct=autopct,
                        startangle=90, counterclock=False, wedgeprops=wedgeprops, normalize=True)

        else:
            col = p.get("value_col", "")
            if not col or col not in df.columns:
                self.ax.text(0.5, 0.5, "Pick a value column", ha='center', va='center')
                self.canvas.draw_idle(); return
            s = _safe_values(col)
            if s.empty:
                self.ax.text(0.5, 0.5, "No numeric values", ha='center', va='center')
                self.canvas.draw_idle(); return
            self.ax.pie(s.values, autopct=autopct, startangle=90,
                        counterclock=False, wedgeprops=wedgeprops, normalize=True)

        self.ax.set_title(self.spec.title or "")
        self.ax.set_aspect("equal")
        self.canvas.draw_idle()


@register
class PieHandler(TypeHandlerBase):
    kind = "Pie"

    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        return {
            "title": "Pie Chart",
            "mode": "none",
            "value_col": (columns[0] if columns else ""),
            "group_col": "",
            "donut": 0.4,
            "autopct": True,
        }

    def create_editor(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None) -> QDialog:
        return PieEditor(spec, columns, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[QWidget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        return PieRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        # Optional: map old keys to new payload
        return {
            "title": flat.get("title", ""),
            "mode": flat.get("calc_mode", "none"),
            "value_col": flat.get("calc_column", ""),
            "group_col": flat.get("calc_group_col", ""),
            "donut": float(flat.get("pie_donut", 0.4)),
            "autopct": bool(flat.get("pie_autopct", True)),
        }
