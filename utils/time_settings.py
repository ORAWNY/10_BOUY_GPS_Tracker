# utils/time_settings.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ---- global config ----
@dataclass
class TimeConfig:
    tz_name: str = "Europe/Dublin"  # Irish time with DST
    dayfirst: bool = True           # dd/mm/yyyy → True
    assume_naive_is_local: bool = True  # treat naive datetimes as Irish time

_cfg = TimeConfig()

def get_config() -> TimeConfig:
    return _cfg

def set_config(tz_name: Optional[str] = None,
               dayfirst: Optional[bool] = None,
               assume_naive_is_local: Optional[bool] = None):
    global _cfg
    if tz_name:  _cfg.tz_name = tz_name
    if dayfirst is not None: _cfg.dayfirst = bool(dayfirst)
    if assume_naive_is_local is not None: _cfg.assume_naive_is_local = bool(assume_naive_is_local)

def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(_cfg.tz_name)
    except Exception:
        return ZoneInfo("Europe/Dublin")

def now_local_naive() -> datetime:
    # UI code expects naive local timestamps for display/filtering
    return datetime.now(local_zone()).replace(tzinfo=None)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def offset_label() -> str:
    z = local_zone()
    off = datetime.now(z).utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = '+' if total >= 0 else '-'
    total = abs(total)
    hh, rem = divmod(total, 3600)
    mm = rem // 60
    return f"UTC{sign}{hh:02d}:{mm:02d}"


# ---- parsing helpers ----
# utils/time_settings.py
def parse_series_to_local_naive(s: pd.Series) -> pd.Series:
    tz = local_zone()
    try:
        dt = pd.to_datetime(
            s, errors="coerce", utc=False, dayfirst=_cfg.dayfirst, format="mixed"
        )
    except TypeError:
        # pandas < 2.0 doesn't support format="mixed"
        dt = pd.to_datetime(s, errors="coerce", utc=False, dayfirst=_cfg.dayfirst)

    # If dtype is tz-aware, convert whole series to local and drop tzinfo.
    try:
        if getattr(dt.dt, "tz", None) is not None:
            try:
                dt = dt.dt.tz_convert(tz).dt.tz_localize(None)
            except Exception:
                dt = dt.dt.tz_localize(None)
    except Exception:
        # If dt isn't datetime-like, just return it
        pass
    return dt

