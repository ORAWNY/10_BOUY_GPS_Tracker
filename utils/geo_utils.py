"""
utils/geo_utils.py
==================
Geospatial helper functions — no GUI dependencies.

Previously haversine_m() and the lat/lon cleaning helpers were copy-pasted
into alerts.py, distance_alert.py, and several chart files.  They now live
here as the single authoritative implementation.
"""
from __future__ import annotations

import math

import pandas as pd

from utils.constants import SENTINEL_VALUES, LAT_MIN, LAT_MAX, LON_MIN, LON_MAX


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS-84 points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def clean_lat_series(s: pd.Series) -> pd.Series:
    """Coerce to numeric, mask sentinel values, keep only valid latitudes."""
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINEL_VALUES))
    return s.where((s >= LAT_MIN) & (s <= LAT_MAX))


def clean_lon_series(s: pd.Series) -> pd.Series:
    """Coerce to numeric, mask sentinel values, keep only valid longitudes."""
    s = pd.to_numeric(s, errors="coerce")
    s = s.mask(s.isin(SENTINEL_VALUES))
    return s.where((s >= LON_MIN) & (s <= LON_MAX))
