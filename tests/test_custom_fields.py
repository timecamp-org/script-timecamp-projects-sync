import unittest

from src.custom_fields import get_task_custom_fields, sync_custom_fields_to_task


class CustomFieldsTest(unittest.TestCase):
    def test_get_task_custom_fields_normalizes_values(self):
        custom_fields = get_task_custom_fields(
            {
                "custom_fields": {
                    "456": " Client ",
                    789: "1099",
                    "": "ignored",
                    "   ": "ignored",
                    "999": "   ",
                    "1000": None,
                }
            }
        )

        self.assertEqual(
            custom_fields,
            {
                "456": "Client",
                "789": "1099",
            },
        )

    def test_get_task_custom_fields_ignores_invalid_payload(self):
        self.assertEqual(get_task_custom_fields({}), {})
        self.assertEqual(get_task_custom_fields({"custom_fields": ["x"]}), {})

    def test_sync_custom_fields_to_task_assigns_each_value(self):
        calls = []

        class FakeClient:
            def assign_custom_field_to_task(self, task_id, template_id, value):
                calls.append((task_id, template_id, value))

        result = sync_custom_fields_to_task(
            client=FakeClient(),
            timecamp_task_id=123,
            source_task={
                "custom_fields": {
                    "456": "W-2",
                    "789": "Client",
                }
            },
        )

        self.assertEqual(result.assigned, 2)
        self.assertEqual(
            calls,
            [
                (123, "456", "W-2"),
                (123, "789", "Client"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
