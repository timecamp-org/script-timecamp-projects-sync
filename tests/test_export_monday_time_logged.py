import unittest
from collections import Counter

from export_monday_time_logged import (
    aggregate_entries_by_source_task,
    build_export_rows,
    current_item_column_hours,
    find_numbers_column_by_title,
    get_ignored_main_row_time,
    prepare_updates,
)


class ExportMondayTimeLoggedTest(unittest.TestCase):
    def test_default_exports_only_subitems_and_ignores_direct_main_time(self):
        source_by_id = {
            "monday_1": {
                "task_id": "monday_1",
                "parent_id": 0,
                "name": "Traffic Studio",
            },
            "monday_1_group_projects": {
                "task_id": "monday_1_group_projects",
                "parent_id": "monday_1",
                "name": "Projects",
            },
            "monday_100": {
                "task_id": "monday_100",
                "parent_id": "monday_1_group_projects",
                "name": "Client A",
            },
            "monday_200": {
                "task_id": "monday_200",
                "parent_id": "monday_100",
                "name": "Client A :: Task 1",
            },
            "monday_300": {
                "task_id": "monday_300",
                "parent_id": "monday_100",
                "name": "Client A :: Task 2",
            },
        }
        timecamp_tasks = [
            {"task_id": "10", "external_task_id": "monday_100"},
            {"task_id": "20", "external_task_id": "monday_200"},
            {"task_id": "30", "external_task_id": "monday_300"},
            {"task_id": "40", "external_task_id": None},
        ]
        entries = [
            {"task_id": "10", "duration": "1800"},
            {"task_id": "20", "duration": "3600"},
            {"task_id": "30", "duration": "7200"},
            {"task_id": "40", "duration": "600"},
            {"task_id": "missing", "duration": "600"},
        ]

        seconds_by_source, skipped = aggregate_entries_by_source_task(
            entries=entries,
            timecamp_tasks=timecamp_tasks,
            source_by_id=source_by_id,
        )
        rows = build_export_rows(seconds_by_source, source_by_id)

        self.assertEqual(
            seconds_by_source,
            {
                "monday_100": 1800,
                "monday_200": 3600,
                "monday_300": 7200,
            },
        )
        self.assertEqual(
            skipped,
            Counter({
                "no_monday_external_task_id": 1,
                "missing_timecamp_task": 1,
            }),
        )
        self.assertEqual(
            {(row.source_task_id, row.kind, row.seconds) for row in rows},
            {
                ("monday_200", "subitem", 3600),
                ("monday_300", "subitem", 7200),
            },
        )
        self.assertEqual(
            get_ignored_main_row_time(seconds_by_source, source_by_id),
            (1, 1800),
        )

    def test_include_main_rows_adds_direct_main_time_and_subitem_rollup(self):
        source_by_id = {
            "monday_1_group_projects": {
                "task_id": "monday_1_group_projects",
                "parent_id": "monday_1",
            },
            "monday_100": {
                "task_id": "monday_100",
                "parent_id": "monday_1_group_projects",
            },
            "monday_200": {
                "task_id": "monday_200",
                "parent_id": "monday_100",
            },
        }
        rows = build_export_rows(
            {"monday_100": 1800, "monday_200": 3600},
            source_by_id,
            include_main_rows=True,
        )

        self.assertEqual(
            {(row.source_task_id, row.kind, row.seconds) for row in rows},
            {
                ("monday_100", "main", 5400),
                ("monday_200", "subitem", 3600),
            },
        )

    def test_rejects_missing_duplicate_and_wrong_type_columns(self):
        base_board = {
            "id": "1",
            "name": "Traffic Studio",
            "columns": [
                {"id": "numbers", "title": "Time Logged", "type": "numbers"},
            ],
        }
        self.assertEqual(
            find_numbers_column_by_title(base_board, "Time Logged")["id"],
            "numbers",
        )

        with self.assertRaisesRegex(ValueError, "has no 'Missing' column"):
            find_numbers_column_by_title(base_board, "Missing")

        with self.assertRaisesRegex(ValueError, "not a numbers column"):
            find_numbers_column_by_title(
                {
                    "id": "1",
                    "name": "Traffic Studio",
                    "columns": [
                        {"id": "text", "title": "Time Logged", "type": "text"},
                    ],
                },
                "Time Logged",
            )

        with self.assertRaisesRegex(ValueError, "multiple"):
            find_numbers_column_by_title(
                {
                    "id": "1",
                    "name": "Traffic Studio",
                    "columns": [
                        {"id": "a", "title": "Time Logged", "type": "numbers"},
                        {"id": "b", "title": "Time Logged", "type": "numbers"},
                    ],
                },
                "Time Logged",
            )

    def test_prepare_updates_skips_unchanged_rows(self):
        rows = build_export_rows(
            {"monday_200": 3600},
            {
                "monday_100": {
                    "task_id": "monday_100",
                    "parent_id": "monday_1_group_projects",
                },
                "monday_200": {
                    "task_id": "monday_200",
                    "parent_id": "monday_100",
                },
            },
            include_main_rows=True,
        )
        monday_items = [
            {
                "id": "100",
                "name": "Client A",
                "board": {"id": "board-main", "name": "Main"},
                "column_values": [
                    {"id": "main_time", "text": "", "value": None},
                ],
            },
            {
                "id": "200",
                "name": "Client A :: Task 1",
                "board": {"id": "board-sub", "name": "Subitems"},
                "column_values": [
                    {"id": "sub_time", "text": "1", "value": "1"},
                ],
            },
        ]
        board_columns = [
            {
                "id": "board-main",
                "name": "Main",
                "columns": [
                    {"id": "main_time", "title": "Time Logged", "type": "numbers"},
                ],
            },
            {
                "id": "board-sub",
                "name": "Subitems",
                "columns": [
                    {"id": "sub_time", "title": "Time Logged", "type": "numbers"},
                ],
            },
        ]

        updates, missing_rows, skipped = prepare_updates(
            rows=rows,
            monday_items=monday_items,
            board_columns=board_columns,
            column_title="Time Logged",
        )

        self.assertEqual(len(missing_rows), 0)
        self.assertEqual(skipped, Counter({"unchanged": 1}))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].item_name, "Client A")
        self.assertEqual(updates[0].value, "1")

    def test_current_item_column_hours_parses_monday_number_shapes(self):
        self.assertEqual(
            current_item_column_hours(
                {"column_values": [{"id": "time", "text": "1.25", "value": None}]},
                "time",
            ),
            1.25,
        )
        self.assertEqual(
            current_item_column_hours(
                {
                    "column_values": [
                        {"id": "time", "text": "", "value": '{"number": "2.5"}'}
                    ]
                },
                "time",
            ),
            2.5,
        )
        self.assertIsNone(
            current_item_column_hours(
                {"column_values": [{"id": "time", "text": "", "value": None}]},
                "time",
            )
        )


if __name__ == "__main__":
    unittest.main()
