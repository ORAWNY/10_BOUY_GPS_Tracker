# utils/charts/__init__.py
from .base import ChartSpec, TypeHandlerBase, REGISTRY, register

# Force import of handlers so their @register runs and populates REGISTRY
from . import xy_chart   # noqa: F401
from . import pie_chart  # noqa: F401
from . import gauge_chart  # noqa: F401
from . import gis_chart  # noqa: F401
from . import traffic_light  # noqa: F401
from . import windrose_chart

__all__ = [
    "ChartSpec",
    "TypeHandlerBase",
    "REGISTRY",
    "register",
]
