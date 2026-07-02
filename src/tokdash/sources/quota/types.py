from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class QuotaSnapshot:
    provider: str
    account: str
    bucket: str
    bucket_label: str
    used_percent: float | None
    resets_at: int | None
    plan: str | None
    captured_at: int
    source: str
    status: str
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
