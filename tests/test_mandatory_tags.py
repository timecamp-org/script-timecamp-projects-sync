import unittest

from src.mandatory_tags import (
    MandatoryTagSyncResult,
    TagDefinition,
    sync_mandatory_tags_to_task,
)


class MandatoryTagsTest(unittest.TestCase):
    def test_sync_uses_provided_current_assignments_without_fetching_task_tags(self):
        class FakeClient:
            def get_task_tags(self, task_id):
                raise AssertionError("should use preloaded task tag assignments")

        result = sync_mandatory_tags_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "mandatory_tags": {
                    "Client": ["Acme"],
                },
            },
            tag_sync_result=MandatoryTagSyncResult(
                tags={("client", "acme"): TagDefinition(tag_list_id=1, tag_id=10)}
            ),
            current_assignments={
                "1": {
                    "id": "1",
                    "inherit": 0,
                    "hasAssignedTags": True,
                    "tags": [
                        {
                            "id": "10",
                            "mandatory": "1",
                            "inherit": 0,
                        }
                    ],
                }
            },
        )

        self.assertEqual(result.assigned, 0)
        self.assertFalse(result.skipped_due_to_limit)


if __name__ == "__main__":
    unittest.main()
