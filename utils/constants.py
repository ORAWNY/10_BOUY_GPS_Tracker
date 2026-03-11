"""
utils/constants.py
==================
Single source of truth for project-wide constants.

Import these wherever you need them rather than redefining locally.
"""
import re as _re

# ---------------------------------------------------------------------------
# Battery column detection
# ---------------------------------------------------------------------------
# Matches any column name that relates to battery or voltage monitoring.
# Patterns covered:
#   batt*  → batt, BATT, Battery, BattA, Batt1, v_batt, …
#   bat\d  → Bat1, Bat2, Bat3, bat1, BAT1, …
#   bat_   → bat_a, bat_voltage, …
#   volt*  → Volt, voltage, VOLTAGE, …
_BATTERY_COL_RE = _re.compile(r'batt|bat\d|bat[_]|volt', _re.IGNORECASE)


def is_battery_column(col_name: str) -> bool:
    """Return True if *col_name* looks like a battery or voltage column."""
    return bool(_BATTERY_COL_RE.search(str(col_name)))

# ---------------------------------------------------------------------------
# Sentinel / bad-data values
# ---------------------------------------------------------------------------
# Instrument fill values that indicate missing or invalid readings.
SENTINEL_VALUES = {0, 0.0, 9999, 9999.0, -9999, -9999.0}

# ---------------------------------------------------------------------------
# Outlook / email
# ---------------------------------------------------------------------------
OUTLOOK_ACCOUNT_DISPLAY_NAME = "Metocean Configuration"

# ---------------------------------------------------------------------------
# Coordinate validity bounds
# ---------------------------------------------------------------------------
LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
