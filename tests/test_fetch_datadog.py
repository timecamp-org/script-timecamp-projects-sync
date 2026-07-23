import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from fetch_datadog import (
    DATADOG_CASES_ROOT_ID,
    DATADOG_INCIDENTS_ROOT_ID,
    DATADOG_REQUEST_TIMEOUT,
    DATADOG_UNASSIGNED_PROJECT_ID,
    DATADOG_UNASSIGNED_SERVICE_ID,
    DatadogClient,
    DatadogFetcher,
    case_task_id,
    incident_task_id,
    main,
    normalize_datadog_api_url,
    service_task_id,
)


class FakeResponse:
    def __init__(self, data=None, error=None):
        self.data = data or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses=None):
        self.headers = {}
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.responses.pop(0)


class FakeDatadogClient:
    def __init__(self, projects=None, cases=None, incidents=None):
        self.projects = projects or []
        self.cases = cases or []
        self.incidents = incidents or []

    def get_case_projects(self):
        return self.projects

    def get_cases(self):
        return self.cases

    def get_incidents(self):
        return self.incidents


class DatadogClientTest(unittest.TestCase):
    def test_configures_site_headers_and_timeout(self):
        session = FakeSession([FakeResponse({"data": []})])
        client = DatadogClient(
            "api-key",
            "app-key",
            "datadoghq.eu",
            session=session,
        )

        client.get_case_projects()

        self.assertEqual(client.base_url, "https://api.datadoghq.eu")
        self.assertEqual(session.headers["DD-API-KEY"], "api-key")
        self.assertEqual(session.headers["DD-APPLICATION-KEY"], "app-key")
        self.assertEqual(
            session.calls[0],
            {
                "url": "https://api.datadoghq.eu/api/v2/cases/projects",
                "params": {},
                "timeout": DATADOG_REQUEST_TIMEOUT,
            },
        )

    def test_normalizes_site_domains_and_urls(self):
        examples = {
            "datadoghq.com": "https://api.datadoghq.com",
            "us3.datadoghq.com/": "https://api.us3.datadoghq.com",
            "https://datadoghq.eu/": "https://api.datadoghq.eu",
            "https://api.us5.datadoghq.com": "https://api.us5.datadoghq.com",
        }
        for configured_site, expected_url in examples.items():
            with self.subTest(configured_site=configured_site):
                self.assertEqual(
                    normalize_datadog_api_url(configured_site),
                    expected_url,
                )

    @patch("fetch_datadog.DATADOG_PAGE_SIZE", 2)
    def test_paginates_cases_from_page_one(self):
        session = FakeSession([
            FakeResponse({
                "data": [{"id": "case-1"}, {"id": "case-2"}],
                "meta": {"page": {"total": 3}},
            }),
            FakeResponse({
                "data": [{"id": "case-3"}],
                "meta": {"page": {"total": 3}},
            }),
        ])
        client = DatadogClient("api", "app", session=session)

        cases = client.get_cases()

        self.assertEqual([case["id"] for case in cases], ["case-1", "case-2", "case-3"])
        self.assertEqual(
            [call["params"]["page[number]"] for call in session.calls],
            [1, 2],
        )

    @patch("fetch_datadog.DATADOG_PAGE_SIZE", 2)
    def test_paginates_incidents_using_next_offset(self):
        session = FakeSession([
            FakeResponse({
                "data": [{"id": "incident-1"}, {"id": "incident-2"}],
                "meta": {"pagination": {"next_offset": 7}},
            }),
            FakeResponse({"data": [{"id": "incident-3"}]}),
        ])
        client = DatadogClient("api", "app", session=session)

        incidents = client.get_incidents()

        self.assertEqual(
            [incident["id"] for incident in incidents],
            ["incident-1", "incident-2", "incident-3"],
        )
        self.assertEqual(
            [call["params"]["page[offset]"] for call in session.calls],
            [0, 7],
        )

    def test_propagates_http_errors(self):
        error = requests.HTTPError("Forbidden")
        session = FakeSession([FakeResponse(error=error)])
        client = DatadogClient("api", "app", session=session)

        with self.assertRaises(requests.HTTPError):
            client.get_case_projects()


