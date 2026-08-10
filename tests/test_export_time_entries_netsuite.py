import unittest

from export_time_entries_netsuite import (
    build_employee_mapping,
    format_netsuite_hours,
    prepare_timebills,
)


class ExportTimeEntriesNetSuiteTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "time_export": {
                "duration_rounding": "nearest",
                "fields": {
                    "employee": "employee",
                    "date": "tranDate",
                    "hours": "hours",
                    "project": "customer",
                    "project_task": "caseTaskEvent",
                    "activity": "item",
                    "memo": "memo",
                },
                "classification": {
                    "mode": "field",
                    "field": "custcol_capex_opex",
                    "value_format": "id",
                    "value_map": {"CAPEX": "11", "OPEX": "12"},
                },
                "fixed_fields": {"isBillable": True},
            }
        }
        self.timecamp_tasks = [
            {
                "task_id": "900",
                "external_task_id": "netsuite_project_task_100",
            }
        ]
        self.source_tasks = [
            {
                "task_id": "netsuite_project_task_100",
                "netsuite": {
                    "project_id": "10",
                    "project_task_id": "100",
                    "activity_id": "501",
                    "capex_opex": "CAPEX",
                },
            }
        ]

    def test_employee_mapping_uses_unique_email_and_explicit_override(self):
        timecamp_users = [
            {"user_id": "1", "email": "One@Example.com"},
            {"user_id": "2", "email": "duplicate@example.com"},
            {"user_id": "3", "email": "explicit@example.com"},
        ]
        netsuite_employees = [
            {"ID": "101", "EMAIL": "one@example.com"},
            {"id": "201", "email": "duplicate@example.com"},
            {"id": "202", "email": "duplicate@example.com"},
        ]

        mapping = build_employee_mapping(
            timecamp_users,
            netsuite_employees,
            {"3": "303"},
        )

        self.assertEqual(mapping, {"1": "101", "3": "303"})

    def test_employee_mapping_rejects_non_object_override(self):
        with self.assertRaisesRegex(ValueError, "must be an object"):
            build_employee_mapping([], [], ["invalid"])

    def test_formats_duration_at_netsuite_minute_precision(self):
        self.assertEqual(format_netsuite_hours(5400), "1:30")
        self.assertEqual(format_netsuite_hours(3631, "nearest"), "1:01")
        self.assertEqual(format_netsuite_hours(3659, "floor"), "1:00")
        self.assertEqual(format_netsuite_hours(3601, "ceil"), "1:01")
        with self.assertRaisesRegex(ValueError, "not a whole minute"):
            format_netsuite_hours(3601, "reject")

    def test_prepares_idempotent_timebill_payload(self):
        entries = [
            {
                "id": "700",
                "task_id": "900",
                "user_id": "1",
                "date": "2026-08-03",
                "duration": "5431",
                "description": "Architecture workshop",
            }
        ]

        prepared, skipped, errors = prepare_timebills(
            entries,
            self.timecamp_tasks,
            self.source_tasks,
            {"1": "101"},
            self.config,
        )

        self.assertEqual(skipped, {})
        self.assertEqual(errors, [])
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].external_id, "timecamp-700")
        self.assertEqual(
            prepared[0].payload,
            {
                "isBillable": True,
                "employee": {"id": "101"},
                "tranDate": "2026-08-03",
                "hours": "1:31",
                "customer": {"id": "10"},
                "caseTaskEvent": {"id": "100"},
                "item": {"id": "501"},
                "memo": "Architecture workshop",
                "custcol_capex_opex": {"id": "11"},
            },
        )

    def test_non_netsuite_entries_are_skipped_but_unmapped_employee_blocks(self):
        entries = [
            {
                "id": "700",
                "task_id": "900",
                "user_id": "missing",
                "date": "2026-08-03",
                "duration": 3600,
            },
            {
                "id": "701",
                "task_id": "901",
                "user_id": "1",
                "date": "2026-08-03",
                "duration": 3600,
            },
        ]
        timecamp_tasks = self.timecamp_tasks + [
            {"task_id": "901", "external_task_id": "jira_123"}
        ]

        prepared, skipped, errors = prepare_timebills(
            entries,
            timecamp_tasks,
            self.source_tasks,
            {},
            self.config,
        )

        self.assertEqual(prepared, [])
        self.assertEqual(skipped["non_netsuite_task"], 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("no NetSuite employee mapping", errors[0])

    def test_project_classification_mode_does_not_write_custom_field(self):
        config = {
            "time_export": {
                "classification": {"mode": "project"},
            }
        }
        entries = [
            {
                "id": "700",
                "task_id": "900",
                "user_id": "1",
                "date": "2026-08-03",
                "duration": 3600,
            }
        ]

        prepared, _skipped, errors = prepare_timebills(
            entries,
            self.timecamp_tasks,
            self.source_tasks,
            {"1": "101"},
            config,
        )

        self.assertEqual(errors, [])
        self.assertNotIn("custcol_capex_opex", prepared[0].payload)

    def test_placeholder_classification_config_blocks_export(self):
        config = {
            "time_export": {
                "classification": {
                    "mode": "field",
                    "field": "REPLACE_WITH_WCG_FIELD",
                    "value_map": {"CAPEX": "REPLACE_WITH_WCG_VALUE"},
                }
            }
        }
        entries = [
            {
                "id": "700",
                "task_id": "900",
                "user_id": "1",
                "date": "2026-08-03",
                "duration": 3600,
            }
        ]

        prepared, _skipped, errors = prepare_timebills(
            entries,
            self.timecamp_tasks,
            self.source_tasks,
            {"1": "101"},
            config,
        )

        self.assertEqual(prepared, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("placeholder", errors[0])


if __name__ == "__main__":
    unittest.main()
