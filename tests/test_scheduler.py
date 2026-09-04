import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mlx_ci.contracts import seal_manifest, seal_result
from mlx_ci.scheduler import QueueReason, Scheduler
from mlx_ci.store import StateConflict, StateStore

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.store.initialize()
        self.scheduler = Scheduler(self.store)

    def test_smallest_live_runner_is_selected(self):
        self.queue_job(memory_gib=8)
        self.add_runner("studio", memory_gib=64)
        self.add_runner("mini", memory_gib=16)

        assignment = self.scheduler.dispatch(at=NOW)

        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.lease["runner_id"], "mini")

    def test_rejection_escalates_without_retrying_the_same_runner(self):
        self.queue_job(memory_gib=8)
        self.add_runner("studio", memory_gib=64)
        self.add_runner("mini", memory_gib=16)
        first = self.scheduler.dispatch(at=NOW)

        self.scheduler.reject(
            first.lease["lease_id"],
            runner_id="mini",
            generation=first.lease["generation"],
            reason="insufficient_memory",
            at=NOW + timedelta(seconds=5),
        )
        second = self.scheduler.dispatch(at=NOW + timedelta(seconds=6))

        self.assertEqual(second.lease["runner_id"], "studio")

    def test_runner_decline_contract_escalates_work(self):
        self.queue_job(memory_gib=8)
        self.add_runner("studio", memory_gib=64)
        self.add_runner("mini", memory_gib=16)
        first = self.scheduler.dispatch(at=NOW)

        self.scheduler.respond(
            self.response(first, decision="declined", reason="insufficient_memory"),
            at=NOW + timedelta(seconds=5),
        )
        second = self.scheduler.dispatch(at=NOW + timedelta(seconds=6))

        self.assertEqual(second.lease["runner_id"], "studio")

    def test_runner_acceptance_is_idempotent_and_immutable(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)
        response = self.response(assignment, decision="accepted")

        first = self.scheduler.respond(response, at=NOW + timedelta(seconds=5))
        replay = self.scheduler.respond(response, at=NOW + timedelta(seconds=6))

        self.assertEqual(first, replay)
        changed = dict(response)
        changed["observed"] = {"memory_gib": 8}
        with self.assertRaisesRegex(StateConflict, "changed"):
            self.scheduler.respond(changed, at=NOW + timedelta(seconds=7))

    def test_runner_response_must_match_lease_identity(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)
        response = self.response(assignment, decision="accepted")
        response["job_id"] = "task:forged"

        with self.assertRaisesRegex(StateConflict, "job_id"):
            self.scheduler.respond(response, at=NOW + timedelta(seconds=5))

    def test_stale_offline_and_undersized_runners_are_excluded(self):
        self.queue_job(memory_gib=32)
        self.add_runner(
            "stale-studio", memory_gib=64, heartbeat=NOW - timedelta(minutes=3)
        )
        self.add_runner("offline-studio", memory_gib=64, status="offline")
        self.add_runner("mini", memory_gib=16)
        self.add_runner("live-studio", memory_gib=64)

        assignment = self.scheduler.dispatch(at=NOW)

        self.assertEqual(assignment.lease["runner_id"], "live-studio")

    def test_no_fit_leaves_work_queued(self):
        self.queue_job(memory_gib=64)
        self.add_runner("mini", memory_gib=16)

        assignment = self.scheduler.dispatch(at=NOW)

        self.assertIsNone(assignment)
        self.assertEqual(self.store.list_jobs()[0]["state"], "queued")

    def test_queue_diagnostic_distinguishes_absent_and_insufficient_capacity(self):
        self.queue_job(memory_gib=64)

        absent = self.scheduler.diagnose("attempt:1", "task:first", at=NOW)
        self.add_runner("mini", memory_gib=16)
        undersized = self.scheduler.diagnose("attempt:1", "task:first", at=NOW)

        self.assertEqual(absent.reason, QueueReason.NO_RUNNERS)
        self.assertTrue(absent.retryable)
        self.assertEqual(undersized.reason, QueueReason.INSUFFICIENT_RESOURCES)
        self.assertFalse(undersized.retryable)

    def test_queue_diagnostic_distinguishes_busy_and_exhausted_candidates(self):
        self.queue_job(memory_gib=8)
        self.queue_job(job_id="task:second", memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        first = self.scheduler.dispatch(at=NOW)

        busy = self.scheduler.diagnose("attempt:1", "task:second", at=NOW)
        self.scheduler.respond(
            self.response(first, decision="declined", reason="busy"),
            at=NOW + timedelta(seconds=5),
        )
        exhausted = self.scheduler.diagnose(
            "attempt:1", "task:first", at=NOW + timedelta(seconds=6)
        )

        self.assertEqual(busy.reason, QueueReason.RUNNERS_BUSY)
        self.assertTrue(busy.retryable)
        self.assertEqual(exhausted.reason, QueueReason.CANDIDATES_EXHAUSTED)
        self.assertFalse(exhausted.retryable)

    def test_queue_diagnostic_marks_stale_inventory_retryable(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16, heartbeat=NOW - timedelta(minutes=3))

        diagnostic = self.scheduler.diagnose("attempt:1", "task:first", at=NOW)

        self.assertEqual(diagnostic.reason, QueueReason.NO_LIVE_RUNNERS)
        self.assertTrue(diagnostic.retryable)

    def test_expired_lease_requeues_work_and_releases_runner(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        first = self.scheduler.dispatch(at=NOW)

        self.add_runner("mini", memory_gib=16, heartbeat=NOW + timedelta(minutes=6))
        second = self.scheduler.dispatch(at=NOW + timedelta(minutes=6))

        self.assertNotEqual(first.lease["lease_id"], second.lease["lease_id"])
        self.assertEqual(second.lease["runner_id"], "mini")
        leases = self.store.list_leases()
        self.assertEqual(leases[0]["release_reason"], "expired")

    def test_lease_renewal_is_owner_safe(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)

        with self.assertRaisesRegex(StateConflict, "owner"):
            self.scheduler.renew(
                assignment.lease["lease_id"],
                runner_id="mini",
                generation="generation:wrong",
                at=NOW + timedelta(minutes=1),
            )

        renewed = self.scheduler.renew(
            assignment.lease["lease_id"],
            runner_id="mini",
            generation=assignment.lease["generation"],
            at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(renewed["expires_at"], "2026-09-04T12:06:00Z")

        with self.assertRaisesRegex(StateConflict, "backwards"):
            self.scheduler.renew(
                assignment.lease["lease_id"],
                runner_id="mini",
                generation=assignment.lease["generation"],
                at=NOW + timedelta(seconds=30),
            )

    def test_result_must_match_repository_job_and_lease_owner(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)
        result = self.result(assignment)
        wrong_repository = dict(result)
        wrong_repository["repository"] = "Example/project-two"

        with self.assertRaisesRegex(StateConflict, "repository"):
            self.scheduler.complete(
                seal_result(wrong_repository),
                generation=assignment.lease["generation"],
                at=NOW + timedelta(minutes=1),
            )

        sealed = seal_result(result)
        completed = self.scheduler.complete(
            sealed,
            generation=assignment.lease["generation"],
            at=NOW + timedelta(minutes=1),
        )
        replay = self.scheduler.complete(
            sealed,
            generation=assignment.lease["generation"],
            at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(completed, replay)
        self.assertEqual(self.store.list_jobs()[0]["state"], "completed")

    def test_result_cannot_claim_execution_before_lease_acquisition(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)
        result = self.result(assignment)
        result["started_at"] = "2026-09-04T11:59:00Z"

        with self.assertRaisesRegex(StateConflict, "before its lease"):
            self.scheduler.complete(
                seal_result(result),
                generation=assignment.lease["generation"],
                at=NOW + timedelta(minutes=1),
            )

    def test_concurrent_dispatch_cannot_double_lease_work_or_runner(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)

        with ThreadPoolExecutor(max_workers=8) as executor:
            assignments = list(
                executor.map(lambda _: self.scheduler.dispatch(at=NOW), range(8))
            )

        claimed = [assignment for assignment in assignments if assignment is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(self.store.list_leases()), 1)

    def test_terminal_attempt_releases_its_runner(self):
        self.queue_job(memory_gib=8)
        self.add_runner("mini", memory_gib=16)
        assignment = self.scheduler.dispatch(at=NOW)

        self.store.set_attempt_state(
            "attempt:1", "cancelled", now="2026-09-04T12:01:00Z"
        )

        lease = self.store.list_leases()[0]
        self.assertEqual(lease["release_reason"], "attempt_cancelled")
        self.assertEqual(self.store.list_jobs()[0]["state"], "cancelled")
        with self.assertRaisesRegex(StateConflict, "active lease"):
            self.scheduler.renew(
                assignment.lease["lease_id"],
                runner_id="mini",
                generation=assignment.lease["generation"],
                at=NOW + timedelta(minutes=1),
            )

    def test_queue_and_leases_are_global_across_repositories(self):
        self.queue_job(memory_gib=8)
        self.queue_job(
            repository="Example/project-two",
            attempt_id="attempt:two",
            request_id="request:two",
            job_id="task:second",
            memory_gib=8,
        )
        self.add_runner("mini-1", memory_gib=16)
        self.add_runner("mini-2", memory_gib=16)

        first = self.scheduler.dispatch(at=NOW)
        second = self.scheduler.dispatch(at=NOW)

        self.assertEqual(
            {first.manifest["repository"], second.manifest["repository"]},
            {"Example/project-one", "Example/project-two"},
        )
        self.assertNotEqual(first.lease["runner_id"], second.lease["runner_id"])

    def queue_job(
        self,
        *,
        repository="Example/project-one",
        attempt_id="attempt:1",
        request_id="request:1",
        job_id="task:first",
        memory_gib,
    ):
        request = {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": request_id,
            "repository": repository,
            "pull_request": 7,
            "comment_id": 99,
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }
        self.store.create_attempt(
            request,
            attempt_id=attempt_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        manifest = seal_manifest(
            {
                "schema_version": 1,
                "kind": "work_manifest",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "repository": repository,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "contract_sha": "c" * 40,
                "component": "repository_component",
                "subject": job_id.rsplit(":", 1)[-1],
                "phases": ["prepare", "execute"],
                "required_memory_gib": memory_gib,
                "required_disk_gib": 8,
                "payload": {"operation": "example"},
            }
        )
        self.store.enqueue_jobs(attempt_id, [manifest], now="2026-09-04T12:00:00Z")

    def add_runner(
        self,
        runner_id,
        *,
        memory_gib,
        heartbeat=NOW,
        status="online",
    ):
        self.store.record_runner(
            {
                "schema_version": 1,
                "kind": "runner_capability",
                "runner_id": runner_id,
                "labels": ["apple-silicon", "mlx-ci-sandbox-v1"],
                "memory_gib": memory_gib,
                "available_disk_gib": 128,
                "status": status,
                "heartbeat_at": heartbeat.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
            }
        )

    @staticmethod
    def result(assignment):
        return {
            "schema_version": 1,
            "kind": "work_result",
            "job_id": assignment.manifest["job_id"],
            "attempt_id": assignment.manifest["attempt_id"],
            "repository": assignment.manifest["repository"],
            "runner_id": assignment.lease["runner_id"],
            "lease_id": assignment.lease["lease_id"],
            "outcome": "passed",
            "failure_code": None,
            "evidence": {"synthetic": {"outcome": "passed"}},
            "started_at": "2026-09-04T12:00:00Z",
            "finished_at": "2026-09-04T12:01:00Z",
        }

    @staticmethod
    def response(assignment, *, decision, reason=None):
        return {
            "schema_version": 1,
            "kind": "runner_response",
            "lease_id": assignment.lease["lease_id"],
            "attempt_id": assignment.lease["attempt_id"],
            "job_id": assignment.lease["job_id"],
            "runner_id": assignment.lease["runner_id"],
            "generation": assignment.lease["generation"],
            "decision": decision,
            "reason": reason,
            "observed": {"memory_gib": 16, "available_disk_gib": 128},
        }


if __name__ == "__main__":
    unittest.main()
