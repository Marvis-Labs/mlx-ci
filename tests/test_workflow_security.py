import re
import unittest
from pathlib import Path


class WorkflowSecurityTests(unittest.TestCase):
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
        self.assertIn("-- /usr/bin/python3 -m ci.work_executor", source)
        self.assertIn("python -m ci.repository_adapter prepare", source)
        self.assertIn("python -m ci.repository_adapter report", source)
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
        self.assertEqual(len(re.findall(r"uses:\s+[^\s@]+@[0-9a-f]{40}", source)), 13)
        self.assertEqual(source.count("persist-credentials: false"), 5)

    def test_superseded_reusable_workflows_are_removed(self):
        workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"

        self.assertFalse((workflows / "repository-ci.yml").exists())
        self.assertFalse((workflows / "repository-plan.yml").exists())


if __name__ == "__main__":
    unittest.main()
