from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"


@dataclass(frozen=True)
class Event:
    ts: int
    type: str
    severity: Severity
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "type": self.type,
                "severity": self.severity.value,
                "detail": self.detail,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def make(type: str, severity: Severity, detail: dict[str, Any], ts: int | None = None) -> Event:
    return Event(
        ts=ts if ts is not None else int(time.time()),
        type=type,
        severity=severity,
        detail=detail,
    )