class DatadogFetcherTest(unittest.TestCase):
    def test_requires_both_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                ValueError, "DATADOG_API_KEY and DATADOG_APP_KEY"
            ):
                DatadogFetcher()

    def test_builds_filtered_deterministic_hierarchies(self):
        projects = [
            {"id": "project-z", "attributes": {"name": "Zebra"}},
            {"id": "project-a", "attributes": {"name": "Alpha"}},
        ]
        cases = [
            self._case("case-2", "CASEM-2", "Beta", "SG_OPEN", "project-a"),
            self._case("case-1", "CASEM-1", "Alpha", "SG_IN_PROGRESS", "project-a"),
            self._case("case-3", "CASEM-3", "Closed", "SG_CLOSED", "project-a"),
            self._case("case-4", "CASEM-4", "Unknown project", "SG_OPEN", "missing"),
            self._case(
                "case-5",
                "CASEM-5",
                "Archived",
                "SG_OPEN",
                "project-a",
                archived_at="2026-07-21T10:00:00Z",
            ),
        ]
        incidents = [
            self._incident("incident-2", 2, "Cache outage", fields={
                "state": {"value": "stable"},
                "services": {"value": ["Zulu", " alpha "]},
            }),
            self._incident("incident-1", 1, "Database outage", state="active", fields={
                "services": {"value": ["Database"]},
            }),
            self._incident("incident-3", 3, "Unknown service", fields={
                "state": {"value": "ACTIVE"},
                "services": {"value": []},
            }),
            self._incident("incident-4", 4, "Resolved", state="resolved"),
            self._incident(
                "incident-5",
                5,
                "Archived",
                state="active",
                archived="2026-07-21T10:00:00Z",
            ),
        ]
        fetcher = DatadogFetcher(FakeDatadogClient(projects, cases, incidents))

        data = fetcher.fetch_all_data()

        self.assertEqual(
            data,
            [
                {"name": "Datadog Cases", "task_id": DATADOG_CASES_ROOT_ID, "parent_id": 0},
                {
                    "name": "Alpha",
                    "task_id": "dd_c_p_project-a",
                    "parent_id": DATADOG_CASES_ROOT_ID,
                },
                {
                    "name": "[CASEM-1] Alpha",
                    "task_id": case_task_id("case-1"),
                    "parent_id": "dd_c_p_project-a",
                },
                {
                    "name": "[CASEM-2] Beta",
                    "task_id": case_task_id("case-2"),
                    "parent_id": "dd_c_p_project-a",
                },
                {
                    "name": "Zebra",
                    "task_id": "dd_c_p_project-z",
                    "parent_id": DATADOG_CASES_ROOT_ID,
                },
                {
                    "name": "Unassigned project",
                    "task_id": DATADOG_UNASSIGNED_PROJECT_ID,
                    "parent_id": DATADOG_CASES_ROOT_ID,
                },
                {
                    "name": "[CASEM-4] Unknown project",
                    "task_id": case_task_id("case-4"),
                    "parent_id": DATADOG_UNASSIGNED_PROJECT_ID,
                },
                {
                    "name": "Datadog Incidents",
                    "task_id": DATADOG_INCIDENTS_ROOT_ID,
                    "parent_id": 0,
                },
                {
                    "name": "alpha",
                    "task_id": service_task_id("alpha"),
                    "parent_id": DATADOG_INCIDENTS_ROOT_ID,
                },
                {
                    "name": "[INC-2] Cache outage",
                    "task_id": incident_task_id("incident-2"),
                    "parent_id": service_task_id("alpha"),
                },
                {
                    "name": "Database",
                    "task_id": service_task_id("Database"),
                    "parent_id": DATADOG_INCIDENTS_ROOT_ID,
                },
                {
                    "name": "[INC-1] Database outage",
                    "task_id": incident_task_id("incident-1"),
                    "parent_id": service_task_id("Database"),
                },
                {
                    "name": "Unassigned service",
                    "task_id": DATADOG_UNASSIGNED_SERVICE_ID,
                    "parent_id": DATADOG_INCIDENTS_ROOT_ID,
                },
                {
                    "name": "[INC-3] Unknown service",
                    "task_id": incident_task_id("incident-3"),
                    "parent_id": DATADOG_UNASSIGNED_SERVICE_ID,
                },
            ],
        )

    def test_service_ids_ignore_case_and_whitespace(self):
        self.assertEqual(service_task_id(" API  Gateway "), service_task_id("api gateway"))

    def test_api_failure_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "tasks.json"
            output_path.write_text("existing data", encoding="utf-8")

            with (
                patch.dict(
                    os.environ,
                    {"DATADOG_API_KEY": "api", "DATADOG_APP_KEY": "app"},
                    clear=True,
                ),
                patch.object(
                    DatadogFetcher,
                    "fetch_all_data",
                    side_effect=requests.HTTPError("API failed"),
                ),
                patch.object(
                    sys,
                    "argv",
                    ["fetch_datadog.py", "--output", str(output_path)],
                ),
            ):
                with self.assertRaises(requests.HTTPError):
                    main()

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing data")

    @staticmethod
    def _case(case_id, key, title, status_group, project_id, archived_at=None):
        return {
            "id": case_id,
            "attributes": {
                "key": key,
                "title": title,
                "status_group": status_group,
                "archived_at": archived_at,
            },
            "relationships": {
                "project": {"data": {"id": project_id, "type": "project"}},
            },
        }

    @staticmethod
    def _incident(
        incident_id,
        public_id,
        title,
        state=None,
        fields=None,
        archived=None,
    ):
        return {
            "id": incident_id,
            "attributes": {
                "public_id": public_id,
                "title": title,
                "state": state,
                "fields": fields or {},
                "archived": archived,
            },
        }


if __name__ == "__main__":
    unittest.main()
