import unittest

from fetch_jira import JiraClient


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"issues": [], "isLast": True}


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse()


class JiraClientTest(unittest.TestCase):
    def test_validate_authentication_calls_myself(self):
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()

        client._validate_authentication()

        self.assertEqual(
            client.session.calls[0]["url"],
            "https://example.atlassian.net/rest/api/3/myself",
        )

    def test_validate_authentication_rejects_invalid_token(self):
        class UnauthorizedResponse(FakeResponse):
            status_code = 401

        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()
        client.session.get = lambda url, params=None, timeout=None: UnauthorizedResponse()

        with self.assertRaisesRegex(RuntimeError, "Jira authentication failed"):
            client._validate_authentication()

    def test_get_projects_does_not_hide_api_errors(self):
        class FailingJira:
            def projects(self):
                raise OSError("API unavailable")

        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.jira = FailingJira()

        with self.assertRaisesRegex(RuntimeError, "Failed to fetch Jira projects"):
            client.get_projects()

    def test_get_issues_quotes_project_key_in_jql(self):
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()

        client.get_issues_for_project("CF")

        self.assertEqual(
            client.session.calls[0]["params"]["jql"],
            'project = "CF" AND status NOT IN ("Done", "Closed", "Resolved", "Completed")',
        )


if __name__ == "__main__":
    unittest.main()
