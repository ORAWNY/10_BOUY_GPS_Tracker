# utils/charts/base.py
from __future__ import annotations
from typing import Callable, Dict, Optional, Any, Type
from dataclasses import dataclass, field

import pandas as pd
from PyQt6.QtWidgets import QWidget, QDialog


def is_app_dark_mode() -> bool:
    """True when the user has chosen the dark theme in BuoyTools settings."""
    try:
        from PyQt6.QtCore import QSettings
        s = QSettings("BuoyTools", "DBViewer")
        return str(s.value("ui/theme", "light")).strip().lower() == "dark"
    except Exception:
        return False


def apply_figure_theme(figure, ax=None, dark: bool | None = None) -> None:
    """
    Apply appropriate background colours to a matplotlib Figure and optional Axes.
    If *dark* is None the current app theme is detected automatically.
    """
    if dark is None:
        dark = is_app_dark_mode()
    bg = "#111827" if dark else "#ffffff"
    ax_bg = "#1e293b" if dark else "#ffffff"
    figure.set_facecolor(bg)
    figure.patch.set_facecolor(bg)
    if ax is not None:
        ax.set_facecolor(ax_bg)


def alert_chart_colors(dark: bool | None = None) -> dict:
    """
    Return a dict of colours for alert viewer charts (threshold/stale/distance).
    Callers use: ``c = alert_chart_colors(); ax.set_facecolor(c["ax_bg"])`` etc.
    """
    if dark is None:
        dark = is_app_dark_mode()
    if dark:
        return {
            "fig_bg":      "#111827",
            "ax_bg":       "#1e293b",
            "spine":       "#374151",
            "grid":        "#374151",
            "tick":        "#9ca3af",
            "label":       "#d1d5db",
            "title":       "#f9fafb",
            "legend_bg":   "#1e293b",
            "legend_edge": "#374151",
            "band_green":  "#14532d",
            "band_amber":  "#713f12",
            "band_red":    "#7f1d1d",
        }
    return {
        "fig_bg":      "#ffffff",
        "ax_bg":       "#ffffff",
        "spine":       "#d1d5db",
        "grid":        "#e5e7eb",
        "tick":        "#6b7280",
        "label":       "#374151",
        "title":       "#111827",
        "legend_bg":   "#ffffff",
        "legend_edge": "#e5e7eb",
        "band_green":  "#dcfce7",
        "band_amber":  "#fef9c3",
        "band_red":    "#fee2e2",
    }

# ---------------- ChartSpec ----------------
@dataclass
class ChartSpec:
    id: str
    chart_kind: str
    title: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "chart_kind": self.chart_kind,
            "title": self.title,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChartSpec":
        return cls(
            id=d.get("id", ""),
            chart_kind=d.get("chart_kind", ""),
            title=d.get("title", ""),
            payload=d.get("payload", {}) or {},
        )


# ---------------- TypeHandlerBase ----------------
class TypeHandlerBase:
    """
    Base class every chart type handler should subclass.
    Subclasses MUST implement:
      - create_renderer()
      - create_editor()
    """
    kind: str = "base"

    def create_renderer(
        self,
        spec: ChartSpec,
        get_df: Callable[[], pd.DataFrame],
        columns: list[str],
        parent: Optional[QWidget],
        get_df_full: Optional[Callable[[], pd.DataFrame]] = None,
    ) -> QWidget:
        raise NotImplementedError

    def create_editor(
        self,
        spec: ChartSpec,
        columns: list[str],
        parent: Optional[QWidget] = None,
    ) -> QDialog:
        raise NotImplementedError

    # optional: supply sensible defaults
    def default_payload(
        self,
        columns: list[str],
        get_df: Callable[[], pd.DataFrame],
    ) -> Dict[str, Any]:
        return {}


# ---------------- Registry ----------------
REGISTRY: Dict[str, TypeHandlerBase] = {}

def register(handler_cls: Type[TypeHandlerBase]) -> Type[TypeHandlerBase]:
    """
    Class decorator to register a chart handler.
    Usage:
        @register
        class XYHandler(TypeHandlerBase):
            kind = "XY"
            ...
    """
    kind = getattr(handler_cls, "kind", None)
    if not kind:
        raise ValueError(f"Chart handler {handler_cls.__name__} missing .kind")
    REGISTRY[kind] = handler_cls()
    return handler_cls
