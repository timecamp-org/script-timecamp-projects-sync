import unittest
from unittest.mock import patch

import sync_projects


class SyncProjectsCliTest(unittest.TestCase):
    def test_main_uses_input_file_for_preview_and_sync(self):
        with (
            patch.object(sync_projects, "TIMECAMP_API_TOKEN", "token"),
            patch.object(sync_projects, "get_enabled_sync_actions", return_value={"tasks"}),
            patch.object(sync_projects, "print_sync_action_plan"),
            patch.object(sync_projects, "show_sync_preview") as show_sync_preview,
            patch.object(sync_projects, "sync_hierarchical_tasks_to_timecamp") as sync_tasks,
        ):
            sync_projects.main(["--input", "task_tc.json"])

        show_sync_preview.assert_called_once_with("task_tc.json")
        sync_tasks.assert_called_once_with({"tasks"}, "task_tc.json")


if __name__ == "__main__":
    unittest.main()
