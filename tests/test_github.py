import unittest
from pathlib import Path
from unittest.mock import patch

from runners.contract import ContractError
from runners.engines import load_engines
from runners.github import resolve_request
from runners.prepare import coalesced_run, prepare

ENGINES = load_engines(Path(__file__).resolve().parents[1] / "engines")


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "schema_version": 1,
            "engine": "vlm",
            "repository": "Marvis-Labs/mlx-vlm",
            "pull_request": 42,
            "comment_id": 123,
        }
        repository = self.request["repository"]
        self.responses = {
            f"repos/{repository}/issues/comments/123": {
                "id": 123,
                "body": "/ci run",
                "created_at": "2026-09-28T18:24:42Z",
                "issue_url": f"https://api.github.com/repos/{repository}/issues/42",
                "user": {"login": "Lazarus-931"},
            },
            f"repos/{repository}/pulls/42": {
                "number": 42,
                "state": "open",
                "base": {
                    "ref": "main",
                    "sha": "a" * 40,
                    "repo": {"full_name": repository},
                },
                "head": {
                    "sha": "b" * 40,
                    "repo": {"full_name": "contributor/mlx-vlm"},
                },
            },
            f"repos/{repository}/branches/main": {"commit": {"sha": "a" * 40}},
        }
        self.files = [
            {"filename": "mlx_vlm/models/qwen2_vl/vision.py", "status": "modified"},
            {
                "filename": "mlx_vlm/models/florence2/model.py",
                "previous_filename": "mlx_vlm/models/florence2/florence2.py",
                "status": "renamed",
            },
        ]

    def resolve(self):
        return resolve_request(
            self.request,
            ENGINES,
            self.responses.__getitem__,
            lambda _: self.files,
        )

    def test_resolves_main_and_pr_head_from_github(self):
        attempt = self.resolve()
        self.assertEqual(attempt["base_sha"], "a" * 40)
        self.assertEqual(attempt["head_sha"], "b" * 40)
        self.assertEqual(attempt["contract_sha"], "a" * 40)
        self.assertEqual(attempt["requested_at"], "2026-09-28T18:24:42Z")
        self.assertEqual(
            attempt["changed_files"],
            [
                "mlx_vlm/models/florence2/florence2.py",
                "mlx_vlm/models/florence2/model.py",
                "mlx_vlm/models/qwen2_vl/vision.py",
            ],
        )

    def test_non_maintainer_is_rejected(self):
        self.responses["repos/Marvis-Labs/mlx-vlm/issues/comments/123"]["user"][
            "login"
        ] = "outsider"
        with self.assertRaises(ContractError):
            self.resolve()

    def test_current_main_is_pinned_when_pr_is_behind(self):
        self.responses["repos/Marvis-Labs/mlx-vlm/branches/main"]["commit"]["sha"] = (
            "c" * 40
        )
        attempt = self.resolve()
        self.assertEqual(attempt["base_sha"], "c" * 40)
        self.assertEqual(attempt["contract_sha"], "c" * 40)

    def test_pull_request_must_target_main(self):
        self.responses["repos/Marvis-Labs/mlx-vlm/pulls/42"]["base"]["ref"] = "dev"
        with self.assertRaises(ContractError):
            self.resolve()

    def test_audio_engine_is_registered(self):
        self.assertEqual(ENGINES["audio"]["repository"], "Marvis-Labs/mlx-audio")

    def test_head_repository_cannot_be_a_path_or_url(self):
        self.responses["repos/Marvis-Labs/mlx-vlm/pulls/42"]["head"]["repo"][
            "full_name"
        ] = "../untrusted"
        with self.assertRaises(ContractError):
            self.resolve()

    def test_prepare_accepts_only_the_registered_dispatch(self):
        event = {"action": "ci-run-request", "client_payload": self.request}
        directory = Path(__file__).resolve().parents[1] / "engines"
        with (
            patch(
                "runners.prepare.github_get",
                side_effect=lambda path, _: self.responses[path],
            ),
            patch("runners.prepare.github_files", return_value=self.files),
        ):
            attempt = prepare(event, directory, "token", 17, 2)
            self.assertEqual(attempt["head_sha"], "b" * 40)
            self.assertEqual(attempt["run_id"], 17)
            self.assertEqual(attempt["run_attempt"], 2)
        with self.assertRaises(ContractError):
            prepare({**event, "action": "other"}, directory, "token", 17, 2)

    def test_prepare_rejects_invalid_run_identity(self):
        event = {"action": "ci-run-request", "client_payload": self.request}
        directory = Path(__file__).resolve().parents[1] / "engines"
        with (
            patch(
                "runners.prepare.github_get",
                side_effect=lambda path, _: self.responses[path],
            ),
            patch("runners.prepare.github_files", return_value=self.files),
        ):
            with self.assertRaises(ContractError):
                prepare(event, directory, "token", 0, 1)

    def test_same_revision_active_or_later_completed_run_is_coalesced(self):
        attempt = {
            **self.resolve(),
            "schema_version": 2,
            "run_id": 20,
            "run_attempt": 1,
        }
        previous = {**attempt, "run_id": 17, "comment_id": 122}
        active = {
            "id": 17,
            "status": "in_progress",
            "updated_at": "2026-09-28T18:24:40Z",
        }
        completed_after_request = {
            **active,
            "status": "completed",
            "conclusion": "success",
            "updated_at": "2026-09-28T18:24:43Z",
        }
        completed_before_request = {
            **active,
            "status": "completed",
            "updated_at": "2026-09-28T18:24:41Z",
        }
        self.assertEqual(coalesced_run(attempt, [(active, previous)]), 17)
        self.assertEqual(
            coalesced_run(
                attempt,
                [
                    ({**active, "id": 19}, {**previous, "run_id": 19}),
                    (active, previous),
                ],
            ),
            17,
        )
        self.assertEqual(
            coalesced_run(attempt, [(completed_after_request, previous)]), 17
        )
        self.assertIsNone(
            coalesced_run(attempt, [(completed_before_request, previous)])
        )
        self.assertIsNone(
            coalesced_run(
                attempt,
                [({**completed_after_request, "conclusion": "failure"}, previous)],
            )
        )
        self.assertIsNone(
            coalesced_run(
                attempt,
                [(active, {**previous, "head_sha": "c" * 40})],
            )
        )
