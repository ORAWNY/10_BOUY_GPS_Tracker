"""
utils/constants.py
==================
Single source of truth for project-wide constants.

Import these wherever you need them rather than redefining locally.
"""

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
