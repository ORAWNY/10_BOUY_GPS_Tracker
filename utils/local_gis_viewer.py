import os
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.stats import gaussian_kde
from scipy.spatial import cKDTree

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT

import cartopy.crs as ccrs
import cartopy.feature as cfeature

from functools import lru_cache

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSizePolicy, QFrame

try:
    from pyproj import Geod
    _GEOD = Geod(ellps="WGS84")
except Exception:
    _GEOD = None

QP = getattr(QSizePolicy, "Policy", QSizePolicy)
QFShape = getattr(QFrame, "Shape", QFrame)


@dataclass
class Layer:
    name: str
    kind: str  # "points_df", "vector_gdf"
    visible: bool = True
    df: Optional[pd.DataFrame] = None
    gdf: Optional[gpd.GeoDataFrame] = None
    style: Optional[Dict[str, Any]] = None


class AspectFigureCanvas(FigureCanvas):
    def __init__(self, figure, parent=None):
        super().__init__(figure)
        self._aspect = 1.0  # height/width
        self._base_width_px = 720
        self.setSizePolicy(QP.Fixed, QP.Fixed)

    def setAspect(self, aspect: float):
        self._aspect = max(1e-6, float(aspect))
        self._apply_fixed_size()

    def setBaseWidth(self, w_px: int):
        self._base_width_px = max(200, int(w_px))
        self._apply_fixed_size()

    def _apply_fixed_size(self):
        w = int(self._base_width_px)
        h = int(round(w * self._aspect))
        self.setFixedSize(w, h)

    def hasHeightForWidth(self) -> bool:
        return False

    def sizeHint(self) -> QSize:
        w = int(self._base_width_px)
        return QSize(w, int(round(w * self._aspect)))


