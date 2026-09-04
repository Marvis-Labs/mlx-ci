from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mlx_ci.contracts import validate_request, validate_work_plan
from mlx_ci.store import StateError, StateStore


class SubmissionDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class SubmissionReceipt:
    repository: str
    request_id: str
    attempt_id: str
    disposition: SubmissionDisposition
    state: str
    plan_digest: str
    job_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "submission_receipt",
            "repository": self.repository,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "disposition": self.disposition.value,
            "state": self.state,
            "plan_digest": self.plan_digest,
            "job_ids": list(self.job_ids),
        }


class ControlPlane:
    def __init__(self, store: StateStore):
        self.store = store

    def submit(
        self,
        request: dict[str, Any],
        plan: dict[str, Any],
        *,
        attempt_id: str,
    ) -> SubmissionReceipt:
        request = validate_request(request)
        plan = validate_work_plan(plan)
        attempt, reused, jobs = self.store.submit_work_plan(
            request, plan, attempt_id=attempt_id
        )
        disposition = (
            SubmissionDisposition.REPLAYED if reused else SubmissionDisposition.CREATED
        )

        stored_plan = self.store.get_plan(attempt["attempt_id"])
        if stored_plan is None:
            raise StateError("submitted attempt has no durable work plan")
        return SubmissionReceipt(
            repository=attempt["repository"],
            request_id=request["request_id"],
            attempt_id=attempt["attempt_id"],
            disposition=disposition,
            state=attempt["state"],
            plan_digest=stored_plan["plan_digest"],
            job_ids=tuple(job["job_id"] for job in jobs),
        )
