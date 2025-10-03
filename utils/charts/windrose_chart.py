# utils/charts/windrose_chart.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional, Tuple

import math
import numpy as np
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QDialog, QFormLayout, QLabel, QComboBox,
    QCheckBox, QLineEdit, QPushButton, QDoubleSpinBox, QSpinBox, QGroupBox,
    QColorDialog, QDateEdit
)

# Windrose: guard import so a missing dependency doesn't crash the app
try:
    from windrose import WindroseAxes
except Exception as _windrose_err:  # noqa: N816
    WindroseAxes = None  # type: ignore

# IMPORTANT: import from the package so @register writes into the shared REGISTRY
from utils.charts import register, TypeHandlerBase, ChartSpec


# --------------------- Utilities / Defaults ---------------------
def _default_theme() -> Dict[str, Any]:
    return {
        "facecolor": "#ffffff",
        "axes_facecolor": "#cccccc",
        "grid": True,
        "grid_linestyle": "-",
        "grid_color": "#ffffff",
        "grid_alpha": 0.7,
        "spines_color": "#cccccc",
        "title_size": 12,
        "title_color": "#111111",
        "axis_label_color": "#111111",
        "label_size": 10,
        "tick_size": 9,
        "tight_layout": True,
    }


def _first_datetime_column(columns: List[str], get_df: Callable[[], pd.DataFrame]) -> str:
    try:
        df = get_df()
        for c in columns:
            if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c]):
                return c
    except Exception:
        pass
    return ""


def _ensure_payload_defaults(spec: ChartSpec, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> None:
    p = spec.payload
    default_dir = next((c for c in columns if "dir" in c.lower() or "wd" in c.lower()), (columns[0] if columns else ""))
    default_speed = next((c for c in columns if "speed" in c.lower() or "ws" in c.lower()), (columns[0] if columns else ""))

    p.setdefault("series", [{
        "dir_col": default_dir,
        "speed_col": default_speed,
        "label": "Windrose",
    }])

    p.setdefault("title", spec.title or "Wind Rose")
    p.setdefault("legend", True)
    p.setdefault("bins", 8)
    p.setdefault("opening", 0.8)
    p.setdefault("edgecolor", "#ffffff")
    p.setdefault("style", _default_theme())
    p.setdefault("normalize", "percent")  # "percent" | "count"
    p.setdefault("ytick_step", 4)
    p.setdefault("ytick_max", 0)

    p.setdefault("date_col", _first_datetime_column(columns, get_df))
    p.setdefault("date_from", "")
    p.setdefault("date_to", "")


# --------------------- Small ui helper ---------------------
class _ColorButton(QWidget):
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


# --------------------- Editor ---------------------
class _SeriesRow(QWidget):
    def __init__(self, columns: List[str], initial: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)

        self.dir_combo = QComboBox(self); self.dir_combo.addItems(columns)
        self.dir_combo.setCurrentText(initial.get("dir_col", columns[0] if columns else ""))

        self.speed_combo = QComboBox(self); self.speed_combo.addItems(columns)
        self.speed_combo.setCurrentText(initial.get("speed_col", columns[0] if columns else ""))

        self.label_edit = QLineEdit(initial.get("label", ""))

        self.remove_btn = QPushButton("−"); self.remove_btn.setFixedWidth(26)

        lay.addWidget(QLabel("Direction:")); lay.addWidget(self.dir_combo, 1)
        lay.addWidget(QLabel("Speed:")); lay.addWidget(self.speed_combo, 1)
        lay.addWidget(QLabel("Label:")); lay.addWidget(self.label_edit, 1)
        lay.addWidget(self.remove_btn)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dir_col": self.dir_combo.currentText(),
            "speed_col": self.speed_combo.currentText(),
            "label": self.label_edit.text().strip(),
        }