class LocalGISViewer(QWidget):
    wants_fixed_canvas = True
    natural_width_px = 720

    def __init__(self, df: pd.DataFrame, table_name: str,
                 radius1: int = 25, radius2: int = 50,
                 show_radii: bool = True,
                 show_centroid: bool = True,
                 centroid_fraction: float = 1.0,
                 heatmap_enabled: bool = True,
                 parent=None):
        super().__init__(parent)
        self.df = df.copy()
        self.table_name = table_name
        self.radius1 = int(radius1)
        self.radius2 = int(radius2)

        # status radii (m) — green optional
        self.radius_green = 0.0
        self.radius_amber = float(radius1 or 0.0)
        self.radius_red = float(radius2 or 0.0)

        # flags / params
        self.show_radii = bool(show_radii)
        self.show_centroid = bool(show_centroid)
        self.centroid_fraction = float(max(0.0, min(1.0, centroid_fraction)))
        self.kde_enabled = bool(heatmap_enabled)
        self.kde_alpha = 0.55
        self.kde_grid_max = 250
        self.kde_mask_buffer_m = 10.0

        # styles
        self.base_marker = "o"
        self.base_size = 8.0
        self.base_color = "#1f77b4"
        self.base_alpha = 0.7
        self.vector_color = "#ffd54f"
        self.vector_linewidth = 1.2

        # appearance
        self.base_name = "Buoy Points"
        self.base_visible = True
        self.show_legend = True
        self.show_grid = True
        self.grid_units = "DD"
        self.display_crs = "EPSG:4326"
        self.kde_cmap = "jet"
        self.basemap_theme = "None"
        self.show_coast = False
        self.coast_color = "#666666"

        # NEW: keep the frame square + equal X/Y ground scale
        self.force_square = True

        self.layers: List[Layer] = []

        # --- Figure & Canvas (tight margins to avoid white frame) ---
        self.fig = plt.figure(figsize=(7.2, 7.2), dpi=100)  # square base fig
        self.ax = self.fig.add_subplot(111, projection=ccrs.PlateCarree())
        self.fig.subplots_adjust(left=0.03, right=0.97, bottom=0.03, top=0.97)
        try: self.ax.margins(0)
        except Exception: pass

        self.canvas = AspectFigureCanvas(self.fig)
        self.canvas.setBaseWidth(self.natural_width_px)
        self.canvas.setAspect(1.0)

        self.toolbar = NavigationToolbar2QT(self.canvas, self)

        # --- Redraw debounce ---
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.timeout.connect(self._redraw_now)

        # Performance / cache
        self.max_points_drawn = 5000
        self.kde_cache_enabled = True
        self._kde_cache_key = None
        self._kde_cache_Z = None
        self._kde_cache_extent = None
        self._kde_img_artist = None

        # --- Layout ---
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolrow = QHBoxLayout()
        toolrow.addWidget(self.toolbar); toolrow.addStretch(1)
        self.btn_measure = QPushButton("Measure"); self.btn_measure.setCheckable(True)
        self.btn_clear_measure = QPushButton("Clear")
        self.lbl_status = QLabel("Dist: 0 m   |   Cursor: —")
        toolrow.addWidget(self.btn_measure); toolrow.addWidget(self.btn_clear_measure)
        toolrow.addSpacing(8); toolrow.addWidget(self.lbl_status)
        layout.addLayout(toolrow)
        layout.addWidget(self.canvas, alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        # Measure state
        self.measure_active = False
        self.measure_pts = []
        self._measure_line = None
        self._measure_scatter = None
        self._measure_ghost = None
        self._cid_click = self.canvas.mpl_connect('button_press_event', self._on_mouse_click)
        self._cid_move  = self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self._cid_key   = self.canvas.mpl_connect('key_press_event', self._on_key_press)
        self.btn_measure.toggled.connect(self._toggle_measure)
        self.btn_clear_measure.clicked.connect(self._clear_measure)

        # Keep aspect after user zoom/pan
        self._limit_guard = False
        self.ax.callbacks.connect('xlim_changed', self._on_limits_changed)
        self.ax.callbacks.connect('ylim_changed', self._on_limits_changed)

        # initial draw
        self._rebuild_default_layers()
        self._schedule_redraw()

    def set_status_radii(self, green_m: float | None, amber_m: float, red_m: float):
        """Set GREEN / AMBER / RED radii (meters). green_m is optional (0/None = no green ring)."""
        self.radius_green = float(green_m or 0.0)
        self.radius_amber = float(amber_m or 0.0)
        self.radius_red = float(red_m or 0.0)
        # keep legacy fields roughly in sync
        self.radius1 = int(round(self.radius_amber))
        self.radius2 = int(round(self.radius_red))
        self._schedule_redraw()

    # ---- QWidget sizing ----
    def sizeHint(self) -> QSize:
        return self.canvas.sizeHint()

    # -------- Public style/config API --------
    def set_force_square(self, on: bool):
        self.force_square = bool(on)
        if self.force_square:
            self.canvas.setAspect(1.0)
        self._schedule_redraw(0)

    def set_canvas_size(self, w: int, h: int):
        w = max(200, int(w)); h = max(200, int(h))
        try:
            if self.force_square:
                side = min(w, h)
                self.canvas.setBaseWidth(side)
                self.canvas.setAspect(1.0)
            else:
                self.canvas.setBaseWidth(w)
                self.canvas.setAspect(float(h) / float(w))
        except Exception:
            self.setFixedSize(w, h)
        self._schedule_redraw(0)

    def set_base_point_style(self, marker="o", size=8.0, color="#1f77b4", alpha=0.7):
        self.base_marker = marker or "o"
        self.base_size = float(size or 8.0)
        self.base_color = color or "#1f77b4"
        self.base_alpha = float(alpha or 0.7)
        self._schedule_redraw()

    def set_vector_style(self, color="#ffd54f", linewidth=1.2):
        self.vector_color = color or "#ffd54f"
        self.vector_linewidth = float(linewidth or 1.2)
        self._schedule_redraw()

    def set_external_layers(self, layers: List[Layer]):
        self.layers = list(layers or [])
        self._schedule_redraw()

    def set_base_name(self, name: str):
        self.base_name = str(name or "Buoy Points"); self._schedule_redraw()

    def set_base_visible(self, on: bool):
        self.base_visible = bool(on); self._schedule_redraw()

    def set_show_legend(self, show: bool):
        self.show_legend = bool(show); self._schedule_redraw()

    def set_grid(self, show: bool, units: str = "DD"):
        self.show_grid = bool(show)
        self.grid_units = str(units or "DD").upper()
        self._schedule_redraw()

    def set_display_crs(self, crs: str):
        self.display_crs = str(crs or "EPSG:4326")
        self._schedule_redraw()

    def set_heatmap_params(self, radius_m: float, resolution: int):
        self.kde_mask_buffer_m = max(0.0, float(radius_m or 0.0))
        self.kde_grid_max = int(max(16, min(2048, resolution or 64)))
        self._schedule_redraw()

    def set_heatmap_cmap(self, name: str):
        self.kde_cmap = str(name or "jet"); self._schedule_redraw()

    def set_heatmap_alpha(self, a: float):
        self.kde_alpha = float(max(0.0, min(1.0, a))); self._schedule_redraw()

    def set_basemap_theme(self, theme: str):
        self.basemap_theme = str(theme or "None"); self._schedule_redraw()

    def set_coastlines(self, show: bool, color: str = "#666666"):
        self.show_coast = bool(show)
        self.coast_color = str(color or "#666666")
        self._schedule_redraw()

    # -------- Public data/toggles --------
    def set_data(self, df: pd.DataFrame):
        self._clear_measure(redraw=False)
        self.df = df.copy()
        self._rebuild_default_layers()
        self._schedule_redraw()

    def set_radii(self, r1: int, r2: int):
        # legacy: r1 = Amber, r2 = Red
        self._clear_measure(redraw=False)
        self.set_status_radii(self.radius_green, r1, r2)  # this already syncs radius1/2 and schedules redraw

    def set_heatmap(self, enabled: bool):
        self.kde_enabled = bool(enabled)
        self._schedule_redraw()

    def set_centroid_fraction(self, frac: float):
        self.centroid_fraction = float(max(0.0, min(1.0, frac)))
        self._schedule_redraw()

    def set_show_radii(self, on: bool):
        self.show_radii = bool(on)
        self._schedule_redraw()

    def set_show_centroid(self, on: bool):
        self.show_centroid = bool(on)
        self._schedule_redraw()

    # convenience aliases
    def refit(self): self._schedule_redraw(0)
    def fit_to_view(self): self._schedule_redraw(0)
    def fit_to_data(self): self._schedule_redraw(0)
    def refresh(self): self._schedule_redraw(0)

    # -------- Internal: layers + drawing --------
    def _rebuild_default_layers(self):
        base_df = self.df.copy()
        has_lat = "Lat" in base_df.columns
        has_lon = "Lon" in base_df.columns
        if has_lat: base_df["Lat"] = pd.to_numeric(base_df["Lat"], errors="coerce")
        if has_lon: base_df["Lon"] = pd.to_numeric(base_df["Lon"], errors="coerce")

        if not (has_lat and has_lon):
            base_df = base_df.iloc[0:0]
        else:
            base_df = base_df.dropna(subset=["Lat", "Lon"])
            base_df = base_df[~base_df["Lat"].isin([-9999, 9999])]
            base_df = base_df[~base_df["Lon"].isin([-9999, 9999])]

        self._df_clean = base_df

    @lru_cache(maxsize=512)
    def _geodesic_circle_lonlat(self, lon, lat, radius_m, n=240):
        lon = float(lon); lat = float(lat); radius_m = float(radius_m); n = int(n)
        if radius_m <= 0: return np.array([]), np.array([])
        if _GEOD is not None:
            az = np.linspace(0, 360, n, endpoint=True)
            lons, lats, _ = _GEOD.fwd(np.full_like(az, lon), np.full_like(az, lat), az, np.full_like(az, radius_m))
            return np.asarray(lons), np.asarray(lats)
        lat_deg_per_m = 1.0 / 111111.0
        lon_deg_per_m = 1.0 / (111111.0 * np.cos(np.radians(lat)))
        theta = np.linspace(0, 2*np.pi, n, endpoint=True)
        xs = lon + (radius_m * np.cos(theta)) * lon_deg_per_m
        ys = lat + (radius_m * np.sin(theta)) * lat_deg_per_m
        return xs, ys

    def _compute_reference_points(self):
        df = getattr(self, "_df_clean", None)
        if df is None or df.empty or "Lat" not in df.columns or "Lon" not in df.columns:
            return None

        rt_aligned = None
        if "received_time" in getattr(self, "df", pd.DataFrame()).columns:
            try:
                rt_raw = pd.to_datetime(self.df["received_time"], errors="coerce")
                rt_aligned = rt_raw.reindex(df.index)
            except Exception:
                rt_aligned = None

        if rt_aligned is not None and rt_aligned.notna().any():
            order = rt_aligned.sort_values(kind="mergesort").index
        else:
            order = df.index

        order = [i for i in order if i in df.index]
        if not order:
            return None

        n = max(1, int(len(order) * float(self.centroid_fraction or 1.0)))
        window_idx = order[-n:]
        window = df.loc[window_idx]

        last_idx = order[-1]
        first_five = df.loc[order[:5]]
        dep_lat = float(first_five["Lat"].mean())
        dep_lon = float(first_five["Lon"].mean())
        cen_lat = float(window["Lat"].mean())
        cen_lon = float(window["Lon"].mean())
        last_lat = float(df.loc[last_idx, "Lat"])
        last_lon = float(df.loc[last_idx, "Lon"])
        return dict(dep_lat=dep_lat, dep_lon=dep_lon, cen_lat=cen_lat, cen_lon=cen_lon,
                    last_lat=last_lat, last_lon=last_lon)

    def _schedule_redraw(self, delay_ms: int = 16):
        self._redraw_timer.start(max(0, int(delay_ms)))

    def _redraw_now(self):
        self._redraw()

    # keep canvas aspect under control (square or rectangular)
    def _apply_aspect_from_extent(self, minx, maxx, miny, maxy):
        if self.force_square:
            self.canvas.setAspect(1.0)
        else:
            lon_span = float(maxx - minx) if np.isfinite(maxx - minx) else 1.0
            lat_span = float(maxy - miny) if np.isfinite(maxy - miny) else 1.0
            lat0 = 0.5 * (float(miny) + float(maxy))
            coslat = float(np.cos(np.radians(lat0))) or 1e-6
            map_w_m = max(lon_span * coslat, 1e-9)
            map_h_m = max(lat_span, 1e-9)
            self.canvas.setAspect(map_h_m / map_w_m)

    def _apply_basemap(self):
        theme = (self.basemap_theme or "None").lower()
        self.ax.set_facecolor("white")
        if theme == "none":
            if self.show_coast:
                try: self.ax.coastlines(resolution="50m", color=self.coast_color, linewidth=0.6, zorder=2)
                except Exception: pass
            return
        if theme == "light":   land, ocean = "#f5f5f2", "#e8f4ff"
        elif theme == "dark":  land, ocean = "#222222", "#0e1420"; self.ax.set_facecolor("#0b0b0b")
        elif theme == "blue":  land, ocean = "#e6f2ff", "#bcd7ff"
        elif theme == "gray":  land, ocean = "#dcdcdc", "#e5e5e5"
        else:                  land, ocean = "#f5f5f2", "#e8f4ff"
        try:
            self.ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=ocean, edgecolor="none", zorder=0)
            self.ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=land, edgecolor="none", zorder=1)
        except Exception:
            self.ax.set_facecolor(ocean)
        if self.show_coast:
            try: self.ax.coastlines(resolution="50m", color=self.coast_color, linewidth=0.6, zorder=2)
            except Exception: pass

    def _normalize_extent_equal_xy(self, x0, x1, y0, y1):
        """Return an extent centered at the same point, expanded so that X/Y
        show equal ground distance per pixel. If force_square, it is also square."""
        cx = 0.5 * (x0 + x1)
        cy = 0.5 * (y0 + y1)
        half_w = 0.5 * abs(x1 - x0)
        half_h = 0.5 * abs(y1 - y0)
        lat0 = float(cy)
        coslat = float(np.cos(np.radians(lat0))) or 1e-6

        # meters represented by current half-extent
        half_w_m = half_w * 111_111.0 * coslat
        half_h_m = half_h * 111_111.0

        if self.force_square:
            m = max(half_w_m, half_h_m)
            half_w = m / (111_111.0 * coslat)
            half_h = m / 111_111.0
        else:
            # only ensure equal scale; keep canvas aspect free
            if half_w_m > half_h_m:
                half_h = half_w_m / 111_111.0
            else:
                half_w = half_h_m / (111_111.0 * coslat)

        return cx - half_w, cx + half_w, cy - half_h, cy + half_h

    def _redraw(self):
        self.ax.clear()
        self._kde_img_artist = None

        try:
            self.fig.subplots_adjust(left=0.03, right=0.97, bottom=0.03, top=0.97)
            self.ax.margins(0)
        except Exception:
            pass

        self._apply_basemap()

        if self.show_grid:
            try:
                self.ax.gridlines(draw_labels=False, linewidth=0.25, color='gray', alpha=0.5, linestyle='--')
            except Exception:
                pass

        if self.kde_enabled:
            self._draw_kde_heatmap()

        # external layers
        for ly in self.layers:
            if not ly.visible: continue
            if ly.kind == "points_df" and ly.df is not None and not ly.df.empty:
                st = ly.style or {}
                self.ax.scatter(
                    ly.df["Lon"], ly.df["Lat"],
                    s=float(st.get("size", self.base_size)),
                    alpha=float(st.get("alpha", self.base_alpha)),
                    marker=st.get("marker", self.base_marker),
                    color=st.get("color", self.base_color),
                    edgecolors='none', transform=ccrs.PlateCarree(), zorder=5, label=ly.name
                )
            elif ly.kind == "vector_gdf" and ly.gdf is not None and not ly.gdf.empty:
                try:
                    st = ly.style or {}
                    ly.gdf.plot(ax=self.ax,
                                edgecolor=st.get("linecolor", self.vector_color),
                                facecolor="none",
                                linewidth=float(st.get("linewidth", self.vector_linewidth)),
                                transform=ccrs.PlateCarree(), zorder=4, label=ly.name)
                except Exception:
                    pass

        refs = self._compute_reference_points()
        df_pts = getattr(self, "_df_clean", pd.DataFrame())

        # builtin points
        if self.base_visible and df_pts is not None and not df_pts.empty:
            pts = df_pts
            if len(pts) > self.max_points_drawn:
                pts = pts.iloc[::max(1, len(pts)//self.max_points_drawn)]
            self.ax.scatter(pts["Lon"], pts["Lat"], s=self.base_size, alpha=self.base_alpha,
                            marker=self.base_marker, color=self.base_color, edgecolors='none',
                            transform=ccrs.PlateCarree(), zorder=5, label=self.base_name)

        # refs + radii
        if refs is not None:
            dep_lon = refs["dep_lon"]; dep_lat = refs["dep_lat"]
            cen_lon = refs["cen_lon"]; cen_lat = refs["cen_lat"]
            last_lon = refs["last_lon"]; last_lat = refs["last_lat"]

            self.ax.scatter([last_lon], [last_lat], color="gold", edgecolor="black", marker="*",
                            s=120, transform=ccrs.PlateCarree(), zorder=7, label="Last")
            if self.show_centroid:
                self.ax.scatter([cen_lon], [cen_lat], color="cyan", marker="o", s=50,
                                transform=ccrs.PlateCarree(), zorder=7, label="Centroid")
            self.ax.scatter([dep_lon], [dep_lat], color="green", marker="X", s=60,
                            transform=ccrs.PlateCarree(), zorder=7, label="Deployed")
            self.ax.plot([dep_lon, last_lon], [dep_lat, last_lat], color="gold",
                         linewidth=2, transform=ccrs.PlateCarree(), zorder=6)
            if self.show_radii:
                # colors match your alert palette
                rings = [
                    ("Green", self.radius_green, "#37b24d"),
                    ("Amber", self.radius_amber, "#f59f00"),
                    ("Red", self.radius_red, "#f03e3e"),
                ]
                for label, r, color in rings:
                    r = float(r or 0.0)
                    if r <= 0:
                        continue
                    xs, ys = self._geodesic_circle_lonlat(dep_lon, dep_lat, r, n=240)
                    if xs.size:
                        # include the meter value in the legend label
                        self.ax.plot(xs, ys, color=color, linewidth=1.8,
                                     transform=ccrs.PlateCarree(), zorder=6,
                                     label=f"{label} radius: {r:g} m")

        # autoscale + normalize + square
        self._autoscale_to_visible()

        if self.show_legend:
            try:
                handles, labels = self.ax.get_legend_handles_labels()
                seen = set();
                uniq_h, uniq_l = [], []
                for h, l in zip(handles, labels):
                    if l in seen:
                        continue
                    seen.add(l);
                    uniq_h.append(h);
                    uniq_l.append(l)
                if uniq_h:
                    self.ax.legend(uniq_h, uniq_l, loc="upper right", fontsize=8, frameon=True)
            except Exception:
                pass

        self.canvas.draw_idle()

    def _draw_kde_heatmap(self):
        df = getattr(self, "_df_clean", None)
        if df is None or df.empty or "Lat" not in df.columns or "Lon" not in df.columns or len(df) < 2:
            self._kde_cache_Z = self._kde_cache_extent = None
            return

        lon = df["Lon"].to_numpy(dtype=np.float32)
        lat = df["Lat"].to_numpy(dtype=np.float32)
        if np.allclose(lon, lon[0]) and np.allclose(lat, lat[0]):
            self._kde_cache_Z = self._kde_cache_extent = None
            return

        lat0 = float(np.nanmean(lat))
        coslat = float(np.cos(np.radians(lat0))) or 1e-6

        if len(df) >= 5:
            dep_lat = float(df.head(5)["Lat"].mean())
            dep_lon = float(df.head(5)["Lon"].mean())
        else:
            dep_lat = float(np.nanmean(lat))
            dep_lon = float(np.nanmean(lon))

        # project to local meters
        x = (lon - dep_lon) * 111_111.0 * coslat
        y = (lat - dep_lat) * 111_111.0

        key = None
        if self.kde_cache_enabled:
            key = (len(x), float(np.nanmean(x)), float(np.nanstd(x)),
                   float(np.nanmean(y)), float(np.nanstd(y)),
                   int(self.kde_grid_max), float(self.kde_alpha), float(self.centroid_fraction),
                   str(self.kde_cmap), float(self.kde_mask_buffer_m))

        nx = min(self.kde_grid_max, max(50, int(np.sqrt(len(x)) * 12)))
        ny = min(self.kde_grid_max, max(50, int(np.sqrt(len(y)) * 12)))
        recompute = (not self.kde_cache_enabled) or (key != self._kde_cache_key) or (self._kde_cache_Z is None)

        if recompute:
            try:
                kde = gaussian_kde(np.vstack([x, y]))
            except Exception:
                self._kde_cache_Z = self._kde_cache_extent = None
                return

            pad_m = 5.0
            x_min, x_max = float(np.nanmin(x)) - pad_m, float(np.nanmax(x)) + pad_m
            y_min, y_max = float(np.nanmin(y)) - pad_m, float(np.nanmax(y)) + pad_m
            if abs(x_max - x_min) < 1e-6: x_min -= 1.0; x_max += 1.0
            if abs(y_max - y_min) < 1e-6: y_min -= 1.0; y_max += 1.0

            gx = np.linspace(x_min, x_max, nx, dtype=np.float32)
            gy = np.linspace(y_min, y_max, ny, dtype=np.float32)
            XX, YY = np.meshgrid(gx, gy)
            try:
                Z = kde(np.vstack([XX.ravel(), YY.ravel()])).reshape(XX.shape)
            except Exception:
                self._kde_cache_Z = self._kde_cache_extent = None
                return

            try:
                tree = cKDTree(np.column_stack([x, y]))
                dists, _ = tree.query(np.column_stack([XX.ravel(), YY.ravel()]))
                D = dists.reshape(XX.shape)
                Z = np.ma.array(Z, mask=(D > float(self.kde_mask_buffer_m)))
            except Exception:
                pass

            zmax = Z.max() if (np.ma.isMaskedArray(Z) and Z.count() > 0) or (isinstance(Z, np.ndarray) and Z.size > 0) else 0.0
            if zmax and np.isfinite(zmax) and zmax > 0:
                Z = Z / zmax

            lon_g = dep_lon + (gx / (111_111.0 * coslat))
            lat_g = dep_lat + (gy / 111_111.0)
            extent = (lon_g.min(), lon_g.max(), lat_g.min(), lat_g.max())

            self._kde_cache_Z = Z
            self._kde_cache_extent = extent
            self._kde_cache_key = key

        if self._kde_cache_Z is not None and self._kde_cache_extent is not None:
            self._kde_img_artist = self.ax.imshow(
                self._kde_cache_Z,
                extent=self._kde_cache_extent,
                origin="lower",
                transform=ccrs.PlateCarree(),
                cmap=self.kde_cmap,
                alpha=self.kde_alpha,
                zorder=3,
                interpolation="bilinear",
                aspect="auto"
            )

    def _autoscale_to_visible(self):
        xs, ys = [], []

        # external layers
        for ly in self.layers:
            if not ly.visible: continue
            if ly.kind == "points_df" and ly.df is not None and not ly.df.empty:
                xs.extend(ly.df["Lon"].tolist()); ys.extend(ly.df["Lat"].tolist())
            elif ly.kind == "vector_gdf" and ly.gdf is not None and not ly.gdf.empty:
                try:
                    b = ly.gdf.total_bounds
                    xs.extend([b[0], b[2]]); ys.extend([b[1], b[3]])
                except Exception: pass

        # builtin points
        df_pts = getattr(self, "_df_clean", None)
        if df_pts is not None and not df_pts.empty:
            xs.extend(df_pts["Lon"].tolist()); ys.extend(df_pts["Lat"].tolist())

        # heatmap extent
        if self._kde_img_artist is not None and self.kde_enabled:
            try:
                e = self._kde_img_artist.get_extent()
                xs.extend([e[0], e[1]]); ys.extend([e[2], e[3]])
            except Exception: pass

        # reference points
        refs = self._compute_reference_points()
        if refs:
            xs.extend([refs["dep_lon"], refs["cen_lon"], refs["last_lon"]])
            ys.extend([refs["dep_lat"], refs["cen_lat"], refs["last_lat"]])

        if xs and ys:
            minx, maxx = float(np.nanmin(xs)), float(np.nanmax(xs))
            miny, maxy = float(np.nanmin(ys)), float(np.nanmax(ys))

            # pad a touch
            pad_x = max(0.0005, (maxx - minx) * 0.05)
            pad_y = max(0.0005, (maxy - miny) * 0.05)
            x0, x1 = minx - pad_x, maxx + pad_x
            y0, y1 = miny - pad_y, maxy + pad_y

            # ensure equal ground scale (+ square frame if enabled)
            x0, x1, y0, y1 = self._normalize_extent_equal_xy(x0, x1, y0, y1)

            self._limit_guard = True
            try:
                self.ax.set_extent([x0, x1, y0, y1], crs=ccrs.PlateCarree())
            finally:
                self._limit_guard = False

            self._apply_aspect_from_extent(x0, x1, y0, y1)
        else:
            x0, x1, y0, y1 = -10, 10, -10, 10
            x0, x1, y0, y1 = self._normalize_extent_equal_xy(x0, x1, y0, y1)
            self.ax.set_extent([x0, x1, y0, y1], crs=ccrs.PlateCarree())
            self._apply_aspect_from_extent(x0, x1, y0, y1)

    # --- keep equal scale after user zoom/pan ---
    def _on_limits_changed(self, _ax):
        if self._limit_guard:
            return
        self._limit_guard = True
        try:
            x0, x1 = self.ax.get_xlim()
            y0, y1 = self.ax.get_ylim()
            x0, x1, y0, y1 = self._normalize_extent_equal_xy(x0, x1, y0, y1)
            self.ax.set_xlim(x0, x1)
            self.ax.set_ylim(y0, y1)
            self._apply_aspect_from_extent(x0, x1, y0, y1)
        finally:
            self._limit_guard = False
        self.canvas.draw_idle()

    # -------- Measuring tool internals (unchanged) --------
    def _toggle_measure(self, checked: bool):
        self.measure_active = checked
        if not checked and self._measure_ghost:
            try: self._measure_ghost.remove()
            except Exception: pass
            self._measure_ghost = None
            self.canvas.draw_idle()

    def _clear_measure(self, redraw=True):
        self.measure_pts = []
        for artist in (self._measure_line, self._measure_scatter, self._measure_ghost):
            try:
                if artist: artist.remove()
            except Exception:
                pass
        self._measure_line = self._measure_scatter = self._measure_ghost = None
        self.lbl_status.setText("Dist: 0 m   |   Cursor: —")
        if redraw:
            self.canvas.draw_idle()

    def _on_key_press(self, event):
        if event.key == 'escape':
            self._clear_measure()

    def _on_mouse_click(self, event):
        if not self.measure_active or event.inaxes != self.ax:
            return
        if event.button in (3, 2):
            self.btn_measure.setChecked(False); return
        if event.button != 1: return
        lon, lat = event.xdata, event.ydata
        if lon is None or lat is None: return
        self.measure_pts.append((lon, lat))
        if len(self.measure_pts) >= 2:
            xs = [p[0] for p in self.measure_pts]; ys = [p[1] for p in self.measure_pts]
            if self._measure_line is None:
                self._measure_line, = self.ax.plot(xs, ys, linestyle='-', linewidth=1.8, color='orange',
                                                   transform=ccrs.PlateCarree(), zorder=10)
            else:
                self._measure_line.set_data(xs, ys)
        self.canvas.draw_idle()

    def _on_mouse_move(self, event):
        if event.inaxes == self.ax and event.xdata is not None and event.ydata is not None:
            total = self._measure_total_m()
            dist_txt = f"Dist: {total:,.1f} m" if total > 0 else "Dist: 0 m"
            self.lbl_status.setText(f"{dist_txt}   |   Cursor: {event.ydata:.5f}°, {event.xdata:.5f}°")
        else:
            txt = self.lbl_status.text(); parts = txt.split("|")
            if parts: self.lbl_status.setText(parts[0].strip() + "   |   Cursor: —")

        if not self.measure_active or event.inaxes != self.ax or not self.measure_pts:
            return
        last_lon, last_lat = self.measure_pts[-1]
        cur_lon, cur_lat = event.xdata, event.ydata
        if cur_lon is None or cur_lat is None: return
        if self._measure_ghost:
            self._measure_ghost.set_data([last_lon, cur_lon], [last_lat, cur_lat])
        else:
            (self._measure_ghost,) = self.ax.plot([last_lon, cur_lon], [last_lat, cur_lat],
                                                  linestyle='--', linewidth=1.2, color='orange',
                                                  transform=ccrs.PlateCarree(), zorder=10)
        self.canvas.draw_idle()

    def _measure_total_m(self) -> float:
        if len(self.measure_pts) < 2: return 0.0
        total = 0.0
        for (lon1, lat1), (lon2, lat2) in zip(self.measure_pts[:-1], self.measure_pts[1:]):
            total += self._geodetic_distance_m(lat1, lon1, lat2, lon2)
        return total

    def _geodetic_distance_m(self, lat1, lon1, lat2, lon2) -> float:
        if _GEOD is not None:
            _, _, dist = _GEOD.inv(lon1, lat1, lon2, lat2); return float(dist)
        R = 6371000.0
        phi1 = np.radians(lat1); phi2 = np.radians(lat2)
        dphi = np.radians(lat2 - lat1); dlmb = np.radians(lon2 - lon1)
        a = np.sin(dphi/2)**2 + np.cos(phi1)*np.cos(phi2)*np.sin(dlmb/2)**2
        return float(2 * R * np.arctan2(np.sqrt(a), np.sqrt(1-a)))
