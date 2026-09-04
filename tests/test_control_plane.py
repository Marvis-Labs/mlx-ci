import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mlx_ci.contracts import ContractError, canonical_digest, seal_work_plan
from mlx_ci.control_plane import ControlPlane, SubmissionDisposition
from mlx_ci.store import StateConflict, StateStore


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.store.initialize()
        self.control = ControlPlane(self.store)

    def test_submission_queues_opaque_repository_work(self):
        plan = self.plan(
            jobs=[
                self.runner_manifest("job:first", operation="vision_generate"),
                self.runner_manifest("job:second", operation="image_embedding"),
            ]
        )

        receipt = self.control.submit(
            self.request("request:1"), plan, attempt_id="attempt:1"
        )

        self.assertEqual(receipt.disposition, SubmissionDisposition.CREATED)
        self.assertEqual(receipt.job_ids, ("job:first", "job:second"))
        manifests = [job["manifest"] for job in self.store.list_jobs()]
        self.assertEqual(
            manifests[0]["payload"]["runner_manifest"]["operation"],
            "vision_generate",
        )

    def test_delivery_replay_is_idempotent(self):
        plan = self.plan()
        first = self.control.submit(
            self.request("request:1"), plan, attempt_id="attempt:1"
        )
        replay = self.control.submit(
            self.request("request:1"), plan, attempt_id="ignored-attempt"
        )

        self.assertEqual(replay.disposition, SubmissionDisposition.REPLAYED)
        self.assertEqual(replay.attempt_id, first.attempt_id)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_active_command_is_coalesced_without_replacing_owners_plan(self):
        first_plan = self.plan()
        first = self.control.submit(
            self.request("request:1"), first_plan, attempt_id="attempt:1"
        )
        changed_plan = self.plan(
            plan_id="plan:2",
            jobs=[self.runner_manifest("job:different", operation="other")],
        )

        second = self.control.submit(
            self.request("request:2"), changed_plan, attempt_id="attempt:2"
        )

        self.assertEqual(second.disposition, SubmissionDisposition.COALESCED)
        self.assertEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.plan_digest, first.plan_digest)
        self.assertEqual(second.job_ids, ("job:first",))

    def test_command_after_terminal_attempt_creates_fresh_work(self):
        plan = self.plan()
        self.control.submit(self.request("request:1"), plan, attempt_id="attempt:1")
        self.store.set_attempt_state(
            "attempt:1", "cancelled", now="2026-09-04T12:01:00Z"
        )

        receipt = self.control.submit(
            self.request("request:2"),
            self.plan(plan_id="plan:2"),
            attempt_id="attempt:2",
        )

        self.assertEqual(receipt.disposition, SubmissionDisposition.CREATED)
        self.assertEqual(receipt.attempt_id, "attempt:2")

    def test_repository_identity_mismatch_is_rejected_without_state(self):
        plan = self.plan(repository="Example/other")

        with self.assertRaisesRegex(StateConflict, "repository"):
            self.control.submit(self.request("request:1"), plan, attempt_id="attempt:1")

        self.assertIsNone(self.store.get_attempt("attempt:1"))

    def test_invalid_plan_is_rejected_without_state(self):
        plan = self.plan()
        plan["jobs"][0]["head_sha"] = "d" * 40
        plan["jobs"][0]["manifest_digest"] = canonical_digest(
            {
                key: value
                for key, value in plan["jobs"][0].items()
                if key != "manifest_digest"
            }
        )
        plan["plan_digest"] = canonical_digest(
            {key: value for key, value in plan.items() if key != "plan_digest"}
        )

        with self.assertRaisesRegex(ContractError, "head_sha"):
            self.control.submit(self.request("request:1"), plan, attempt_id="attempt:1")

        self.assertIsNone(self.store.get_attempt("attempt:1"))

    def test_concurrent_commands_create_one_owner_attempt(self):
        def submit(number):
            return self.control.submit(
                self.request(f"request:{number}"),
                self.plan(plan_id=f"plan:{number}"),
                attempt_id=f"attempt:{number}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            receipts = list(executor.map(submit, range(1, 9)))

        dispositions = [receipt.disposition for receipt in receipts]
        self.assertEqual(dispositions.count(SubmissionDisposition.CREATED), 1)
        self.assertEqual(dispositions.count(SubmissionDisposition.COALESCED), 7)
        self.assertEqual(len({receipt.attempt_id for receipt in receipts}), 1)
        self.assertEqual(len(self.store.list_jobs()), 1)

    @staticmethod
    def request(request_id, repository="Example/project"):
        return {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": request_id,
            "repository": repository,
            "pull_request": 7,
            "comment_id": int(request_id.rsplit(":", 1)[-1]),
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }

    @classmethod
    def plan(
        cls,
        *,
        plan_id="plan:1",
        repository="Example/project",
        jobs=None,
    ):
        jobs = jobs or [cls.runner_manifest("job:first", repository=repository)]
        value = {
            "schema_version": 1,
            "kind": "repository_work_plan",
            "plan_id": plan_id,
            "repository": repository,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "jobs": jobs,
            "metadata": {"planning_outcome": "ready"},
        }
        return seal_work_plan(value)

    @staticmethod
    def runner_manifest(
        job_id,
        *,
        repository="Example/project",
        operation="generate",
    ):
        value = {
            "id": job_id,
            "repository": repository,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "component": "repository_component",
            "subject": job_id.rsplit(":", 1)[-1],
            "phases": ["synthetic", "real_checkpoint"],
            "required_memory_gib": 16,
            "required_disk_gib": 8,
            "operation": operation,
        }
        value["manifest_digest"] = canonical_digest(value)
        return value


if __name__ == "__main__":
    unittest.main()
