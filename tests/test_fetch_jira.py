import unittest

from fetch_jira import JiraClient


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"issues": [], "isLast": True}


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse()


class JiraClientTest(unittest.TestCase):
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
