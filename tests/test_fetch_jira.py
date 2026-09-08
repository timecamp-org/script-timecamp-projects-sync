import os
import unittest
from unittest.mock import patch

from fetch_jira import JiraClient, JiraFetcher, get_jira_archiving_delay_days
from src.jira_client import (
    build_jira_issue_external_id,
    generate_jira_org_id,
    parse_jira_issue_external_id,
)


class FakeResponse:
    status_code = 200
    content = b"{}"

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

    def request(
        self,
        method,
        url,
        params=None,
        json=None,
        timeout=None,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
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
        client.session.get = (
            lambda url, params=None, timeout=None: UnauthorizedResponse()
        )

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

    def test_get_issues_keeps_recently_archived_tasks_for_default_delay(self):
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()

        client.get_issues_for_project("CF")

        self.assertEqual(
            client.session.calls[0]["params"]["jql"],
            'project = "CF" AND (status NOT IN '
            '("Done", "Closed", "Resolved", "Completed") OR '
            'statusCategoryChangedDate >= -14d)',
        )

    def test_get_issues_uses_configured_archiving_delay(self):
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()

        client.get_issues_for_project("CF", archiving_delay_days=3)

        self.assertEqual(
            client.session.calls[0]["params"]["jql"],
            'project = "CF" AND (status NOT IN '
            '("Done", "Closed", "Resolved", "Completed") OR '
            'statusCategoryChangedDate >= -3d)',
        )

    def test_zero_archiving_delay_excludes_archived_statuses_immediately(self):
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession()

        client.get_issues_for_project("CF", archiving_delay_days=0)

        self.assertEqual(
            client.session.calls[0]["params"]["jql"],
            'project = "CF" AND status NOT IN '
            '("Done", "Closed", "Resolved", "Completed")',
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

    def test_worklog_methods_use_v3_and_leave_estimate_unchanged(self):
        response = FakeResponse({"id": "9001"})
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession(response)

        created = client.create_worklog(
            "TCD-123",
            {"timeSpentSeconds": 3600},
        )
        client.update_worklog(
            "TCD-123",
            "9001",
            {"timeSpentSeconds": 1800},
        )
        client.delete_worklog("TCD-123", "9001")

        self.assertEqual(created, {"id": "9001"})
        self.assertEqual(
            [call["method"] for call in client.session.calls],
            ["POST", "PUT", "DELETE"],
        )
        self.assertEqual(
            client.session.calls[0]["url"],
            "https://example.atlassian.net/rest/api/3/issue/TCD-123/worklog",
        )
        self.assertEqual(
            client.session.calls[1]["url"],
            "https://example.atlassian.net/rest/api/3/issue/TCD-123/worklog/9001",
        )
        self.assertEqual(
            client.session.calls[2]["params"],
            {"adjustEstimate": "leave"},
        )

    def test_rejects_duplicate_timecamp_worklog_properties(self):
        response = FakeResponse(
            {
                "worklogs": [
                    {
                        "id": "9001",
                        "properties": [
                            {
                                "key": "timecamp.entry",
                                "value": {"entryId": "101"},
                            }
                        ],
                    },
                    {
                        "id": "9002",
                        "properties": [
                            {
                                "key": "timecamp.entry",
                                "value": {"entryId": "101"},
                            }
                        ],
                    },
                ],
                "total": 2,
            }
        )
        client = object.__new__(JiraClient)
        client.server = "https://example.atlassian.net"
        client.session = FakeSession(response)

        with self.assertRaisesRegex(ValueError, "multiple worklogs"):
            client.get_timecamp_worklog_map("TCD-123")


class JiraExternalIdTest(unittest.TestCase):
    def test_round_trips_issue_id_with_underscored_project_key(self):
        instance_id = generate_jira_org_id("https://example.atlassian.net")
        external_id = build_jira_issue_external_id(
            instance_id,
            "TCD_CORE",
            "TCD_CORE-123",
        )

        target = parse_jira_issue_external_id(external_id, [instance_id])

        self.assertIsNotNone(target)
        self.assertEqual(target.instance_id, instance_id)
        self.assertEqual(target.issue_key, "TCD_CORE-123")

    def test_rejects_project_issue_key_mismatch(self):
        self.assertIsNone(
            parse_jira_issue_external_id(
                "org_1_proj_TCD_OTHER-123",
                ["org_1"],
            )
        )

    def test_parses_sync_prefix_added_by_timecamp_project_sync(self):
        target = parse_jira_issue_external_id(
            "sync_org_1_proj_TCD_TCD-123",
            ["org_1"],
        )

        self.assertIsNotNone(target)
        self.assertEqual(target.instance_id, "org_1")
        self.assertEqual(target.issue_key, "TCD-123")


class JiraFetcherTest(unittest.TestCase):
    def test_archiving_delay_defaults_to_14_days(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_jira_archiving_delay_days(), 14)

    def test_archiving_delay_uses_environment_override(self):
        with patch.dict(
            os.environ,
            {"JIRA_ARCHIVING_DELAY_DAYS": "3"},
            clear=True,
        ):
            self.assertEqual(get_jira_archiving_delay_days(), 3)

    def test_archiving_delay_rejects_invalid_environment_value(self):
        for value in ("tomorrow", "-1", "1.5"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {"JIRA_ARCHIVING_DELAY_DAYS": value},
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "JIRA_ARCHIVING_DELAY_DAYS must be a non-negative whole number",
                ):
                    get_jira_archiving_delay_days()

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
        fetcher.archiving_delay_days = 14

        data = fetcher.fetch_all_data()

        issue = next(item for item in data if item["task_id"].endswith("_TCD-123"))
        self.assertEqual(issue["original_estimate"], "2h")
        self.assertEqual(issue["original_estimate_seconds"], 7200)
        self.assertTrue(issue["restore_if_archived"])
        client.get_issues_for_project.assert_called_once_with(
            "TCD",
            archiving_delay_days=14,
        )


if __name__ == "__main__":
    unittest.main()
