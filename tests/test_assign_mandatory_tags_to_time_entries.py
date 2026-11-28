import io
import unittest
from contextlib import redirect_stdout

from helpers.assign_mandatory_tags_to_time_entries import (
    RequiredTag,
    assign_missing_mandatory_tags,
    build_required_tags_by_timecamp_task_id,
    plan_entry_tag_change,
)

CLIENT_LIST_ID = 10
TEAM_LIST_ID = 20
CLIENT_A = RequiredTag(CLIENT_LIST_ID, 101, "Client", "Client A")
TAG_LISTS_BY_ID = {CLIENT_LIST_ID: "Client", TEAM_LIST_ID: "Team"}


def entry_tag(tag_id, name, tag_list_id=CLIENT_LIST_ID, tag_list_name="Client"):
    return {
        "tagListName": tag_list_name,
        "tagListId": str(tag_list_id),
        "tagId": str(tag_id),
        "name": name,
        "mandatory": "1",
    }


def entry(entry_id, tags, task_id=500, duration=3600, locked="0"):
    return {
        "id": entry_id,
        "task_id": str(task_id),
        "duration": str(duration),
        "locked": locked,
        "tags": tags,
    }


class FakeClient:
    def __init__(self, allowed_tag_ids=None):
        self.calls = []
        self.allowed_tag_ids = allowed_tag_ids

    def add_tags_to_entry(self, entry_id, tag_ids):
        self.calls.append(("add", entry_id, tag_ids))
        # TimeCamp returns only the tag ids that the entry's task allows.
        return [
            str(tag_id)
            for tag_id in tag_ids
            if self.allowed_tag_ids is None or tag_id in self.allowed_tag_ids
        ]

    def remove_tags_from_entry(self, entry_id, tag_ids):
        self.calls.append(("remove", entry_id, tag_ids))


def run_assign(
    entries,
    replace_mismatched,
    dry_run=False,
    client=None,
    replaceable_tag_names=None,
):
    client = client or FakeClient()
    with redirect_stdout(io.StringIO()):
        summary = assign_missing_mandatory_tags(
            client=client,
            entries=entries,
            required_tags_by_task_id={500: [CLIENT_A]},
            tag_lists_by_id=TAG_LISTS_BY_ID,
            dry_run=dry_run,
            replace_mismatched=replace_mismatched,
            replaceable_tag_names=replaceable_tag_names,
        )
    return client, summary


class PlanEntryTagChangeTest(unittest.TestCase):
    def test_add_only_mode_keeps_any_tag_from_the_list(self):
        change = plan_entry_tag_change(
            [entry_tag(102, "All clients")],
            [CLIENT_A],
            TAG_LISTS_BY_ID,
        )

        self.assertTrue(change.is_empty)

    def test_add_only_mode_adds_tag_when_list_is_missing(self):
        change = plan_entry_tag_change(
            [entry_tag(201, "Design", TEAM_LIST_ID, "Team")],
            [CLIENT_A],
            TAG_LISTS_BY_ID,
        )

        self.assertEqual(change.tags_to_add, (CLIENT_A,))
        self.assertEqual(change.tag_ids_to_remove, ())

    def test_replace_mode_swaps_only_tags_from_the_same_list(self):
        change = plan_entry_tag_change(
            [
                entry_tag(102, "All clients"),
                entry_tag(201, "Design", TEAM_LIST_ID, "Team"),
            ],
            [CLIENT_A],
            TAG_LISTS_BY_ID,
            replace_mismatched=True,
        )

        self.assertEqual(change.tags_to_add, (CLIENT_A,))
        self.assertEqual(change.tag_ids_to_remove, (102,))
        self.assertEqual(change.removed_tag_names, ("All clients",))

    def test_replace_mode_keeps_entry_with_the_required_tag(self):
        change = plan_entry_tag_change(
            [entry_tag(101, "Client A")],
            [CLIENT_A],
            TAG_LISTS_BY_ID,
            replace_mismatched=True,
        )

        self.assertTrue(change.is_empty)


class AssignMandatoryTagsTest(unittest.TestCase):
    def test_replace_adds_new_tag_before_removing_old_one(self):
        client, summary = run_assign(
            [entry(1, [entry_tag(102, "All clients")])],
            replace_mismatched=True,
        )

        self.assertEqual(client.calls, [("add", 1, [101]), ("remove", 1, [102])])
        self.assertEqual(summary.replaced, 1)
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.changes[("All clients", "Client A")], 1)
        self.assertEqual(summary.change_seconds[("All clients", "Client A")], 3600)

    def test_replaceable_tags_limit_which_tags_are_replaced(self):
        client, summary = run_assign(
            [
                entry(1, [entry_tag(102, "All clients")]),
                entry(2, [entry_tag(102, "All clients"), entry_tag(103, "Client B")]),
                entry(3, [entry_tag(103, "Client B")]),
                entry(4, []),
            ],
            replace_mismatched=True,
            replaceable_tag_names=["all CLIENTS"],
        )

        self.assertEqual(
            client.calls,
            [("add", 1, [101]), ("remove", 1, [102]), ("add", 4, [101])],
        )
        self.assertEqual(summary.replaced, 1)
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.kept_other_tags, 2)

    def test_old_tag_is_kept_when_timecamp_does_not_add_the_new_one(self):
        client, summary = run_assign(
            [entry(1, [entry_tag(102, "All clients")])],
            replace_mismatched=True,
            client=FakeClient(allowed_tag_ids=set()),
        )

        self.assertEqual(client.calls, [("add", 1, [101])])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.replaced, 0)

    def test_dry_run_and_locked_entries_make_no_calls(self):
        entries = [
            entry(1, [entry_tag(102, "All clients")]),
            entry(2, [entry_tag(102, "All clients")], locked="1"),
            entry(3, []),
        ]

        client, summary = run_assign(entries, replace_mismatched=True, dry_run=True)

        self.assertEqual(client.calls, [])
        self.assertEqual(summary.replaced, 1)
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.locked, 1)

    def test_add_only_mode_does_not_touch_other_tags(self):
        client, summary = run_assign(
            [entry(1, [entry_tag(102, "All clients")]), entry(2, [])],
            replace_mismatched=False,
        )

        self.assertEqual(client.calls, [("add", 2, [101])])
        self.assertEqual(summary.already_tagged, 1)
        self.assertEqual(summary.updated, 1)


class BuildRequiredTagsTest(unittest.TestCase):
    def test_all_timecamp_tasks_with_the_same_external_id_get_required_tags(self):
        source_tasks = [
            {
                "task_id": "monday_1",
                "external_task_id": "monday_1",
                "parent_id": 0,
                "meandatory_tags": {"Client": ["Client A"]},
            },
            {"task_id": "monday_2", "external_task_id": "monday_2", "parent_id": "monday_1"},
        ]
        timecamp_tasks = [
            {"task_id": "500", "external_task_id": "monday_2"},
            {"task_id": "501", "external_task_id": "monday_2", "archived": "1"},
            {"task_id": "600", "external_task_id": "monday_1"},
        ]

        with redirect_stdout(io.StringIO()):
            required = build_required_tags_by_timecamp_task_id(
                source_tasks=source_tasks,
                timecamp_tasks=timecamp_tasks,
                tag_definitions={("client", "client a"): (CLIENT_LIST_ID, 101)},
            )

        self.assertEqual(set(required), {500, 501, 600})
        self.assertEqual(required[501], [CLIENT_A])


if __name__ == "__main__":
    unittest.main()
