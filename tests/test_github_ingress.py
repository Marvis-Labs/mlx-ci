import unittest

from mlx_ci.github_ingress import (
    GitHubIngress,
    GitHubIngressError,
    IngressOutcome,
    RepositoryRegistration,
)


class FakeGitHub:
    def __init__(self, *, permission="write", pull_request=None):
        self.permission = permission
        self.pull_request_value = pull_request or {
            "number": 7,
            "state": "open",
            "base": {
                "sha": "a" * 40,
                "repo": {"full_name": "Example/project-one"},
            },
            "head": {"sha": "b" * 40},
        }
        self.calls = []

    def collaborator_permission(self, repository, username):
        self.calls.append(("permission", repository, username))
        return self.permission

    def pull_request(self, repository, number):
        self.calls.append(("pull_request", repository, number))
        return self.pull_request_value


class GitHubIngressTests(unittest.TestCase):
    def test_registered_maintainer_command_resolves_immutable_run(self):
        client = FakeGitHub()
        ingress = self.ingress(client)

        decision = ingress.issue_comment(self.event(), delivery_id="delivery-1")

        self.assertEqual(decision.outcome, IngressOutcome.ACCEPTED)
        self.assertEqual(decision.reason, "authorized")
        self.assertIsNotNone(decision.run)
        self.assertEqual(decision.run.request["request_id"], "github:delivery-1")
        self.assertEqual(decision.run.request["requester"], "maintainer")
        self.assertEqual(decision.run.base_sha, "a" * 40)
        self.assertEqual(decision.run.head_sha, "b" * 40)
        self.assertEqual(decision.run.contract_sha, "c" * 40)
        self.assertEqual(
            client.calls,
            [
                ("permission", "Example/project-one", "maintainer"),
                ("pull_request", "Example/project-one", 7),
            ],
        )

    def test_author_association_is_not_authorization(self):
        client = FakeGitHub(permission="read")
        event = self.event()
        event["comment"]["author_association"] = "OWNER"

        decision = self.ingress(client).issue_comment(event, delivery_id="delivery-2")

        self.assertEqual(decision.outcome, IngressOutcome.DENIED)
        self.assertEqual(decision.reason, "insufficient_permission")
        self.assertIsNone(decision.run)
        self.assertEqual(len(client.calls), 1)

    def test_command_must_match_exactly(self):
        client = FakeGitHub()
        event = self.event()
        event["comment"]["body"] = " /ci run"

        decision = self.ingress(client).issue_comment(event, delivery_id="delivery-3")

        self.assertEqual(decision.outcome, IngressOutcome.IGNORED)
        self.assertEqual(decision.reason, "unsupported_command")
        self.assertEqual(client.calls, [])

    def test_unregistered_repository_is_ignored_before_api_calls(self):
        client = FakeGitHub()
        event = self.event(repository="Example/project-two")

        decision = self.ingress(client).issue_comment(event, delivery_id="delivery-4")

        self.assertEqual(decision.outcome, IngressOutcome.IGNORED)
        self.assertEqual(decision.reason, "repository_not_registered")
        self.assertEqual(client.calls, [])

    def test_non_pull_request_comment_is_ignored(self):
        client = FakeGitHub()
        event = self.event()
        del event["issue"]["pull_request"]

        decision = self.ingress(client).issue_comment(event, delivery_id="delivery-5")

        self.assertEqual(decision.outcome, IngressOutcome.IGNORED)
        self.assertEqual(decision.reason, "not_a_pull_request")
        self.assertEqual(client.calls, [])

    def test_closed_pull_request_is_ignored(self):
        pull_request = FakeGitHub().pull_request_value
        pull_request["state"] = "closed"
        client = FakeGitHub(pull_request=pull_request)

        decision = self.ingress(client).issue_comment(
            self.event(), delivery_id="delivery-6"
        )

        self.assertEqual(decision.outcome, IngressOutcome.IGNORED)
        self.assertEqual(decision.reason, "pull_request_not_open")

    def test_pull_request_identity_must_match_event(self):
        pull_request = FakeGitHub().pull_request_value
        pull_request["base"]["repo"]["full_name"] = "Example/project-two"

        with self.assertRaisesRegex(GitHubIngressError, "base repository"):
            self.ingress(FakeGitHub(pull_request=pull_request)).issue_comment(
                self.event(), delivery_id="delivery-7"
            )

    def test_pull_request_number_must_match_event(self):
        pull_request = FakeGitHub().pull_request_value
        pull_request["number"] = 8

        with self.assertRaisesRegex(GitHubIngressError, "number"):
            self.ingress(FakeGitHub(pull_request=pull_request)).issue_comment(
                self.event(), delivery_id="delivery-8"
            )

    def test_mutable_head_reference_is_rejected(self):
        pull_request = FakeGitHub().pull_request_value
        pull_request["head"]["sha"] = "feature-branch"

        with self.assertRaisesRegex(GitHubIngressError, "immutable commit SHA"):
            self.ingress(FakeGitHub(pull_request=pull_request)).issue_comment(
                self.event(), delivery_id="delivery-9"
            )

    def test_duplicate_registration_is_rejected(self):
        registration = RepositoryRegistration("Example/project-one", "c" * 40)

        with self.assertRaisesRegex(GitHubIngressError, "duplicated"):
            GitHubIngress([registration, registration], FakeGitHub())

    def test_invalid_delivery_identity_is_rejected(self):
        with self.assertRaisesRegex(GitHubIngressError, "run request"):
            self.ingress(FakeGitHub()).issue_comment(
                self.event(), delivery_id="invalid delivery"
            )

    @staticmethod
    def ingress(client):
        return GitHubIngress(
            [RepositoryRegistration("Example/project-one", "c" * 40)], client
        )

    @staticmethod
    def event(repository="Example/project-one"):
        return {
            "action": "created",
            "repository": {"full_name": repository},
            "issue": {
                "number": 7,
                "pull_request": {"url": "https://api.example.test/pulls/7"},
            },
            "comment": {
                "id": 42,
                "body": "/ci run",
                "created_at": "2026-09-04T12:00:00Z",
                "author_association": "NONE",
            },
            "sender": {"login": "maintainer"},
        }


if __name__ == "__main__":
    unittest.main()
