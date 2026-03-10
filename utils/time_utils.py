"""
utils/time_utils.py
====================
Project-wide time/duration formatting helpers — no GUI dependencies.
"""
from __future__ import annotations


def fmt_duration(secs: float) -> str:
    """Format a duration in seconds as a human-readable string.

    Examples:
        90061  → '1d 01h 01m 01s'
        3661   → '1h 01m 01s'
        90     → '1m 30s'
    """
    try:
        secs = int(max(0, float(secs)))
    except Exception:
        return str(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"
