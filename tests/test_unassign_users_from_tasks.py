import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from helpers.unassign_users_from_tasks import (
    REQUEST_TIMEOUT,
    TIMECAMP_API_URL,
    TimeCampClient,
    build_unassignment_plan,
    parse_args,
    parse_task_ids,
    unassign_users,
)


def task(task_id, parent_id=0, users=None, **extra):
    return {
        "task_id": str(task_id),
        "parent_id": str(parent_id),
        "users": {} if users is None else users,
        **extra,
    }


class UnassignUsersTest(unittest.TestCase):
    def test_cli_accepts_both_task_id_spellings(self):
        for flag in ("--task_ids", "--task-ids"):
            with self.subTest(flag=flag), patch(
                "sys.argv", ["helper", flag, "123, 456,123", "--dry-run"]
            ):
                args = parse_args()
                self.assertEqual(args.task_ids, [123, 456])
                self.assertTrue(args.dry_run)

    def test_rejects_empty_non_integer_and_non_positive_ids(self):
        for value in ("", " ", "123,", "1,,2", "all", "1.5", "0", "-1", "1,0"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_task_ids(value)

    def test_selects_all_descendants_and_deduplicates_overlapping_roots(self):
        tasks = [
            task(1, users={"10": {"user_id": "10", "role_id": "3"}}),
            task(2, 1, users=[{"userId": 20}, {"userId": 20}]),
            task(3, 2, users={"30": {"role_id": 3}}, archived=0),
            task(4),
            task(5, 4, users={"50": {"user_id": 50}}),
        ]
        for selected in ([1, 2, 1], [2, 1]):
            with self.subTest(selected=selected):
                self.assertEqual(
                    build_unassignment_plan(tasks, selected),
                    [(3, [30]), (2, [20]), (1, [10])],
                )

    def test_skips_archived_tasks_without_blocking_active_descendants_or_siblings(self):
        for archived in (1, "1", True, "true", "YES"):
            with self.subTest(archived=archived):
                client = Mock()
                client.get_tasks.return_value = [
                    task(1, users={"10": {}}),
                    task(2, 1, users={"20": {}}, archived=archived),
                    task(3, 2, users={"30": {}}, archived="0"),
                    task(4, 1, users={"40": {}}, archived=0),
                ]

                def unassign(task_id, user_ids):
                    if task_id == 2:
                        raise RuntimeError("403 Forbidden: archived task")

                client.unassign_users_from_task.side_effect = unassign
                with redirect_stdout(io.StringIO()) as output:
                    result = unassign_users(client, [1, 2], dry_run=False)
                self.assertEqual(result, 0)
                self.assertEqual(
                    [call.args for call in client.unassign_users_from_task.call_args_list],
                    [(3, [30]), (4, [40]), (1, [10])],
                )
                self.assertIn("Task 2: skipping archived task", output.getvalue())
                self.assertIn("Removed 3 user assignment(s)", output.getvalue())

    def test_archived_root_without_users_is_skipped_in_dry_run(self):
        client = Mock()
        archived_task = task(1, archived="1")
        del archived_task["users"]
        client.get_tasks.return_value = [archived_task, task(2, 1, users={"20": {}})]
        with redirect_stdout(io.StringIO()) as output:
            result = unassign_users(client, [1], dry_run=True)
        self.assertEqual(result, 0)
        self.assertIn("Task 1: skipping archived task", output.getvalue())
        self.assertIn("Task 2: would unassign [20]", output.getvalue())
        client.unassign_users_from_task.assert_not_called()

    def test_handles_thousands_of_wide_and_deep_tasks_without_recursion(self):
        for deep in (False, True):
            with self.subTest(deep=deep):
                tasks = [task(1)] + [
                    task(i, i - 1 if deep else 1, users={"10": {"user_id": 10}})
                    for i in range(2, 5002)
                ]
                client = Mock()
                client.get_tasks.return_value = tasks
                with redirect_stdout(io.StringIO()):
                    result = unassign_users(client, [1, 2500], dry_run=False)
                self.assertEqual(result, 0)
                client.get_tasks.assert_called_once_with()
                self.assertEqual(client.unassign_users_from_task.call_count, 5000)
                self.assertEqual(
                    {call.args[0] for call in client.unassign_users_from_task.call_args_list},
                    set(range(2, 5002)),
                )

    def test_collects_and_validates_every_assignment_before_mutating(self):
        client = Mock()
        # The valid child would be removed first if collection and writes interleaved.
        client.get_tasks.return_value = [task(1), task(2, 1, users={"10": {}})]
        del client.get_tasks.return_value[0]["users"]
        with redirect_stdout(io.StringIO()) as output:
            result = unassign_users(client, [1], dry_run=False)
        self.assertEqual(result, 1)
        self.assertIn("No assignments changed", output.getvalue())
        client.unassign_users_from_task.assert_not_called()

    def test_unknown_root_and_fetch_failure_abort_before_mutating(self):
        for fetch_error in (False, True):
            with self.subTest(fetch_error=fetch_error):
                client = Mock()
                client.get_tasks.return_value = [task(1, users={"10": {}})]
                if fetch_error:
                    client.get_tasks.side_effect = RuntimeError("unavailable")
                with redirect_stdout(io.StringIO()):
                    result = unassign_users(client, [1, 999], dry_run=False)
                self.assertEqual(result, 1)
                client.unassign_users_from_task.assert_not_called()

    def test_cycle_aborts_before_mutating(self):
        client = Mock()
        client.get_tasks.return_value = [task(1, 2), task(2, 1)]
        with redirect_stdout(io.StringIO()) as output:
            result = unassign_users(client, [1], dry_run=False)
        self.assertEqual(result, 1)
        self.assertIn("Cycle", output.getvalue())
        client.unassign_users_from_task.assert_not_called()

    def test_dry_run_fetches_assignments_but_does_not_write(self):
        client = Mock()
        client.get_tasks.return_value = [task(1, users={"10": {}})]
        with redirect_stdout(io.StringIO()) as output:
            result = unassign_users(client, [1], dry_run=True)
        self.assertEqual(result, 0)
        self.assertIn("would unassign [10]", output.getvalue())
        client.get_tasks.assert_called_once_with()
        client.unassign_users_from_task.assert_not_called()

    def test_removes_children_before_parents_and_stops_on_failure(self):
        client = Mock()
        client.get_tasks.return_value = [
            task(1, users={"10": {}}),
            task(2, 1, users={"20": {}}),
            task(3, 2, users={"30": {}}),
        ]
        client.unassign_users_from_task.side_effect = [None, RuntimeError("forbidden")]
        with redirect_stdout(io.StringIO()) as output:
            result = unassign_users(client, [1], dry_run=False)
        self.assertEqual(result, 1)
        self.assertEqual(
            [call.args for call in client.unassign_users_from_task.call_args_list],
            [(3, [30]), (2, [20])],
        )
        self.assertIn("Stopped after removing 1/3", output.getvalue())

    def test_empty_assignments_make_no_write_requests(self):
        client = Mock()
        client.get_tasks.return_value = [task(1), task(2, 1, users=[])]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(unassign_users(client, [1], dry_run=False), 0)
        client.unassign_users_from_task.assert_not_called()


class TimeCampClientTest(unittest.TestCase):
    def test_fetch_includes_archived_and_handles_both_response_shapes(self):
        for data in ({"1": task(1)}, [task(1)]):
            with self.subTest(data=data):
                client = TimeCampClient("token")
                self.addCleanup(client.session.close)
                with patch.object(client.session, "get") as get:
                    get.return_value.json.return_value = data
                    self.assertEqual(client.get_tasks(), [task(1)])
                get.assert_called_once_with(
                    f"{TIMECAMP_API_URL}/tasks",
                    params={"status": "all"},
                    timeout=REQUEST_TIMEOUT,
                )
                get.return_value.raise_for_status.assert_called_once_with()

    def test_unassign_sends_user_ids_and_accepts_empty_success_body(self):
        client = TimeCampClient("token")
        self.addCleanup(client.session.close)
        with patch.object(client.session, "put") as put:
            client.unassign_users_from_task(123, [10, 20])
        put.assert_called_once_with(
            f"{TIMECAMP_API_URL}/v3/projects/123/unassign",
            json={"userIds": [10, 20]},
            timeout=REQUEST_TIMEOUT,
        )
        put.return_value.raise_for_status.assert_called_once_with()
        put.return_value.json.assert_not_called()

    def test_retries_rate_limits_and_temporary_errors_for_reads_and_writes(self):
        client = TimeCampClient("token")
        self.addCleanup(client.session.close)
        retries = client.session.get_adapter(TIMECAMP_API_URL).max_retries
        self.assertEqual(retries.total, 5)
        self.assertTrue(retries.respect_retry_after_header)
        for method in ("GET", "PUT"):
            for status in (429, 500, 502, 503, 504):
                with self.subTest(method=method, status=status):
                    self.assertTrue(retries.is_retry(method, status))
            self.assertFalse(retries.is_retry(method, 403))


if __name__ == "__main__":
    unittest.main()
