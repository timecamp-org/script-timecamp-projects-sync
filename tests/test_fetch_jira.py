import os
import unittest
from unittest.mock import patch

from fetch_jira import JiraClient, JiraFetcher


class FakeResponse:
    status_code = 200

    def __init__(self, data=None):
        self.data = data or {"issues": [], "isLast": True}

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


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

    def test_get_issues_includes_original_estimate_without_extra_api_calls(self):
        response = FakeResponse({
            "issues": [{
                "id": "10001",
                "key": "TCD-123",
                "fields": {
                    "summary": "Task name",
                    "timetracking": {
                        "originalEstimate": "2h",
                        "originalEstimateSeconds": 7200,
                    },
                },
            }],
            "isLast": True,
        })
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession(response)

        issues = client.get_issues_for_project("TCD")

        self.assertEqual(len(client.session.calls), 1)
        requested_fields = client.session.calls[0]["params"]["fields"].split(",")
        self.assertIn("timetracking", requested_fields)
        self.assertEqual(issues[0]["original_estimate"], "2h")
        self.assertEqual(issues[0]["original_estimate_seconds"], 7200)


class JiraFetcherTest(unittest.TestCase):
    def test_prefixes_issue_key_when_enabled(self):
        with patch.dict(
            os.environ,
            {
                "JIRA_INSTANCES": "[]",
                "JIRA_PREFIX_ISSUE_KEY_TO_TASK_NAME": "true",
            },
        ):
            fetcher = JiraFetcher()

        self.assertEqual(
            fetcher._format_issue_name({"key": "TCD-123", "summary": "Task name"}),
            "[TCD-123] Task name",
        )

    def test_keeps_summary_unchanged_when_prefix_is_disabled(self):
        with patch.dict(
            os.environ,
            {
                "JIRA_INSTANCES": "[]",
                "JIRA_PREFIX_ISSUE_KEY_TO_TASK_NAME": "false",
            },
        ):
            fetcher = JiraFetcher()

        self.assertEqual(
            fetcher._format_issue_name({"key": "TCD-123", "summary": "Task name"}),
            "Task name",
        )

    @patch("fetch_jira.JiraClient")
    def test_fetch_output_includes_original_estimate(self, jira_client_class):
        client = jira_client_class.return_value
        client.get_projects.return_value = [{"key": "TCD", "name": "Project"}]
        client.get_issues_for_project.return_value = [{
            "key": "TCD-123",
            "summary": "Task name",
            "parent": None,
            "original_estimate": "2h",
            "original_estimate_seconds": 7200,
        }]
        fetcher = object.__new__(JiraFetcher)
        fetcher.instances = [{
            "name": "Jira",
            "url": "https://example.atlassian.net",
            "email": "user@example.com",
            "token": "token",
        }]
        fetcher.prefix_issue_key_to_task_name = False

        data = fetcher.fetch_all_data()

        issue = next(item for item in data if item["task_id"].endswith("_TCD-123"))
        self.assertEqual(issue["original_estimate"], "2h")
        self.assertEqual(issue["original_estimate_seconds"], 7200)
        client.get_issues_for_project.assert_called_once_with("TCD")


if __name__ == "__main__":
    unittest.main()
