import unittest
from pathlib import Path


class WorkflowSecurityTests(unittest.TestCase):
    def test_plan_workflow_runs_only_trusted_control_code(self):
        source = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "repository-plan.yml"
        ).read_text()

        self.assertIn("types: [ci-plan-request]", source)
        self.assertIn("python -m mlx_ci.github_dispatch", source)
        self.assertIn("python -m mlx_ci.repository.adapter plan", source)
        self.assertIn("python -m mlx_ci.repository.adapter hosted-checks", source)
        self.assertIn("Publish planning failure", source)
        self.assertIn("steps.planning.outcome != 'success'", source)
        self.assertIn("PYTHONPATH=orchestrator:control", source)
        self.assertNotIn("PYTHONPATH=head", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("issue_comment", source)
        self.assertNotIn("self-hosted", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertEqual(
            source.count("persist-credentials: false"),
            source.count("uses: actions/checkout@"),
        )
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")

    def test_private_dispatch_workflow_keeps_trust_boundaries_explicit(self):
        source = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "repository-dispatch.yml"
        ).read_text()

        self.assertIn("repository_dispatch:", source)
        self.assertIn("types: [ci-run-request]", source)
        self.assertIn("python -m mlx_ci.github_dispatch", source)
        self.assertIn("cancel-in-progress: false", source)
        self.assertIn("<!-- mlx-ci-request:$COMMENT_ID -->", source)
        self.assertIn("already_reported", source)
        self.assertIn("-- /usr/bin/python3 -m mlx_ci.repository.executor", source)
        self.assertIn("python -m mlx_ci.repository.adapter prepare", source)
        self.assertIn("python -m mlx_ci.repository.adapter report", source)
        self.assertIn("--repository-path control", source)
        self.assertIn('--pr-number "$PR_NUMBER"', source)
        self.assertIn('--run-url "$RUN_URL"', source)
        self.assertIn("Render infrastructure fallback", source)
        self.assertIn("steps.renderer.outcome != 'success'", source)
        self.assertNotIn("/usr/bin/env PYTHONPATH=", source)
        self.assertIn("permissions: {}", source)
        self.assertNotIn("author_association", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("issue_comment", source)
        self.assertNotIn("secrets: inherit", source)
        device = source.split("  device:", 1)[1].split("  report:", 1)[0]
        self.assertNotIn("MLX_CI_APP_PRIVATE_KEY", device)
        self.assertNotIn("create-github-app-token", device)
        self.assertIn("shasum -a 256 -c sources.sha256", device)
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")
        self.assertRegex(source, r"uses:\s+[^\s@]+@[0-9a-f]{40}")
        self.assertEqual(
            source.count("persist-credentials: false"),
            source.count("uses: actions/checkout@"),
        )

    def test_superseded_reusable_workflow_is_removed(self):
        workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"

        self.assertFalse((workflows / "repository-ci.yml").exists())


if __name__ == "__main__":
    unittest.main()
