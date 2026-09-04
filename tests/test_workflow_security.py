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
        self.assertIn("permissions: {}", source)
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertNotRegex(source, r"uses:\s+[^\s@]+@(main|master|v\d+)(?:\s|$)")
        self.assertEqual(len(re.findall(r"uses:\s+[^\s@]+@[0-9a-f]{40}", source)), 14)
        self.assertEqual(source.count("persist-credentials: false"), 8)


if __name__ == "__main__":
    unittest.main()
