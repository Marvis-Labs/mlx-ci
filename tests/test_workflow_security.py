import re
import unittest
from pathlib import Path


class WorkflowSecurityTests(unittest.TestCase):
    def test_reusable_workflow_keeps_trust_boundaries_explicit(self):
        source = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "repository-ci.yml"
        ).read_text()

        self.assertIn("workflow_call:", source)
        self.assertIn("github.event.comment.body == '/ci run'", source)
        self.assertIn("Verify maintainer permission", source)
        self.assertIn('PYTHONPATH="$GITHUB_WORKSPACE/control"', source)
        self.assertIn("--repository-path control", source)
        self.assertIn('--pr-number "$PR_NUMBER"', source)
        self.assertIn('--run-url "$RUN_URL"', source)
        self.assertIn("Render infrastructure fallback", source)
        self.assertIn("steps.renderer.outcome != 'success'", source)
        self.assertIn("permissions: {}", source)
        self.assertNotIn("author_association", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")
        self.assertEqual(len(re.findall(r"uses:\s+[^\s@]+@[0-9a-f]{40}", source)), 14)
        self.assertEqual(source.count("persist-credentials: false"), 8)

    def test_pull_request_plan_uses_trusted_repository_code(self):
        source = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "repository-plan.yml"
        ).read_text()

        self.assertIn("workflow_call:", source)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", source)
        self.assertIn("PYTHONPATH=control python -m ci.control plan", source)
        self.assertIn("PYTHONPATH=control python -m ci.hosted_checks", source)
        self.assertIn("--repository control", source)
        self.assertIn("permissions: {}", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")
        self.assertEqual(len(re.findall(r"uses:\s+[^\s@]+@[0-9a-f]{40}", source)), 6)
        self.assertEqual(source.count("persist-credentials: false"), 2)


if __name__ == "__main__":
    unittest.main()
