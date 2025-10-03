# utils/alerts/__init__.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Callable, List, Optional, Protocol, TypedDict
import uuid
import enum

# -------- Registry --------
REGISTRY: Dict[str, "AlertHandler"] = {}

def register(cls):
    kind = getattr(cls, "kind", None)
    if not kind:
        raise ValueError(f"{cls.__name__} missing class attribute 'kind'")
    REGISTRY[kind] = cls()
    return cls

# -------- Status model --------
class Status(enum.Enum):
    OFF = "OFF"       # not enabled
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"

@dataclass
class AlertSpec:
    id: str
    kind: str
    name: str
    enabled: bool
    recipients: List[str]
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "enabled": self.enabled,
            "recipients": self.recipients,
            "payload": self.payload,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AlertSpec":
        return AlertSpec(
            id=d.get("id") or str(uuid.uuid4()),
            kind=str(d.get("kind", "")),
            name=str(d.get("name", "")),
            enabled=bool(d.get("enabled", False)),
            recipients=list(d.get("recipients", [])),
            payload=dict(d.get("payload", {})),
        )

class EvalResult(TypedDict, total=False):
    status: Status
    observed: float
    summary: str    # short human text to show in table
    extra: Dict[str, Any]

# Host contract (the table tab) – same assumptions you used before
class Host(Protocol):
    df: Any                        # pandas.DataFrame
    datetime_col: str
    table_name: str
    def update_map(self) -> None: ...
    @property
    def latlong_widget(self) -> Any: ...

# -------- Handler Protocol --------
class AlertHandler(Protocol):
    kind: str        # Unique key used in the registry menu, e.g. "Distance"

    def default_spec(self, host: Host) -> AlertSpec: ...
    def create_editor(self, spec: AlertSpec, host: Host, parent=None): ...
    def evaluate(self, spec: AlertSpec, host: Host) -> EvalResult: ...

# -------- Built-in handlers (import for side-effects: @register) --------
# Ensure these modules are imported so their @register runs and REGISTRY is populated.
from . import threshold_alert      # noqa: F401
from . import distance_alert       # noqa: F401
from . import stale_alert          # noqa: F401
from . import missing_data_alert   # noqa: F401
