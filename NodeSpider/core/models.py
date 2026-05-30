from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class NodeStatus:
    is_alive: bool = False
    latency_ms: float | None = None
    speed_mbps: float | None = None
    error: str | None = None


@dataclass
class NodeIPInfo:
    country: str | None = None
    isp: str | None = None
    is_residential: bool | None = None
    risk_score: int | None = None
    proxy: bool | None = None
    hosting: bool | None = None
    query: str | None = None
    error: str | None = None


@dataclass
class NodeItem:
    protocol: str
    name: str
    address: str
    port: int
    raw_link: str
    extras: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    status: NodeStatus = field(default_factory=NodeStatus)
    ip_info: NodeIPInfo = field(default_factory=NodeIPInfo)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
