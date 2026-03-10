"""
utils/alerts/evaluator.py
=========================
Helpers for loading and saving alert specs from the per-project state DB.

AlertsTab (when instantiated) remains the evaluation + email engine.
These helpers are used by:
  - TableAlertsDialog (alerts_panel.py) for CRUD
  - SummaryPage for reading stored status to paint the Alerts column
  - The _API provider in Create_tabs.py
"""
from __future__ import annotations

import json
from typing import List

from utils.alerts import AlertSpec
from utils.alerts.store import (
    ensure_alerts_tables,
    read_current_settings,
    write_current_settings,
)


def load_specs(db_path: str, table_name: str) -> List[AlertSpec]:
    """Load saved alert specs for *table_name* from the state sidecar DB."""
    ensure_alerts_tables(db_path)
    data = read_current_settings(db_path, table_name)
    if not isinstance(data, dict):
        return []
    return [AlertSpec.from_dict(d) for d in data.get("items", [])]


def save_specs(
    db_path: str,
    table_name: str,
    specs: List[AlertSpec],
    timer_min: int = 5,
) -> None:
    """Persist *specs* for *table_name* to the state sidecar DB."""
    payload = {
        "table": table_name,
        "version": 6,
        "items": [s.to_dict() for s in specs],
        "timer_min": timer_min,
    }
    write_current_settings(
        db_path, table_name, json.dumps(payload, ensure_ascii=False)
    )
