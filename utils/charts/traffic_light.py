# utils/charts/traffic_light.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional
import math

import pandas as pd

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QDialog, QFormLayout,
    QLabel, QComboBox, QDoubleSpinBox, QPushButton, QLineEdit, QGroupBox
)

from utils.charts import register, TypeHandlerBase, ChartSpec


# ---------- helpers ----------
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _eval_op(value: float, op: str, rhs: float) -> bool:
    if op == "<":  return value <  rhs
    if op == "<=": return value <= rhs
    if op == ">":  return value >  rhs
    if op == ">=": return value >= rhs
    if op == "==": return value == rhs
    if op == "!=": return value != rhs
    return False


# ---------- editor ----------
class TrafficLightEditor(QDialog):
    """
    Configure:
      Source: Metric (column + calc) OR Distance from deployment
      Rules:  Green rule (op, value), Amber rule (op, value), else Red
    """
    def __init__(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Traffic light")
        self.setModal(True)
        self.spec = spec
        self.columns = columns
        p = self.spec.payload

        # defaults
        p.setdefault("title", self.spec.title or "Traffic light")
        p.setdefault("source", {"kind": "metric", "column": (columns[0] if columns else ""), "calc": "last"})
        p.setdefault("rule_green", {"op": "<=", "value": 0.0})
        p.setdefault("rule_amber", {"op": "<=", "value": 0.0})
        # color hexes (optional)
        p.setdefault("color_green", "#28a745")
        p.setdefault("color_amber", "#ffc107")
        p.setdefault("color_red",   "#dc3545")

        root = QVBoxLayout(self)

        # Title
        self.title_edit = QLineEdit(p.get("title", "Traffic light"))
        tform = QFormLayout(); tform.addRow("Title:", self.title_edit)
        tbox = QGroupBox("Title"); tbox.setLayout(tform)
        root.addWidget(tbox)

        # Source config
        sbox = QGroupBox("Source")
        sform = QFormLayout(sbox)
        self.src_kind = QComboBox(); self.src_kind.addItems(["metric", "distance"])
        self.src_kind.setCurrentText(p["source"].get("kind", "metric"))

        self.src_col = QComboBox(); self.src_col.addItems([""] + columns)
        self.src_col.setCurrentText(p["source"].get("column", ""))

        self.src_calc = QComboBox(); self.src_calc.addItems(["last", "mean", "min", "max"])
        self.src_calc.setCurrentText(p["source"].get("calc", "last"))

        sform.addRow("Kind:", self.src_kind)
        sform.addRow("Column (metric):", self.src_col)
        sform.addRow("Calc (metric):", self.src_calc)
        root.addWidget(sbox)

        def _toggle_metric_fields(kind: str):
            is_metric = (kind == "metric")
            self.src_col.setEnabled(is_metric)
            self.src_calc.setEnabled(is_metric)
        _toggle_metric_fields(self.src_kind.currentText())
        self.src_kind.currentTextChanged.connect(_toggle_metric_fields)

        # Rules
        rbox = QGroupBox("Rules")
        rform = QFormLayout(rbox)
        ops = ["<", "<=", ">", ">=", "==", "!="]

        self.gr_op = QComboBox(); self.gr_op.addItems(ops); self.gr_op.setCurrentText(p["rule_green"].get("op", "<="))
        self.gr_val = QDoubleSpinBox(); self.gr_val.setRange(-1e12, 1e12); self.gr_val.setDecimals(6)
        self.gr_val.setValue(float(p["rule_green"].get("value", 0.0)))

        self.am_op = QComboBox(); self.am_op.addItems(ops); self.am_op.setCurrentText(p["rule_amber"].get("op", "<="))
        self.am_val = QDoubleSpinBox(); self.am_val.setRange(-1e12, 1e12); self.am_val.setDecimals(6)
        self.am_val.setValue(float(p["rule_amber"].get("value", 0.0)))

        rform.addRow("Green when:", _row(self.gr_op, QLabel(" value "), self.gr_val))
        rform.addRow("Amber when:", _row(self.am_op, QLabel(" value "), self.am_val))
        root.addWidget(rbox)

        # buttons
        btns = QHBoxLayout(); btns.addStretch(1)
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        btns.addWidget(ok); btns.addWidget(cancel)
        root.addLayout(btns)

    def accept(self):
        p = self.spec.payload
        p["title"] = self.title_edit.text().strip() or "Traffic light"
        self.spec.title = p["title"]
        kind = self.src_kind.currentText()
        p["source"] = {"kind": kind}
        if kind == "metric":
            p["source"]["column"] = self.src_col.currentText()
            p["source"]["calc"] = self.src_calc.currentText()
        # rules
        p["rule_green"] = {"op": self.gr_op.currentText(), "value": float(self.gr_val.value())}
        p["rule_amber"] = {"op": self.am_op.currentText(), "value": float(self.am_val.value())}
        super().accept()


def _row(*widgets):
    w = QWidget()
    lay = QHBoxLayout(w); lay.setContentsMargins(0,0,0,0)
    for x in widgets: lay.addWidget(x)
    lay.addStretch(1)
    return w


# ---------- renderer ----------
class TrafficLightRenderer(QWidget):
    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame], columns: List[str], parent=None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns

        root = QVBoxLayout(self)
        hl = QHBoxLayout(); root.addLayout(hl)
        self.dot = QLabel("   "); self.dot.setFixedSize(26, 26); self.dot.setStyleSheet("border-radius:13px;background:#999;")
        self.value_lbl = QLabel("—")
        self.title_lbl = QLabel(self.spec.title or "Traffic light"); self.title_lbl.setStyleSheet("font-weight:600;")
        hl.addWidget(self.title_lbl); hl.addStretch(1)
        hl.addWidget(self.value_lbl); hl.addSpacing(8); hl.addWidget(self.dot)

        self.refresh_data()

    def _metric_value(self, df: pd.DataFrame, src: dict) -> Optional[float]:
        col = src.get("column") or ""
        if not col or col not in df.columns:
            return None
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty: return None
        calc = src.get("calc", "last")
        if calc == "last": return float(s.iloc[-1])
        if calc == "mean": return float(s.mean())
        if calc == "min":  return float(s.min())
        if calc == "max":  return float(s.max())
        return None

    def _distance_value(self, df: pd.DataFrame) -> Optional[float]:
        if "Lat" not in df.columns or "Lon" not in df.columns:
            return None
        lat = pd.to_numeric(df["Lat"], errors="coerce")
        lon = pd.to_numeric(df["Lon"], errors="coerce")
        ok = lat.notna() & lon.notna()
        if not ok.any(): return None
        lat = lat[ok]; lon = lon[ok]
        dep_lat = float(lat.head(5).mean()); dep_lon = float(lon.head(5).mean())
        last_lat = float(lat.iloc[-1]);      last_lon = float(lon.iloc[-1])
        return _haversine_m(dep_lat, dep_lon, last_lat, last_lon)

    def refresh_data(self):
        p = self.spec.payload
        df = self.get_df()
        if df is None or df.empty:
            self._set("—", "#999"); return

        src = p.get("source", {"kind":"metric"})
        if src.get("kind","metric") == "distance":
            val = self._distance_value(df)
        else:
            val = self._metric_value(df, src)

        if val is None:
            self._set("—", "#999"); return

        # rules: check green first, then amber, else red
        gr = p.get("rule_green", {"op":"<=", "value":0.0})
        am = p.get("rule_amber", {"op":"<=", "value":0.0})
        color_g = p.get("color_green", "#28a745")
        color_a = p.get("color_amber", "#ffc107")
        color_r = p.get("color_red",   "#dc3545")

        if _eval_op(val, gr.get("op","<="), float(gr.get("value",0.0))):
            self._set(f"{val:.6g}", color_g)
        elif _eval_op(val, am.get("op","<="), float(am.get("value",0.0))):
            self._set(f"{val:.6g}", color_a)
        else:
            self._set(f"{val:.6g}", color_r)

    def _set(self, text: str, color: str):
        self.value_lbl.setText(text)
        self.dot.setStyleSheet(f"border-radius:13px;background:{color};")
        self.title_lbl.setText(self.spec.title or "Traffic light")


@register
class TrafficLightHandler(TypeHandlerBase):
    kind = "TrafficLight"

    # instance methods
    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        first = columns[0] if columns else ""
        return {
            "title": "Traffic light",
            "source": {"kind": "metric", "column": first, "calc": "last"},
            "rule_green": {"op": "<=", "value": 0.0},
            "rule_amber": {"op": "<=", "value": 0.0},
            "color_green": "#28a745",
            "color_amber": "#ffc107",
            "color_red":   "#dc3545",
        }

    def create_editor(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None) -> QDialog:
        return TrafficLightEditor(spec, columns, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[QWidget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        return TrafficLightRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        # if you previously stored flat dicts, map them to new payload here
        return {}
