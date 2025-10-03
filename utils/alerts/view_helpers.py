# utils/alerts/view_helpers.py
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple

import pandas as pd


def normalize_threshold(obj: Dict[str, Any]) -> Optional[float | str]:
    """
    Accepts an alert result .get('extra', {}) or a log row.
    Returns a best-effort threshold (float/str) or None.
    """
    for k in ("threshold", "threshold_value", "limit", "duration", "amber", "red", "green"):
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None

def normalize_observed(obj: Dict[str, Any]) -> Optional[float | str]:
    for k in ("observed", "value", "last_value", "current"):
        v = obj.get(k, None)
        if v not in (None, ""):
            return v
    return None

def last_lat_lon_from_df(df: Optional[pd.DataFrame]) -> Tuple[Optional[float], Optional[float]]:
    if df is None or df.empty:
        return None, None
    # common names
    cand_lat = ["last_lat", "Lat", "lat", "Latitude", "latitude"]
    cand_lon = ["last_lon", "Lon", "lon", "Longitude", "longitude", "lng"]
    lat_col = next((c for c in cand_lat if c in df.columns), None)
    lon_col = next((c for c in cand_lon if c in df.columns), None)
    if not lat_col or not lon_col:
        return None, None
    last = df[[lat_col, lon_col]].tail(1).copy()
    lat = pd.to_numeric(last[lat_col], errors="coerce").dropna()
    lon = pd.to_numeric(last[lon_col], errors="coerce").dropna()
    if lat.empty or lon.empty:
        return None, None
    return float(lat.iloc[0]), float(lon.iloc[0])


def enrich_extra_for_log(spec, res: Dict[str, Any], host) -> Dict[str, Any]:
    """
    Build a dict with guaranteed keys used by alerts_log and UI:
      threshold, observed, last_lat, last_lon
    It prefers values in res['extra'], then falls back to host.df when sensible.
    """
    extra = dict(res.get("extra", {}) or {})
    out: Dict[str, Any] = {}

    thr = normalize_threshold(extra)
    if thr is None:
        # Threshold alert: use payload numbers if not present in extra
        try:
            if spec.kind == "Threshold":
                mode = str(spec.payload.get("mode", "greater"))
                # prefer the primary bound that indicates entering alert
                if mode == "greater":
                    thr = spec.payload.get("amber") or spec.payload.get("red") or spec.payload.get("green")
                else:
                    thr = spec.payload.get("red") or spec.payload.get("amber") or spec.payload.get("green")
            elif spec.kind == "Stale":
                thr = spec.payload.get("amber_min") or spec.payload.get("threshold_min") or 30
            elif spec.kind == "Distance":
                thr = spec.payload.get("red_threshold_m") or spec.payload.get("red_m") or spec.payload.get("r2") or 500
        except Exception:
            pass
    out["threshold"] = thr

    obs = normalize_observed(res)
    if obs is None:
        # Threshold: observed = last numeric value of the chosen column
        try:
            if spec.kind == "Threshold":
                vcol = spec.payload.get("column", "")
                if vcol and hasattr(host, "df") and host.df is not None and vcol in host.df.columns:
                    v = pd.to_numeric(host.df[vcol], errors="coerce").dropna()
                    if not v.empty:
                        obs = float(v.iloc[-1])
        except Exception:
            pass
    out["observed"] = obs

    lat, lon = extra.get("last_lat"), extra.get("last_lon")
    if lat is None or lon is None:
        try:
            df = getattr(host, "df", None)
            lat, lon = last_lat_lon_from_df(df)
        except Exception:
            lat = lat if lat is not None else None
            lon = lon if lon is not None else None

    out["last_lat"] = lat
    out["last_lon"] = lon
    return out
