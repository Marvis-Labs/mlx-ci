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
        self.assertIn("python -m ci.repository_adapter plan", source)
        self.assertIn("python -m ci.repository_adapter hosted-checks", source)
        self.assertNotIn("ci.component_config", source)
        self.assertNotIn("git -C control fetch", source)
        self.assertIn("permissions: {}", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")
        self.assertEqual(len(re.findall(r"uses:\s+[^\s@]+@[0-9a-f]{40}", source)), 8)
        self.assertEqual(source.count("persist-credentials: false"), 4)


if __name__ == "__main__":
    unittest.main()
