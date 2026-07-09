import unittest

from src.assigned_users import AssignedUserSyncResult, sync_users_to_task


class AssignedUsersTest(unittest.TestCase):
    def test_strict_sync_unassigns_direct_users_missing_from_source(self):
        calls = []

        class FakeClient:
            def assign_users_to_task(self, task_id, user_ids, role_id):
                calls.append(("assign", task_id, user_ids, role_id))

            def get_project_assigned_users(self, task_id):
                calls.append(("list", task_id))
                return [
                    {"userId": 10, "taskId": 123},
                    {"userId": 20, "taskId": 123},
                    {"userId": 30, "taskId": 999},
                    {"userId": 40, "taskId": None},
                ]

            def unassign_users_from_task(self, task_id, user_ids):
                calls.append(("unassign", task_id, user_ids))

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "name": "Project",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "keep@example.com",
                        "username": "Keep User",
                    }
                },
            },
            user_sync_result=AssignedUserSyncResult(
                users_by_email={"keep@example.com": 10},
                users_by_username={},
            ),
            strict=True,
        )

        self.assertEqual(result.assigned, 1)
        self.assertEqual(result.unassigned, 1)
        self.assertEqual(
            calls,
            [
                ("assign", 123, [10], 3),
                ("list", 123),
                ("unassign", 123, [20]),
            ],
        )

    def test_strict_sync_uses_current_task_users_without_fetching_assigned_users(self):
        calls = []

        class FakeClient:
            def assign_users_to_task(self, task_id, user_ids, role_id):
                calls.append(("assign", task_id, user_ids, role_id))

            def get_project_assigned_users(self, task_id):
                raise AssertionError("should use users from /tasks")

            def unassign_users_from_task(self, task_id, user_ids):
                calls.append(("unassign", task_id, user_ids))

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "name": "Project",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "keep@example.com",
                        "username": "Keep User",
                    }
                },
            },
            user_sync_result=AssignedUserSyncResult(
                users_by_email={"keep@example.com": 10},
                users_by_username={},
            ),
            strict=True,
            current_assigned_users={
                "10": {"user_id": "10", "role_id": "3"},
                "20": {"user_id": "20", "role_id": "3"},
            },
        )

        self.assertEqual(result.assigned, 0)
        self.assertEqual(result.unassigned, 1)
        self.assertEqual(
            calls,
            [
                ("unassign", 123, [20]),
            ],
        )

    def test_sync_does_not_assign_when_current_task_users_already_match_source(self):
        class FakeClient:
            def assign_users_to_task(self, task_id, user_ids, role_id):
                raise AssertionError("should not assign users already on task")

            def get_project_assigned_users(self, task_id):
                raise AssertionError("should use users from /tasks")

            def unassign_users_from_task(self, task_id, user_ids):
                raise AssertionError("should not unassign matching users")

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "name": "Project",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "keep@example.com",
                        "username": "Keep User",
                    }
                },
            },
            user_sync_result=AssignedUserSyncResult(
                users_by_email={"keep@example.com": 10},
                users_by_username={},
            ),
            strict=True,
            current_assigned_users={
                "10": {"user_id": "10", "role_id": "3"},
            },
        )

        self.assertEqual(result.assigned, 0)
        self.assertEqual(result.unassigned, 0)

    def test_sync_assigns_only_users_missing_from_current_task_users(self):
        calls = []

        class FakeClient:
            def assign_users_to_task(self, task_id, user_ids, role_id):
                calls.append(("assign", task_id, user_ids, role_id))

            def get_project_assigned_users(self, task_id):
                raise AssertionError("should use users from /tasks")

            def unassign_users_from_task(self, task_id, user_ids):
                calls.append(("unassign", task_id, user_ids))

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "name": "Project",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "existing@example.com",
                        "username": "Existing User",
                    },
                    "monday_user_2": {
                        "email": "missing@example.com",
                        "username": "Missing User",
                    },
                },
            },
            user_sync_result=AssignedUserSyncResult(
                users_by_email={
                    "existing@example.com": 10,
                    "missing@example.com": 20,
                },
                users_by_username={},
            ),
            strict=True,
            current_assigned_users={
                "10": {"user_id": "10", "role_id": "3"},
            },
        )

        self.assertEqual(result.assigned, 1)
        self.assertEqual(result.unassigned, 0)
        self.assertEqual(calls, [("assign", 123, [20], 3)])

    def test_strict_sync_unassigns_all_direct_users_when_source_has_no_users(self):
        calls = []

        class FakeClient:
            def get_project_assigned_users(self, task_id):
                calls.append(("list", task_id))
                return [
                    {"userId": 20, "taskId": 123},
                    {"userId": 30, "taskId": 999},
                ]

            def unassign_users_from_task(self, task_id, user_ids):
                calls.append(("unassign", task_id, user_ids))

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={"name": "Project"},
            user_sync_result=AssignedUserSyncResult(
                users_by_email={},
                users_by_username={},
            ),
            strict=True,
        )

        self.assertEqual(result.assigned, 0)
        self.assertEqual(result.unassigned, 1)
        self.assertEqual(
            calls,
            [
                ("list", 123),
                ("unassign", 123, [20]),
            ],
        )

    def test_strict_sync_does_not_unassign_when_source_user_cannot_be_resolved(self):
        calls = []

        class FakeClient:
            def get_project_assigned_users(self, task_id):
                calls.append(("list", task_id))
                return [{"userId": 20, "taskId": 123}]

            def unassign_users_from_task(self, task_id, user_ids):
                calls.append(("unassign", task_id, user_ids))

        result = sync_users_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "name": "Project",
                "assigned_users": {
                    "monday_user_1": {
                        "email": "missing@example.com",
                        "username": "Missing User",
                    }
                },
            },
            user_sync_result=AssignedUserSyncResult(
                users_by_email={},
                users_by_username={},
            ),
            strict=True,
        )

        self.assertEqual(result.assigned, 0)
        self.assertEqual(result.unassigned, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
