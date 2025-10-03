# utils/charts/base.py
from __future__ import annotations
from typing import Callable, Dict, Optional, Any, Type
from dataclasses import dataclass, field

import pandas as pd
from PyQt6.QtWidgets import QWidget, QDialog

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
