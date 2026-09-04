from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mlx_ci.store import StateError, StateStore


@dataclass(frozen=True)
class Assignment:
    manifest: dict[str, Any]
    lease: dict[str, Any]


class Scheduler:
    def __init__(
        self,
        store: StateStore,
        *,
        lease_ttl: timedelta = timedelta(minutes=5),
        heartbeat_timeout: timedelta = timedelta(minutes=2),
        id_factory: Callable[[str], str] | None = None,
    ):
        if lease_ttl <= timedelta(0) or heartbeat_timeout <= timedelta(0):
            raise ValueError("scheduler durations must be positive")
        self.store = store
        self.lease_ttl = lease_ttl
        self.heartbeat_timeout = heartbeat_timeout
        self.id_factory = id_factory or _new_id

    def dispatch(self, *, at: datetime) -> Assignment | None:
        at = _utc(at)
        claimed = self.store.claim_next(
            lease_id=self.id_factory("lease"),
            generation=self.id_factory("generation"),
            now=_timestamp(at),
            expires_at=_timestamp(at + self.lease_ttl),
            stale_before=_timestamp(at - self.heartbeat_timeout),
        )
        if claimed is None:
            return None
        return Assignment(manifest=claimed["manifest"], lease=claimed["lease"])

    def renew(
        self, lease_id: str, *, runner_id: str, generation: str, at: datetime
    ) -> dict[str, Any]:
        at = _utc(at)
        return self.store.renew_lease(
            lease_id,
            runner_id=runner_id,
            generation=generation,
            now=_timestamp(at),
            expires_at=_timestamp(at + self.lease_ttl),
        )

    def reject(
        self,
        lease_id: str,
        *,
        runner_id: str,
        generation: str,
        reason: str,
        at: datetime,
    ) -> None:
        self.store.reject_lease(
            lease_id,
            runner_id=runner_id,
            generation=generation,
            reason=reason,
            now=_timestamp(_utc(at)),
        )

    def complete(
        self, result: dict[str, Any], *, generation: str, at: datetime
    ) -> dict[str, Any]:
        return self.store.complete_lease(
            result,
            generation=generation,
            now=_timestamp(_utc(at)),
        )

    def reap(self, *, at: datetime) -> int:
        return self.store.reap_expired(now=_timestamp(_utc(at)))


def _new_id(kind: str) -> str:
    return f"{kind}:{secrets.token_hex(16)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StateError("scheduler timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
