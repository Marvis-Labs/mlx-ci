import unittest
from unittest.mock import patch

from mlx_ci.github_dispatch import authorize_dispatch


class FakeGitHubAPI:
    def __init__(self, token):
        self.token = token

    def issue_comment(self, repository, comment_id):
        return {
            "id": comment_id,
            "body": "/ci run",
            "created_at": "2026-09-05T12:00:00Z",
            "issue_url": f"https://api.github.com/repos/{repository}/issues/9",
            "user": {"login": "maintainer"},
        }

    def collaborator_permission(self, repository, username):
        return "maintain"

    def pull_request(self, repository, number):
        return {
            "number": number,
            "state": "open",
            "base": {"sha": "a" * 40, "repo": {"full_name": repository}},
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "Contributor/project"},
            },
        }


class GitHubDispatchTests(unittest.TestCase):
    @patch("mlx_ci.github_dispatch.GitHubAPI", FakeGitHubAPI)
    def test_authorizes_bounded_dispatch_into_immutable_identity(self):
        result = authorize_dispatch(
            {
                "action": "ci-run-request",
                "client_payload": {
                    "schema_version": 1,
                    "repository": "Example/project",
                    "pull_request": 9,
                    "comment_id": 71,
                },
            },
            token="x" * 40,
            repositories=["Example/project"],
        )

        self.assertEqual(result["repository"], "Example/project")
        self.assertEqual(result["comment_id"], 71)
        self.assertEqual(result["base_sha"], "a" * 40)
        self.assertEqual(result["head_sha"], "b" * 40)
        self.assertEqual(result["contract_sha"], "a" * 40)
        self.assertEqual(result["head_repository"], "Contributor/project")
        self.assertEqual(result["request"]["request_id"], "github:comment:71")


if __name__ == "__main__":
    unittest.main()
