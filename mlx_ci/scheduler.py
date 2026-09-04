from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from mlx_ci.store import StateError, StateStore


@dataclass(frozen=True)
class Assignment:
    manifest: dict[str, Any]
    lease: dict[str, Any]


class QueueReason(StrEnum):
    READY = "ready"
    NO_RUNNERS = "no_runners"
    NO_LIVE_RUNNERS = "no_live_runners"
    RUNNERS_BUSY = "runners_busy"
    INSUFFICIENT_RESOURCES = "insufficient_resources"
    CANDIDATES_EXHAUSTED = "candidates_exhausted"


@dataclass(frozen=True)
class QueueDiagnostic:
    attempt_id: str
    job_id: str
    job_state: str
    reason: QueueReason
    retryable: bool
    required_memory_gib: int
    required_disk_gib: int
    runner_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "queue_diagnostic",
            "attempt_id": self.attempt_id,
            "job_id": self.job_id,
            "job_state": self.job_state,
            "reason": self.reason.value,
            "retryable": self.retryable,
            "required_memory_gib": self.required_memory_gib,
            "required_disk_gib": self.required_disk_gib,
            "runner_counts": dict(self.runner_counts),
        }


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

    def respond(self, response: dict[str, Any], *, at: datetime) -> dict[str, Any]:
        return self.store.record_runner_response(
            response,
            now=_timestamp(_utc(at)),
        )

    def reap(self, *, at: datetime) -> int:
        return self.store.reap_expired(now=_timestamp(_utc(at)))

    def diagnose(
        self, attempt_id: str, job_id: str, *, at: datetime
    ) -> QueueDiagnostic:
        at = _utc(at)
        self.reap(at=at)
        facts = self.store.queue_facts(attempt_id, job_id)
        job = facts["job"]
        if job["state"] != "queued":
            raise StateError("queue diagnostics require a queued job")
        runners = facts["runners"]
        stale_before = at - self.heartbeat_timeout
        live = [
            runner
            for runner in runners
            if runner["status"] == "online"
            and _parse_timestamp(runner["heartbeat_at"]) >= stale_before
        ]
        fitting = [
            runner
            for runner in live
            if runner["memory_gib"] >= job["required_memory_gib"]
            and runner["available_disk_gib"] >= job["required_disk_gib"]
        ]
        available = [runner for runner in fitting if not runner["leased"]]
        eligible = [runner for runner in available if not runner["rejected"]]
        waiting = [runner for runner in fitting if not runner["rejected"]]

        if eligible:
            reason, retryable = QueueReason.READY, True
        elif waiting:
            reason, retryable = QueueReason.RUNNERS_BUSY, True
        elif fitting:
            reason, retryable = QueueReason.CANDIDATES_EXHAUSTED, False
        elif not runners:
            reason, retryable = QueueReason.NO_RUNNERS, True
        elif not live:
            reason, retryable = QueueReason.NO_LIVE_RUNNERS, True
        else:
            reason, retryable = QueueReason.INSUFFICIENT_RESOURCES, False

        return QueueDiagnostic(
            attempt_id=attempt_id,
            job_id=job_id,
            job_state=job["state"],
            reason=reason,
            retryable=retryable,
            required_memory_gib=job["required_memory_gib"],
            required_disk_gib=job["required_disk_gib"],
            runner_counts={
                "total": len(runners),
                "live": len(live),
                "fitting": len(fitting),
                "available": len(available),
                "eligible": len(eligible),
                "rejected": sum(bool(runner["rejected"]) for runner in fitting),
            },
        )


def _new_id(kind: str) -> str:
    return f"{kind}:{secrets.token_hex(16)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StateError("scheduler timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
