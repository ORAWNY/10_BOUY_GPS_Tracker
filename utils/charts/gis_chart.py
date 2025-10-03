# utils/charts/gis_chart.py
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional, Union

import os, re
import pandas as pd
import geopandas as gpd

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QDialog, QHBoxLayout,
    QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QPushButton, QLineEdit, QLabel,
    QFileDialog, QListWidget, QListWidgetItem, QSizePolicy, QTabWidget, QScrollArea, QFrame
)
from PyQt6.QtCore import Qt, QSize

from utils.charts import register, TypeHandlerBase, ChartSpec
from utils.local_gis_viewer import LocalGISViewer, Layer


# -------------------- helpers --------------------
def _scrollable(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidget(inner)
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    sa.setFrameShape(QFrame.Shape.NoFrame)  # <-- PyQt6 enum namespace
    return sa

def _numbers(s: str) -> List[float]:
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", s or "")]

def _hemi_sign(s: str, is_lat: bool) -> int:
    m = re.search(r"[NSEW]", (s or "").upper())
    if not m:
        return 1
    h = m.group(0)
    if is_lat:
        return -1 if h == "S" else 1
    return -1 if h == "W" else 1

def _to_dd(value: Union[str, float, int], fmt: str, *, is_lat: bool) -> Optional[float]:
    """
    Convert coordinate to Decimal Degrees.
    DD  : -41.288, 174.777 (optional N/S/E/W)
    DM  : 41 17.28 S     or 41°17.28' S
    DMS : 41 17 16.8 S   or 41°17'16.8"S
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and fmt == "DD":
        dd = float(value)
        if is_lat and not (-90 <= dd <= 90): return None
        if not is_lat and not (-180 <= dd <= 180): return None
        return dd

    s = str(value).strip()
    if not s:
        return None

    nums = _numbers(s)
    if not nums:
        return None

    # precedence: explicit sign OR hemisphere letter
    sign = -1 if s.lstrip().startswith("-") else _hemi_sign(s, is_lat)

    try:
        if fmt == "DD":
            dd = float(nums[0])
        elif fmt == "DM":
            if len(nums) < 2: return None
            deg, minutes = float(nums[0]), float(nums[1])
            dd = abs(deg) + minutes / 60.0
        elif fmt == "DMS":
            if len(nums) < 3: return None
            deg, minutes, seconds = float(nums[0]), float(nums[1]), float(nums[2])
            dd = abs(deg) + minutes / 60.0 + seconds / 3600.0
        else:
            dd = float(nums[0])
        dd *= sign
        if is_lat and not (-90 <= dd <= 90): return None
        if not is_lat and not (-180 <= dd <= 180): return None
        return dd
    except Exception:
        return None

def _convert_df_to_dd(df: pd.DataFrame, lat_col: str, lon_col: str, fmt: str) -> pd.DataFrame:
    out = pd.DataFrame()
    if df is None or df.empty or lat_col not in df.columns or lon_col not in df.columns:
        return out
    lat_src = df[lat_col]
    lon_src = df[lon_col]

    fmt = (fmt or "DD").upper()
    if fmt == "DD":
        out["Lat"] = pd.to_numeric(lat_src, errors="coerce")
        out["Lon"] = pd.to_numeric(lon_src, errors="coerce")
    else:
        out["Lat"] = lat_src.map(lambda v: _to_dd(v, fmt, is_lat=True))
        out["Lon"] = lon_src.map(lambda v: _to_dd(v, fmt, is_lat=False))

    out = out.dropna(subset=["Lat", "Lon"]).astype(float)
    return out


def _default_layers_payload() -> List[Dict[str, Any]]:
    return []

# caches
_CSV_POINTS_CACHE: dict[str, pd.DataFrame] = {}
_SHP_GDF_CACHE: dict[str, gpd.GeoDataFrame] = {}


# ==================== Editor ====================
class GISEditor(QDialog):
    """
    Scrollable, tabbed editor:
      • Coordinates & Projection  — columns, input format (DD/DM/DMS), display CRS
      • Heatmap                   — on/off, radius (m), grid resolution, colormap, alpha
      • Layers                    — builtin + external layers (name/visible/symbols editing)
      • Appearance                — basemap theme + coastlines
      • Frame                     — map size (px), square option, legend, styles, aids
      • Labels & Axis             — grid/XY line markers and units (DD/meters)
    """
    _BUILTIN_ID = "__builtin_points__"

    def __init__(self, spec: ChartSpec, columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("GIS Map Settings")
        self.setModal(True)
        self.spec = spec
        self.columns = columns

        p = self.spec.payload
        # ---- defaults ----
        p.setdefault("lon_col", "Lon" if "Lon" in columns else (columns[0] if columns else ""))
        p.setdefault("lat_col", "Lat" if "Lat" in columns else (columns[1] if len(columns) > 1 else ""))
        p.setdefault("coord_input_format", "DD")          # "DD" | "DM" | "DMS"
        p.setdefault("display_crs", "EPSG:4326")          # viewer target; data in DD
        p.setdefault("heatmap", True)
        p.setdefault("heat_radius_m", 200.0)
        p.setdefault("heat_resolution", 64)
        p.setdefault("heatmap_cmap", "jet")
        p.setdefault("heat_alpha", 0.55)
        p.setdefault("show_radii", True)
        p.setdefault("r1", 25)
        p.setdefault("r2", 50)
        p.setdefault("g_radius_m", 0)
        p.setdefault("show_centroid", True)
        p.setdefault("centroid_pct", 100.0)
        p.setdefault("layers", _default_layers_payload())
        p.setdefault("base_name", "Buoy Points")
        p.setdefault("base_visible", True)
        p.setdefault("base_marker", "o")
        p.setdefault("base_size", 8.0)
        p.setdefault("base_color", "#1f77b4")
        p.setdefault("base_alpha", 0.7)
        p.setdefault("vector_color", "#ffd54f")
        p.setdefault("vector_linewidth", 1.2)
        p.setdefault("map_width", 720)
        p.setdefault("map_height", 480)
        p.setdefault("square_map", True)
        p.setdefault("show_legend", True)
        p.setdefault("show_grid", True)
        p.setdefault("grid_units", "DD")
        p.setdefault("basemap_theme", "None")             # None | Light | Dark | Blue | Gray
        p.setdefault("show_coastlines", False)
        p.setdefault("coastline_color", "#666666")
        p.setdefault("title", self.spec.title or "GIS Map")

        root = QVBoxLayout(self)

        tabs = QTabWidget()
        root.addWidget(tabs)

        # ---------- Coordinates & Projection ----------
        c_widget = QWidget(); c_form = QFormLayout(c_widget)
        self.title_edit = QLineEdit(p.get("title", self.spec.title or "GIS Map"))
        self.lon_combo = QComboBox(); self.lon_combo.addItems([""] + columns); self.lon_combo.setCurrentText(p["lon_col"])
        self.lat_combo = QComboBox(); self.lat_combo.addItems([""] + columns); self.lat_combo.setCurrentText(p["lat_col"])
        self.format_combo = QComboBox(); self.format_combo.addItems(["DD", "DM", "DMS"]); self.format_combo.setCurrentText(p.get("coord_input_format","DD"))
        self.crs_combo = QComboBox()
        self.crs_combo.addItems(["EPSG:4326 (WGS84 — DD)", "EPSG:3857 (Web Mercator — meters)"])
        crs_display = "EPSG:4326 (WGS84 — DD)" if str(p.get("display_crs","EPSG:4326")).upper().endswith("4326") \
                      else "EPSG:3857 (Web Mercator — meters)"
        self.crs_combo.setCurrentText(crs_display)
        c_form.addRow("Title:", self.title_edit)
        c_form.addRow("Longitude column:", self.lon_combo)
        c_form.addRow("Latitude column:", self.lat_combo)
        c_form.addRow("Input format:", self.format_combo)
        c_form.addRow("Display CRS:", self.crs_combo)
        tabs.addTab(_scrollable(c_widget), "Coordinates & Projection")

        # ---------- Heatmap ----------
        h_widget = QWidget(); h_form = QFormLayout(h_widget)
        self.chk_heat = QCheckBox(); self.chk_heat.setChecked(bool(p.get("heatmap", True)))
        self.heat_radius = QDoubleSpinBox(); self.heat_radius.setRange(1.0, 100000.0); self.heat_radius.setValue(float(p.get("heat_radius_m", 200.0)))
        self.heat_res = QSpinBox(); self.heat_res.setRange(8, 2048); self.heat_res.setValue(int(p.get("heat_resolution", 64)))
        self.heat_cmap = QComboBox()
        self.heat_cmap.addItems(["jet","turbo","viridis","plasma","inferno","magma","cividis","hot","coolwarm","Greens","Blues","Reds","Purples","gray"])
        self.heat_cmap.setCurrentText(str(p.get("heatmap_cmap","jet")))
        self.heat_alpha = QDoubleSpinBox(); self.heat_alpha.setRange(0.0, 1.0); self.heat_alpha.setSingleStep(0.05); self.heat_alpha.setValue(float(p.get("heat_alpha", 0.55)))
        h_form.addRow("Enable heatmap:", self.chk_heat)
        h_form.addRow("Interpolation radius (m):", self.heat_radius)
        h_form.addRow("Grid resolution:", self.heat_res)
        h_form.addRow("Colormap:", self.heat_cmap)
        h_form.addRow("Heatmap alpha:", self.heat_alpha)
        tabs.addTab(_scrollable(h_widget), "Heatmap")

        # ---------- Layers (list + editor) ----------
        lyr_widget = QWidget(); lyr_v = QVBoxLayout(lyr_widget)
        lyr_v.addWidget(QLabel("<b>Layers</b> (builtin + external)"))
        self.layers_list = QListWidget(); self.layers_list.setSelectionMode(self.layers_list.SelectionMode.SingleSelection)
        lyr_v.addWidget(self.layers_list)

        # Editor panel (name/visibility + symbol props)
        lyr_formw = QWidget(); lyr_form = QFormLayout(lyr_formw)
        self.lyr_kind = QLabel("—")
        self.lyr_name = QLineEdit()
        self.lyr_visible = QCheckBox()
        # points
        self.lyr_marker = QComboBox(); self.lyr_marker.addItems(["o",".","x","^","s","+","*","D"])
        self.lyr_size = QDoubleSpinBox(); self.lyr_size.setRange(0.5, 200.0)
        self.lyr_color = QLineEdit()
        self.lyr_alpha = QDoubleSpinBox(); self.lyr_alpha.setRange(0.0, 1.0); self.lyr_alpha.setSingleStep(0.05)
        # vectors
        self.lyr_linecolor = QLineEdit()
        self.lyr_linewidth = QDoubleSpinBox(); self.lyr_linewidth.setRange(0.1, 20.0); self.lyr_linewidth.setSingleStep(0.1)

        lyr_form.addRow("Kind:", self.lyr_kind)
        lyr_form.addRow("Name:", self.lyr_name)
        lyr_form.addRow("Visible:", self.lyr_visible)
        lyr_form.addRow(QLabel("<b>Point style</b>"))
        lyr_form.addRow("Marker:", self.lyr_marker)
        lyr_form.addRow("Size:", self.lyr_size)
        lyr_form.addRow("Color:", self.lyr_color)
        lyr_form.addRow("Alpha:", self.lyr_alpha)
        lyr_form.addRow(QLabel("<b>Vector style</b>"))
        lyr_form.addRow("Line color:", self.lyr_linecolor)
        lyr_form.addRow("Line width:", self.lyr_linewidth)

        lyr_v.addWidget(lyr_formw)

        # Buttons row
        btnrow = QHBoxLayout()
        self.btn_add_shp = QPushButton("Add Shapefile…")
        self.btn_add_csv = QPushButton("Add CSV (Lat/Lon)…")
        self.btn_toggle_vis = QPushButton("Toggle Visible")
        self.btn_remove = QPushButton("Remove")
        self.btn_apply_layer = QPushButton("Apply layer changes")
        btnrow.addWidget(self.btn_add_shp); btnrow.addWidget(self.btn_add_csv)
        btnrow.addStretch(1)
        btnrow.addWidget(self.btn_toggle_vis); btnrow.addWidget(self.btn_remove); btnrow.addWidget(self.btn_apply_layer)
        lyr_v.addLayout(btnrow)

        self._layers_payload: List[Dict[str, Any]] = list(p.get("layers", []))
        self._refresh_layer_list()
        self.layers_list.currentRowChanged.connect(self._on_select_layer)

        self.btn_add_shp.clicked.connect(self._on_add_shp)
        self.btn_add_csv.clicked.connect(self._on_add_csv)
        self.btn_remove.clicked.connect(self._on_remove_layer)
        self.btn_toggle_vis.clicked.connect(self._on_toggle_layer)
        self.btn_apply_layer.clicked.connect(self._on_apply_layer_changes)

        tabs.addTab(_scrollable(lyr_widget), "Layers")

        # ---------- Appearance ----------
        a2_widget = QWidget(); a2_form = QFormLayout(a2_widget)
        self.basemap_theme = QComboBox()
        self.basemap_theme.addItems(["None","Light","Dark","Blue","Gray"])
        self.basemap_theme.setCurrentText(str(p.get("basemap_theme","None")))
        self.coast_chk = QCheckBox(); self.coast_chk.setChecked(bool(p.get("show_coastlines", False)))
        self.coast_color = QLineEdit(str(p.get("coastline_color","#666666")))
        a2_form.addRow("Basemap theme:", self.basemap_theme)
        a2_form.addRow("Show coastlines:", self.coast_chk)
        a2_form.addRow("Coastlines color:", self.coast_color)
        tabs.addTab(_scrollable(a2_widget), "Appearance")

        # ---------- Frame ----------
        f_widget = QWidget(); f_form = QFormLayout(f_widget)
        self.map_w = QSpinBox(); self.map_w.setRange(200, 8000); self.map_w.setValue(int(p.get("map_width", 720)))
        self.map_h = QSpinBox(); self.map_h.setRange(200, 8000); self.map_h.setValue(int(p.get("map_height", 480)))
        self.chk_square = QCheckBox(); self.chk_square.setChecked(bool(p.get("square_map", True)))
        self.chk_legend = QCheckBox(); self.chk_legend.setChecked(bool(p.get("show_legend", True)))
        # working aids
        self.chk_radii = QCheckBox(); self.chk_radii.setChecked(bool(p.get("show_radii", True)))
        self.r1 = QSpinBox(); self.r1.setRange(0, 1_000_000); self.r1.setValue(int(p.get("r1", 25)))
        self.r2 = QSpinBox(); self.r2.setRange(0, 1_000_000); self.r2.setValue(int(p.get("r2", 50)))
        self.rg = QSpinBox();
        self.rg.setRange(0, 1_000_000);
        self.rg.setValue(int(p.get("g_radius_m", 0)))
        f_form.addRow("Green radius (m):", self.rg)

        self.chk_centroid = QCheckBox(); self.chk_centroid.setChecked(bool(p.get("show_centroid", True)))
        self.cfrac = QDoubleSpinBox(); self.cfrac.setRange(0.0, 100.0); self.cfrac.setSingleStep(1.0); self.cfrac.setValue(float(p.get("centroid_pct", 100.0)))
        # builtin points styles (kept here, also editable through “Layers” when selecting builtin)
        self.base_marker = QLineEdit(str(p.get("base_marker", "o")))
        self.base_size = QDoubleSpinBox(); self.base_size.setRange(0.5, 200.0); self.base_size.setValue(float(p.get("base_size", 8.0)))
        self.base_color = QLineEdit(str(p.get("base_color", "#1f77b4")))
        self.base_alpha = QDoubleSpinBox(); self.base_alpha.setRange(0.0, 1.0); self.base_alpha.setSingleStep(0.1); self.base_alpha.setValue(float(p.get("base_alpha", 0.7)))
        self.vector_color = QLineEdit(str(p.get("vector_color", "#ffd54f")))
        self.vector_linewidth = QDoubleSpinBox(); self.vector_linewidth.setRange(0.1, 12.0); self.vector_linewidth.setSingleStep(0.1); self.vector_linewidth.setValue(float(p.get("vector_linewidth", 1.2)))

        f_form.addRow("Map width (px):", self.map_w)
        f_form.addRow("Map height (px):", self.map_h)
        f_form.addRow("Square map (force 1:1):", self.chk_square)
        f_form.addRow("Show legend:", self.chk_legend)
        f_form.addRow("Show working radii:", self.chk_radii)
        f_form.addRow("Radius 1 (m):", self.r1)
        f_form.addRow("Radius 2 (m):", self.r2)
        f_form.addRow("Show centroid:", self.chk_centroid)
        f_form.addRow("Centroid window (% recent):", self.cfrac)
        f_form.addRow(QLabel("<b>Default styles</b>"))
        f_form.addRow("Base marker (o,.,x,^):", self.base_marker)
        f_form.addRow("Base size:", self.base_size)
        f_form.addRow("Base color:", self.base_color)
        f_form.addRow("Base alpha:", self.base_alpha)
        f_form.addRow("Vector color:", self.vector_color)
        f_form.addRow("Vector linewidth:", self.vector_linewidth)
        tabs.addTab(_scrollable(f_widget), "Frame")

        # keep width/height synced when square is enabled
        def _enforce_square_now():
            if self.chk_square.isChecked():
                side = min(self.map_w.value(), self.map_h.value())
                self.map_w.blockSignals(True); self.map_h.blockSignals(True)
                self.map_w.setValue(side); self.map_h.setValue(side)
                self.map_w.blockSignals(False); self.map_h.blockSignals(False)

        def _maybe_sync_square_w(val: int):
            if self.chk_square.isChecked():
                self.map_h.blockSignals(True); self.map_h.setValue(val); self.map_h.blockSignals(False)

        def _maybe_sync_square_h(val: int):
            if self.chk_square.isChecked():
                self.map_w.blockSignals(True); self.map_w.setValue(val); self.map_w.blockSignals(False)

        self.chk_square.toggled.connect(lambda _: _enforce_square_now())
        self.map_w.valueChanged.connect(_maybe_sync_square_w)
        self.map_h.valueChanged.connect(_maybe_sync_square_h)

        # ---------- Labels & Axis ----------
        a_widget = QWidget(); a_form = QFormLayout(a_widget)
        self.chk_grid = QCheckBox(); self.chk_grid.setChecked(bool(p.get("show_grid", True)))
        self.grid_units = QComboBox(); self.grid_units.addItems(["DD", "Meters"]); self.grid_units.setCurrentText(str(p.get("grid_units","DD")))
        a_form.addRow("Show XY grid / line markers:", self.chk_grid)
        a_form.addRow("Grid / axis units:", self.grid_units)
        tabs.addTab(_scrollable(a_widget), "Labels & Axis")

        # ---- Buttons (Apply / OK / Cancel) ----
        okrow = QHBoxLayout(); okrow.addStretch(1)
        btn_apply = QPushButton("Apply"); ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        okrow.addWidget(btn_apply); okrow.addWidget(ok); okrow.addWidget(cancel)
        root.addLayout(okrow)

        btn_apply.clicked.connect(lambda: self._apply_to_payload(refresh_only=True))
        ok.clicked.connect(lambda: (self._apply_to_payload(refresh_only=False), self.accept()))
        cancel.clicked.connect(self.reject)

        # Select the first layer entry by default
        if self.layers_list.count() > 0:
            self.layers_list.setCurrentRow(0)

    # ---- Layers helpers ----
    def _refresh_layer_list(self):
        self.layers_list.clear()
        # Builtin (virtual) first
        p = self.spec.payload
        bi = QListWidgetItem(f"👁 {p.get('base_name','Buoy Points')} — (built-in)")
        bi.setData(Qt.ItemDataRole.UserRole, {"__id": self._BUILTIN_ID, "kind": "points"})
        self.layers_list.addItem(bi)
        # External
        for lyr in (self._layers_payload or []):
            vis = "👁 " if lyr.get("visible", True) else "🚫 "
            item = QListWidgetItem(f"{vis}{lyr.get('name','(unnamed)')} — {os.path.basename(lyr.get('path',''))}")
            item.setData(Qt.ItemDataRole.UserRole, lyr)
            self.layers_list.addItem(item)

    def _selected_payload_index(self) -> int:
        """Return index into self._layers_payload (external only) for current selection, or -1."""
        row = self.layers_list.currentRow()
        if row <= 0:  # 0 is builtin
            return -1
        idx = row - 1
        return idx if 0 <= idx < len(self._layers_payload) else -1

    def _on_add_shp(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Shapefile", "", "Shapefiles (*.shp)")
        if not path: return
        name = os.path.splitext(os.path.basename(path))[0]
        lyr = {"type": "shp", "path": path, "name": name, "visible": True,
               "style": {"linecolor": self.vector_color.text().strip() or "#ffd54f",
                         "linewidth": float(self.vector_linewidth.value())}}
        self._layers_payload.append(lyr); self._refresh_layer_list()

    def _on_add_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open CSV with Lat/Lon", "", "CSV files (*.csv)")
        if not path: return
        name = os.path.splitext(os.path.basename(path))[0]
        lyr = {"type": "csv", "path": path, "name": name, "visible": True,
               "style": {"marker": self.base_marker.text().strip() or "o",
                         "size": float(self.base_size.value()),
                         "color": self.base_color.text().strip() or "#1f77b4",
                         "alpha": float(self.base_alpha.value())}}
        self._layers_payload.append(lyr); self._refresh_layer_list()

    def _on_remove_layer(self):
        idx = self._selected_payload_index()
        if idx >= 0:
            self._layers_payload.pop(idx)
            self._refresh_layer_list()

    def _on_toggle_layer(self):
        row = self.layers_list.currentRow()
        if row == 0:
            # builtin toggle maps to base_visible
            self.spec.payload["base_visible"] = not bool(self.spec.payload.get("base_visible", True))
            self._refresh_layer_list()
            return
        idx = self._selected_payload_index()
        if idx >= 0:
            self._layers_payload[idx]["visible"] = not bool(self._layers_payload[idx].get("visible", True))
            self._refresh_layer_list()

    # ---- Layer editor wiring ----
    def _on_select_layer(self, row: int):
        # Reset fields
        self.lyr_kind.setText("—")
        for w in (self.lyr_name, self.lyr_color, self.lyr_linecolor):
            w.setText("")
        self.lyr_visible.setChecked(True)
        self.lyr_marker.setCurrentText("o")
        self.lyr_size.setValue(8.0)
        self.lyr_alpha.setValue(0.7)
        self.lyr_linewidth.setValue(1.2)

        if row < 0:
            return

        if row == 0:
            # builtin
            p = self.spec.payload
            self.lyr_kind.setText("Points (built-in)")
            self.lyr_name.setText(p.get("base_name","Buoy Points"))
            self.lyr_visible.setChecked(bool(p.get("base_visible", True)))
            self.lyr_marker.setCurrentText(str(p.get("base_marker","o")))
            self.lyr_size.setValue(float(p.get("base_size",8.0)))
            self.lyr_color.setText(str(p.get("base_color","#1f77b4")))
            self.lyr_alpha.setValue(float(p.get("base_alpha",0.7)))
            # vector style fields disabled for builtin
            return

        # external
        lyr = self._layers_payload[self._selected_payload_index()]
        k = "points" if lyr.get("type") == "csv" else "vector"
        self.lyr_kind.setText("Points" if k == "points" else "Vector")
        self.lyr_name.setText(str(lyr.get("name","(unnamed)")))
        self.lyr_visible.setChecked(bool(lyr.get("visible", True)))
        st = lyr.get("style") or {}
        if k == "points":
            self.lyr_marker.setCurrentText(str(st.get("marker","o")))
            self.lyr_size.setValue(float(st.get("size",8.0)))
            self.lyr_color.setText(str(st.get("color","#1f77b4")))
            self.lyr_alpha.setValue(float(st.get("alpha",0.7)))
        else:
            self.lyr_linecolor.setText(str(st.get("linecolor","#ffd54f")))
            self.lyr_linewidth.setValue(float(st.get("linewidth",1.2)))

    def _on_apply_layer_changes(self):
        row = self.layers_list.currentRow()
        if row < 0:
            return
        if row == 0:
            # builtin -> map to base_* fields
            p = self.spec.payload
            p["base_name"] = self.lyr_name.text().strip() or "Buoy Points"
            p["base_visible"] = bool(self.lyr_visible.isChecked())
            p["base_marker"] = self.lyr_marker.currentText() or "o"
            p["base_size"] = float(self.lyr_size.value())
            p["base_color"] = self.lyr_color.text().strip() or "#1f77b4"
            p["base_alpha"] = float(self.lyr_alpha.value())
            self._refresh_layer_list()
            self._request_live_refresh()
            return

        # external
        idx = self._selected_payload_index()
        if idx < 0:
            return
        lyr = self._layers_payload[idx]
        lyr["name"] = self.lyr_name.text().strip() or lyr.get("name","(unnamed)")
        lyr["visible"] = bool(self.lyr_visible.isChecked())
        st = dict(lyr.get("style") or {})
        if lyr.get("type") == "csv":
            st["marker"] = self.lyr_marker.currentText() or st.get("marker","o")
            st["size"] = float(self.lyr_size.value())
            st["color"] = self.lyr_color.text().strip() or st.get("color","#1f77b4")
            st["alpha"] = float(self.lyr_alpha.value())
        else:
            st["linecolor"] = self.lyr_linecolor.text().strip() or st.get("linecolor","#ffd54f")
            st["linewidth"] = float(self.lyr_linewidth.value())
        lyr["style"] = st
        self._refresh_layer_list()
        self._request_live_refresh()

    # ---- Apply payload (used by Apply and OK) ----
    def _apply_to_payload(self, refresh_only: bool):
        p = self.spec.payload
        title = self.title_edit.text().strip() or "GIS Map"
        p["title"] = title; self.spec.title = title

        p["lon_col"] = self.lon_combo.currentText()
        p["lat_col"] = self.lat_combo.currentText()
        p["coord_input_format"] = self.format_combo.currentText()

        crs_label = self.crs_combo.currentText()
        p["display_crs"] = "EPSG:4326" if "4326" in crs_label else "EPSG:3857"

        p["heatmap"] = self.chk_heat.isChecked()
        p["heat_radius_m"] = float(self.heat_radius.value())
        p["heat_resolution"] = int(self.heat_res.value())
        p["heatmap_cmap"] = self.heat_cmap.currentText()
        p["heat_alpha"] = float(self.heat_alpha.value())

        p["map_width"] = int(self.map_w.value())
        p["map_height"] = int(self.map_h.value())
        p["square_map"] = bool(self.chk_square.isChecked())
        p["show_legend"] = self.chk_legend.isChecked()

        p["show_radii"] = self.chk_radii.isChecked()
        p["r1"] = int(self.r1.value()); p["r2"] = int(self.r2.value())
        p["show_centroid"] = self.chk_centroid.isChecked()
        p["centroid_pct"] = float(self.cfrac.value())

        p["base_marker"] = self.base_marker.text().strip() or "o"
        p["base_size"] = float(self.base_size.value())
        p["base_color"] = self.base_color.text().strip() or "#1f77b4"
        p["base_alpha"] = float(self.base_alpha.value())
        p["vector_color"] = self.vector_color.text().strip() or "#ffd54f"
        p["vector_linewidth"] = float(self.vector_linewidth.value())

        p["show_grid"] = self.chk_grid.isChecked()
        p["grid_units"] = self.grid_units.currentText()

        p["basemap_theme"] = self.basemap_theme.currentText()
        p["show_coastlines"] = bool(self.coast_chk.isChecked())
        p["coastline_color"] = self.coast_color.text().strip() or "#666666"

        p["layers"] = list(self._layers_payload)

        self._request_live_refresh()

    # ---- Live refresh hook finder ----
    def _request_live_refresh(self):
        """
        1) Refresh all GISRenderer instances tied to this ChartSpec (fast path).
        2) If that fails, try common parent hooks and finally a broad widget scan.
        """
        # --- NEW: direct refresh via registry ---
        try:
            GISRenderer.refresh_by_spec(self.spec)
            return
        except Exception:
            pass

        # --- existing fallbacks ---
        parent = self.parent()
        visited = set()
        while parent and id(parent) not in visited:
            visited.add(id(parent))
            try:
                if hasattr(parent, "on_chart_apply"):
                    parent.on_chart_apply(self.spec); return
                if hasattr(parent, "refresh_chart"):
                    parent.refresh_chart(self.spec); return
                if hasattr(parent, "refresh_current_panel"):
                    parent.refresh_current_panel(); return
                if hasattr(parent, "refresh_all"):
                    parent.refresh_all(); return
                if hasattr(parent, "refresh"):
                    parent.refresh(); return
            except Exception:
                pass
            try:
                parent = parent.parent()
            except Exception:
                break

        # final broad scan stays the same ...
        try:
            from PyQt6.QtWidgets import QApplication, QWidget
            roots = []
            w = self.parent()
            while w and isinstance(w, QWidget):
                roots.append(w); w = w.parent()
            roots.extend(QApplication.topLevelWidgets())
            seen = set()
            for root in roots:
                if id(root) in seen: continue
                seen.add(id(root))
                for child in root.findChildren(QWidget):
                    try:
                        if getattr(child, "is_gis_renderer", False) and hasattr(child, "refresh_data"):
                            child.refresh_data()
                    except Exception:
                        pass
        except Exception:
            pass

    def accept(self):
        self._apply_to_payload(refresh_only=False)
        super().accept()


# ==================== Renderer ====================
class GISRenderer(QWidget):
    """Renderer with DD-standardised coordinates and optional heatmap/grid/legend sizing."""
    is_gis_renderer = True

    # --- NEW: registry so editors can find renderers by spec ---
    _RENDERERS_BY_SPEC: dict[int, list["GISRenderer"]] = {}

    @classmethod
    def refresh_by_spec(cls, spec):
        for r in list(cls._RENDERERS_BY_SPEC.get(id(spec), []) or []):
            try:
                r.refresh_data()
            except Exception:
                pass

    def __init__(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                 columns: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.spec = spec
        # register
        GISRenderer._RENDERERS_BY_SPEC.setdefault(id(self.spec), []).append(self)
        self.get_df = get_df
        self.columns = columns

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.viewer = LocalGISViewer(
            pd.DataFrame(columns=["Lat", "Lon"]),
            table_name=(self.spec.title or "GIS Map"),
            radius1=25, radius2=50,
            parent=self
        )
        self.viewer.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lay.addWidget(self.viewer, 0, Qt.AlignmentFlag.AlignCenter)
        self.refresh_data()

    def closeEvent(self, e):
        try:
            lst = GISRenderer._RENDERERS_BY_SPEC.get(id(self.spec))
            if lst and self in lst:
                lst.remove(self)
            if lst and not lst:
                GISRenderer._RENDERERS_BY_SPEC.pop(id(self.spec), None)
        except Exception:
            pass
        super().closeEvent(e)

    def sizeHint(self):
        p = self.spec.payload or {}
        w = int(p.get("map_width", 720)); h = int(p.get("map_height", 480))
        if bool(p.get("square_map", True)):
            side = min(w, h); w = h = side
        # leave room for toolbar to avoid clipping
        extra_h = self.viewer.toolbar.sizeHint().height() + 8
        return QSize(w, h + extra_h)



    def _load_external_layers(self, payload_layers: List[Dict[str, Any]]) -> List[Layer]:
        out: List[Layer] = []
        for ent in (payload_layers or []):
            try:
                ltype = ent.get("type")
                path = ent.get("path")
                name = ent.get("name") or os.path.basename(path or "") or "(layer)"
                vis = bool(ent.get("visible", True))
                style = ent.get("style") or {}
                if not vis:
                    continue
                if ltype == "csv" and path:
                    df = _CSV_POINTS_CACHE.get(path)
                    if df is None:
                        raw = pd.read_csv(path)
                        lat_col = next((c for c in raw.columns if c.lower() in ("lat", "latitude")), None)
                        lon_col = next((c for c in raw.columns if c.lower() in ("lon", "lng", "longitude")), None)
                        if not lat_col or not lon_col:
                            continue
                        raw["Lat"] = pd.to_numeric(raw[lat_col], errors="coerce")
                        raw["Lon"] = pd.to_numeric(raw[lon_col], errors="coerce")
                        raw.dropna(subset=["Lat", "Lon"], inplace=True)
                        df = raw[["Lat", "Lon"]].copy()
                        _CSV_POINTS_CACHE[path] = df
                    out.append(Layer(name=name, kind="points_df", df=df, visible=True, style=style))
                elif ltype == "shp" and path:
                    gdf = _SHP_GDF_CACHE.get(path)
                    if gdf is None:
                        gdf = gpd.read_file(path)
                        if gdf.crs is not None and (gdf.crs.to_epsg() or 4326) != 4326:
                            gdf = gdf.to_crs(4326)
                        _SHP_GDF_CACHE[path] = gdf
                    out.append(Layer(name=name, kind="vector_gdf", gdf=gdf, visible=True, style=style))
            except Exception:
                continue
        return out

    def refresh_data(self):
        df = self.get_df()
        p = self.spec.payload or {}

        # ---- (1) Convert to Decimal Degrees based on chosen input format ----
        if df is None or df.empty or not p.get("lon_col") or not p.get("lat_col"):
            dmap = pd.DataFrame(columns=["Lat", "Lon"])
        else:
            fmt = str(p.get("coord_input_format", "DD")).upper()
            dmap = _convert_df_to_dd(df, str(p["lat_col"]), str(p["lon_col"]), fmt)

            # (2) canvas size
            w = int(p.get("map_width", 720));
            h = int(p.get("map_height", 480))
            if bool(p.get("square_map", True)):
                side = min(w, h);
                w = h = side

            # NEW: tell the viewer whether to keep the frame square
            if hasattr(self.viewer, "set_force_square"):
                self.viewer.set_force_square(bool(p.get("square_map", True)))

            if hasattr(self.viewer, "set_canvas_size"):
                try:
                    self.viewer.set_canvas_size(w, h)
                except Exception:
                    self.viewer.setFixedSize(w, h)
            else:
                self.viewer.setFixedSize(w, h)

        # ---- (3) Push data & styles ----
        self.viewer.set_data(dmap)

        # Builtin (base) layer config
        if hasattr(self.viewer, "set_base_name"):
            self.viewer.set_base_name(p.get("base_name","Buoy Points"))
        if hasattr(self.viewer, "set_base_visible"):
            self.viewer.set_base_visible(bool(p.get("base_visible", True)))

        self.viewer.set_base_point_style(
            marker=p.get("base_marker", "o"),
            size=float(p.get("base_size", 8.0)),
            color=p.get("base_color", "#1f77b4"),
            alpha=float(p.get("base_alpha", 0.7)),
        )
        self.viewer.set_vector_style(
            color=p.get("vector_color", "#ffd54f"),
            linewidth=float(p.get("vector_linewidth", 1.2)),
        )

        # Working radii / centroid — 3-ring support
        if p.get("show_radii", True):
            g = float(p.get("g_radius_m", p.get("green_m", p.get("green", 0.0))))
            a = float(p.get("r1", p.get("amber_m", p.get("amber", 25))))
            r = float(p.get("r2", p.get("red_m", p.get("red", 50))))
            if hasattr(self.viewer, "set_status_radii"):
                self.viewer.set_status_radii(g, a, r)
            else:
                self.viewer.set_radii(int(a), int(r))
        else:
            if hasattr(self.viewer, "set_status_radii"):
                self.viewer.set_status_radii(0.0, 0.0, 0.0)
            else:
                self.viewer.set_radii(0, 0)

        self.viewer.set_show_centroid(bool(p.get("show_centroid", True)))
        self.viewer.set_centroid_fraction(float(p.get("centroid_pct", 100.0)) / 100.0)

        # Heatmap + params
        self.viewer.set_heatmap(bool(p.get("heatmap", True)))
        if hasattr(self.viewer, "set_heatmap_params"):
            try:
                self.viewer.set_heatmap_params(
                    radius_m=float(p.get("heat_radius_m", 200.0)),
                    resolution=int(p.get("heat_resolution", 64)),
                )
            except Exception:
                pass
        if hasattr(self.viewer, "set_heatmap_cmap"):
            self.viewer.set_heatmap_cmap(str(p.get("heatmap_cmap","jet")))
        if hasattr(self.viewer, "set_heatmap_alpha"):
            self.viewer.set_heatmap_alpha(float(p.get("heat_alpha", 0.55)))

        # Legend
        if hasattr(self.viewer, "set_show_legend"):
            try: self.viewer.set_show_legend(bool(p.get("show_legend", True)))
            except Exception: pass

        # Grid
        if hasattr(self.viewer, "set_grid"):
            try:
                self.viewer.set_grid(
                    show=bool(p.get("show_grid", True)),
                    units=str(p.get("grid_units", "DD")).upper(),
                )
            except Exception:
                pass

        # CRS / Projection (viewer may ignore if not supported)
        if hasattr(self.viewer, "set_display_crs"):
            try: self.viewer.set_display_crs(str(p.get("display_crs", "EPSG:4326")))
            except Exception: pass

        # Appearance
        if hasattr(self.viewer, "set_basemap_theme"):
            self.viewer.set_basemap_theme(str(p.get("basemap_theme","None")))
        if hasattr(self.viewer, "set_coastlines"):
            self.viewer.set_coastlines(bool(p.get("show_coastlines", False)), color=str(p.get("coastline_color","#666666")))

        # External layers
        layers = self._load_external_layers(p.get("layers") or [])
        self.viewer.set_external_layers(layers)

        # ---- (4) Refit after any size/data change ----
        for meth in ("fit_to_view", "fit_to_data", "refit", "refresh"):
            if hasattr(self.viewer, meth):
                try:
                    getattr(self.viewer, meth)()
                    break
                except Exception:
                    pass


# ==================== Handler ====================
@register
class GISHandler(TypeHandlerBase):
    kind = "GIS"

    def default_payload(self, columns: List[str], get_df: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        lon = "Lon" if "Lon" in columns else (columns[0] if columns else "")
        lat = "Lat" if "Lat" in columns else (columns[1] if len(columns) > 1 else "")
        return {
            "title": "GIS Map",
            "lon_col": lon,
            "lat_col": lat,
            "coord_input_format": "DD",
            "display_crs": "EPSG:4326",

            # heatmap
            "heatmap": True,
            "heat_radius_m": 200.0,
            "heat_resolution": 64,
            "heatmap_cmap": "jet",
            "heat_alpha": 0.55,

            # working aids
            "show_radii": True,
            "r1": 25,
            "r2": 50,
            "show_centroid": True,
            "centroid_pct": 100.0,

            # builtin/base points layer
            "base_name": "Buoy Points",
            "base_visible": True,
            "base_marker": "o",
            "base_size": 8.0,
            "base_color": "#1f77b4",
            "base_alpha": 0.7,

            # external layers
            "layers": _default_layers_payload(),

            # default vector style
            "vector_color": "#ffd54f",
            "vector_linewidth": 1.2,

            # frame / legend
            "map_width": 720,
            "map_height": 480,
            "square_map": True,
            "show_legend": True,

            # grid / axis
            "show_grid": True,
            "grid_units": "DD",

            # appearance
            "basemap_theme": "None",
            "show_coastlines": False,
            "coastline_color": "#666666",
        }

    def create_editor(self, spec: ChartSpec, columns: List[str],
                      parent: Optional[QWidget] = None) -> QDialog:
        return GISEditor(spec, columns, parent)

    def create_renderer(self, spec: ChartSpec, get_df: Callable[[], pd.DataFrame],
                        columns: List[str], parent: Optional[QWidget] = None,
                        get_df_full: Optional[Callable[[], pd.DataFrame]] = None) -> QWidget:
        return GISRenderer(spec, get_df, columns, parent)

    def upgrade_legacy_dict(self, flat: Dict[str, Any]) -> Dict[str, Any]:
        # Keep old keys; add new ones with sensible defaults
        return {
            "title": flat.get("title", "GIS Map"),
            "lon_col": flat.get("gis_lon_col", flat.get("x_col", "Lon")),
            "lat_col": flat.get("gis_lat_col", "Lat"),

            "coord_input_format": flat.get("coord_input_format", "DD"),
            "display_crs": flat.get("display_crs", "EPSG:4326"),

            "heatmap": bool(flat.get("gis_heatmap", flat.get("heatmap", True))),
            "heat_radius_m": float(flat.get("heat_radius_m", 200.0)),
            "heat_resolution": int(flat.get("heat_resolution", 64)),
            "heatmap_cmap": flat.get("heatmap_cmap", "jet"),
            "heat_alpha": float(flat.get("heat_alpha", 0.55)),

            "show_radii": bool(flat.get("gis_show_radii", True)),
            "r1": int(flat.get("gis_radius1_m", 25)),
            "r2": int(flat.get("gis_radius2_m", 50)),
            "show_centroid": bool(flat.get("gis_show_centroid", True)),
            "centroid_pct": float(flat.get("gis_centroid_pct", 100.0)),

            "base_name": flat.get("base_name", "Buoy Points"),
            "base_visible": bool(flat.get("base_visible", True)),
            "base_marker": flat.get("base_marker", "o"),
            "base_size": float(flat.get("base_size", 8.0)),
            "base_color": flat.get("base_color", "#1f77b4"),
            "base_alpha": float(flat.get("base_alpha", 0.7)),

            "layers": _default_layers_payload(),

            "vector_color": flat.get("vector_color", "#ffd54f"),
            "vector_linewidth": float(flat.get("vector_linewidth", 1.2)),

            "map_width": int(flat.get("map_width", 720)),
            "map_height": int(flat.get("map_height", 480)),
            "square_map": bool(flat.get("square_map", True)),
            "show_legend": bool(flat.get("show_legend", True)),

            "show_grid": bool(flat.get("show_grid", True)),
            "grid_units": flat.get("grid_units", "DD"),

            "basemap_theme": flat.get("basemap_theme", "None"),
            "show_coastlines": bool(flat.get("show_coastlines", False)),
            "coastline_color": flat.get("coastline_color", "#666666"),
        }