class WindRoseEditor(QDialog):
    def __init__(self, spec: ChartSpec, columns: List[str], get_df: Callable[[], pd.DataFrame],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Wind Rose")
        self.setModal(True)
        self.spec = spec
        self.columns = columns
        self.get_df = get_df
        _ensure_payload_defaults(self.spec, columns, get_df)

        p = self.spec.payload
        theme = {**_default_theme(), **(p.get("style") or {})}

        root = QVBoxLayout(self)

        # --- Series group ---
        series_group = QGroupBox("Series")
        sv = QVBoxLayout(series_group); sv.setSpacing(6)
        self.series_rows: List[_SeriesRow] = []
        self.add_series_btn = QPushButton("＋ Add series"); self.add_series_btn.setFixedWidth(110)

        for s in (p.get("series") or []):
            row = _SeriesRow(columns, s, self)
            row.remove_btn.clicked.connect(lambda _=None, r=row: self._remove_row(r))
            self.series_rows.append(row); sv.addWidget(row)
        sv.addWidget(self.add_series_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(series_group)

        # --- Global form ---
        form = QFormLayout()

        self.title_edit = QLineEdit(p.get("title", self.spec.title or "Wind Rose"))

        self.bins_spin = QSpinBox(); self.bins_spin.setRange(1, 72); self.bins_spin.setValue(int(p.get("bins", 8)))
        self.opening_spin = QDoubleSpinBox(); self.opening_spin.setRange(0.05, 1.0); self.opening_spin.setSingleStep(0.05)
        self.opening_spin.setValue(float(p.get("opening", 0.8)))
        self.legend_check = QCheckBox(); self.legend_check.setChecked(bool(p.get("legend", True)))

        self.normalize_combo = QComboBox(); self.normalize_combo.addItems(["percent", "count"])
        self.normalize_combo.setCurrentText(p.get("normalize", "percent"))

        self.ytick_step = QSpinBox(); self.ytick_step.setRange(0, 100); self.ytick_step.setValue(int(p.get("ytick_step", 4)))
        self.ytick_max  = QSpinBox(); self.ytick_max.setRange(0, 1000); self.ytick_max.setValue(int(p.get("ytick_max", 0)))

        self.facecolor_btn = _ColorButton(theme.get("axes_facecolor", "#cccccc"))
        self.edgecolor_btn = _ColorButton(p.get("edgecolor", "#ffffff"))
        self.title_size = QSpinBox(); self.title_size.setRange(6, 36); self.title_size.setValue(int(theme.get("title_size", 12)))
        self.title_color = _ColorButton(theme.get("title_color", "#111111"))

        form.addRow("Chart title:", self.title_edit)
        form.addRow("Speed bins:", self.bins_spin)
        form.addRow("Bar opening (0–1):", self.opening_spin)
        form.addRow("Show legend:", self.legend_check)
        form.addRow("Normalize:", self.normalize_combo)
        form.addRow("Radial tick step (0=auto):", self.ytick_step)
        form.addRow("Radial tick max (0=auto):", self.ytick_max)
        form.addRow("Rose facecolor:", self.facecolor_btn)
        form.addRow("Bar edgecolor:", self.edgecolor_btn)
        form.addRow("Title size:", self.title_size)
        form.addRow("Title color:", self.title_color)

        # --- Optional date filter ---
        df = self.get_df()
        self.date_col_combo = QComboBox(); self.date_col_combo.addItems([""] + columns)
        self.date_col_combo.setCurrentText(p.get("date_col", ""))

        self.date_from = QDateEdit(); self.date_from.setCalendarPopup(True)
        self.date_to   = QDateEdit(); self.date_to.setCalendarPopup(True)

        dcol = p.get("date_col", "")
        if dcol and dcol in df.columns and pd.api.types.is_datetime64_any_dtype(df[dcol]):
            try:
                dmin = pd.to_datetime(df[dcol].min()).date()
                dmax = pd.to_datetime(df[dcol].max()).date()
                self.date_from.setDate(QDate(dmin.year, dmin.month, dmin.day))
                self.date_to.setDate(QDate(dmax.year, dmax.month, dmax.day))
            except Exception:
                self.date_from.setDate(QDate.currentDate()); self.date_to.setDate(QDate.currentDate())
        else:
            self.date_from.setDate(QDate.currentDate())
            self.date_to.setDate(QDate.currentDate())

        form.addRow("Date column (optional):", self.date_col_combo)
        form.addRow("From date:", self.date_from)
        form.addRow("To date:", self.date_to)

        root.addLayout(form)

        # Buttons
        btns = QHBoxLayout(); btns.addStretch(1)
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        btns.addWidget(ok); btns.addWidget(cancel)
        root.addLayout(btns)
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)

        # Wiring
        self.add_series_btn.clicked.connect(self._add_series)

    def _add_series(self):
        s = {"dir_col": self.columns[0] if self.columns else "", "speed_col": self.columns[0] if self.columns else "", "label": ""}
        row = _SeriesRow(self.columns, s, self)
        row.remove_btn.clicked.connect(lambda _=None, r=row: self._remove_row(r))
        self.series_rows.append(row)
        parent_layout = self.add_series_btn.parentWidget().layout()  # type: ignore
        parent_layout.insertWidget(parent_layout.count() - 1, row)

    def _remove_row(self, row: _SeriesRow):
        try: self.series_rows.remove(row)
        except ValueError: pass
        row.setParent(None); row.deleteLater()

    def accept(self):
        p = self.spec.payload
        _ensure_payload_defaults(self.spec, self.columns, self.get_df)

        p["title"] = (self.title_edit.text().strip() or "Wind Rose"); self.spec.title = p["title"]
        p["series"] = [r.to_dict() for r in self.series_rows] or p["series"]
        p["bins"] = int(max(1, self.bins_spin.value()))
        p["opening"] = float(min(1.0, max(0.05, self.opening_spin.value())))
        p["legend"] = self.legend_check.isChecked()
        p["normalize"] = self.normalize_combo.currentText()
        p["ytick_step"] = int(self.ytick_step.value())
        p["ytick_max"] = int(self.ytick_max.value())
        p["edgecolor"] = self.edgecolor_btn.value() or "#ffffff"

        theme = {**_default_theme(), **(p.get("style") or {})}
        theme["axes_facecolor"] = self.facecolor_btn.value() or "#cccccc"
        theme["title_size"] = int(self.title_size.value())
        theme["title_color"] = self.title_color.value() or "#111111"
        p["style"] = theme

        p["date_col"] = self.date_col_combo.currentText().strip()
        p["date_from"] = self.date_from.date().toString("yyyy-MM-dd") if p["date_col"] else ""
        p["date_to"]   = self.date_to.date().toString("yyyy-MM-dd") if p["date_col"] else ""

        super().accept()


# --------------------- Renderer ---------------------
class WindRoseRenderer(QWidget):
    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame], columns: List[str],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        self.get_df = get_df
        self.columns = columns

        lay = QVBoxLayout(self)
        self.canvas = FigureCanvas(Figure(figsize=(7.6, 3.8), tight_layout=True))
        self.canvas.setMinimumHeight(240)
        lay.addWidget(self.canvas)
        self.refresh_data()

    # --- Helpers ---
    def _filter_by_date(self, df: pd.DataFrame, p: Dict[str, Any]) -> pd.DataFrame:
        dcol = p.get("date_col", "")
        if dcol and dcol in df.columns and pd.api.types.is_datetime64_any_dtype(df[dcol]):
            start = p.get("date_from") or ""
            end   = p.get("date_to") or ""
            try:
                if start:
                    df = df[df[dcol] >= pd.to_datetime(start)]
                if end:
                    # include the whole 'end' day
                    df = df[df[dcol] <= pd.to_datetime(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)]
            except Exception:
                pass
        return df

    def _subplot_geometry(self, n: int) -> Tuple[int, int]:
        if n <= 1: return 1, 1
        if n == 2: return 1, 2
        if n <= 4: return 2, 2
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        return rows, cols

    def _prepare_wind_data(self, df: pd.DataFrame, dir_col: str, spd_col: str) -> Tuple[np.ndarray, np.ndarray]:
        """Return (direction_degrees[0..360), speed>=0) as numpy arrays, NaNs/Infs removed."""
        d = df[dir_col].copy()
        v = df[spd_col].copy()

        # Coerce to numeric
        d = pd.to_numeric(d, errors="coerce")
        v = pd.to_numeric(v, errors="coerce")

        # Normalize directions into [0,360)
        d = d % 360.0

        # Filter: finite + speed >= 0
        mask = np.isfinite(d.values) & np.isfinite(v.values) & (v.values >= 0)
        d = d.values[mask].astype(float)
        v = v.values[mask].astype(float)
        return d, v

    def refresh_data(self):
        # Clear and draw an error message if windrose is missing
        if WindroseAxes is None:
            self.canvas.figure.clf()
            ax = self.canvas.figure.add_subplot(111)
            ax.text(0.5, 0.5, "windrose package not installed", ha="center", va="center")
            self.canvas.draw_idle()
            return

        df = self.get_df()
        p = self.spec.payload
        _ensure_payload_defaults(self.spec, self.columns, self.get_df)

        self.canvas.figure.clf()

        if df is None or df.empty or not p.get("series"):
            ax = self.canvas.figure.add_subplot(111)
            ax.text(0.5, 0.5, "No data / configure chart…", ha="center", va="center")
            self.canvas.draw_idle()
            return

        # Optional date filter
        df = self._filter_by_date(df, p)

        series = [s for s in p["series"] if s.get("dir_col") in df.columns and s.get("speed_col") in df.columns]
        if not series:
            ax = self.canvas.figure.add_subplot(111)
            ax.text(0.5, 0.5, "Pick direction & speed columns", ha="center", va="center")
            self.canvas.draw_idle()
            return

        rows, cols = self._subplot_geometry(len(series))
        theme = {**_default_theme(), **(p.get("style") or {})}

        # Figure title
        try:
            self.canvas.figure.suptitle(self.spec.title or "", fontsize=int(theme.get("title_size", 12)),
                                        color=theme.get("title_color", "#111111"))
        except Exception:
            pass

        bins = max(1, int(p.get("bins", 8)))
        opening = float(min(1.0, max(0.05, p.get("opening", 0.8))))
        edgecolor = p.get("edgecolor") or "#ffffff"
        normed = (str(p.get("normalize", "percent")).lower() == "percent")
        tick_sz = max(7, int(theme.get("tick_size", 9)))
        ystep = int(p.get("ytick_step", 4)) if normed else 0
        ymax_cfg = int(p.get("ytick_max", 0))

        any_drawn = False

        # Build each windrose
        idx = 1
        for s in series:
            ax = self.canvas.figure.add_subplot(rows, cols, idx, projection="windrose")
            idx += 1
            ax.set_facecolor(theme.get("axes_facecolor", "#cccccc"))

            try:
                d, v = self._prepare_wind_data(df, s["dir_col"], s["speed_col"])
                if d.size == 0 or v.size == 0:
                    ax.text(0.5, 0.5, "No valid data", ha="center", va="center")
                else:
                    ax.bar(d, v, bins=bins, normed=normed, opening=opening, edgecolor=edgecolor)

                    # Tick font sizes
                    for label in ax.get_xticklabels() + ax.get_yticklabels():
                        try:
                            label.set_fontsize(tick_sz)
                        except Exception:
                            pass

                    # Radial ticks (percent only)
                    if normed and ystep > 0:
                        try:
                            rmax = ax.get_rmax()
                            ymax = ymax_cfg if ymax_cfg > 0 else max(ystep, int(round(rmax)))
                            ax.set_yticks(list(range(0, ymax + 1, ystep)))
                            ax.set_yticklabels(list(range(0, ymax + 1, ystep)))
                        except Exception:
                            pass

                    if p.get("legend", True):
                        try:
                            ax.set_legend()
                        except Exception:
                            pass

                    any_drawn = True
            except Exception as err:
                ax.text(0.5, 0.5, f"WindRose error:\n{err}", ha="center", va="center")

            subtitle = s.get("label") or f"{s.get('dir_col')} vs {s.get('speed_col')}"
            try:
                ax.set_title(subtitle, y=1.05, fontsize=max(8, int(theme.get("label_size", 10))))
            except Exception:
                pass

        # Tight layout
        try:
            if any_drawn and theme.get("tight_layout", True):
                self.canvas.figure.tight_layout()
        except Exception:
            pass

        self.canvas.draw_idle()


# --------------------- Handler ---------------------
@register
class WindRoseHandler(TypeHandlerBase):
    kind = "WindRose"

    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        default_dir = next((c for c in columns if "dir" in c.lower() or "wd" in c.lower()), (columns[0] if columns else ""))
        default_speed = next((c for c in columns if "speed" in c.lower() or "ws" in c.lower()), (columns[0] if columns else ""))
        date_col = _first_datetime_column(columns, get_df)

        return {
            "title": "Wind Rose",
            "series": [{
                "dir_col": default_dir,
                "speed_col": default_speed,
                "label": "Windrose",
            }],
            "legend": True,
            "bins": 8,
            "opening": 0.8,
            "edgecolor": "#ffffff",
            "normalize": "percent",
            "ytick_step": 4,
            "ytick_max": 0,
            "date_col": date_col,
            "date_from": "",
            "date_to": "",
            "style": _default_theme(),
        }

    def create_editor(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None) -> QDialog:
        _ensure_payload_defaults(spec, columns, self._get_df_fallback)
        return WindRoseEditor(spec, columns, self._get_df_fallback, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[QWidget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        _ensure_payload_defaults(spec, columns, get_df)
        return WindRoseRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "title": flat.get("title", "Wind Rose"),
            "series": [{
                "dir_col": flat.get("dir_col", ""),
                "speed_col": flat.get("speed_col", ""),
                "label": flat.get("label", "Windrose"),
            }],
            "legend": flat.get("legend", True),
            "bins": int(flat.get("bins", 8)),
            "opening": float(flat.get("opening", 0.8)),
            "edgecolor": flat.get("edgecolor", "#ffffff"),
            "normalize": flat.get("normalize", "percent"),
            "ytick_step": int(flat.get("ytick_step", 4)),
            "ytick_max": int(flat.get("ytick_max", 0)),
            "date_col": flat.get("date_col", ""),
            "date_from": flat.get("date_from", ""),
            "date_to": flat.get("date_to", ""),
            "style": {**_default_theme()},
        }
        return payload

    def _get_df_fallback(self) -> pd.DataFrame:
        return pd.DataFrame()
