from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class TaskCustomFieldSyncResult:
    assigned: int = 0


def get_task_custom_fields(task: Dict[str, Any]) -> Dict[str, str]:
    """Return TimeCamp custom field template id -> value from a source task."""
    raw_value = task.get("custom_fields")
    if not isinstance(raw_value, dict):
        return {}

    custom_fields: Dict[str, str] = {}
    for template_id, field_value in raw_value.items():
        mapped_id = str(template_id).strip()
        if not mapped_id:
            continue

        if field_value is None:
            continue

        value = str(field_value).strip()
        if not value:
            continue

        custom_fields[mapped_id] = value

    return custom_fields


def sync_custom_fields_to_task(
    client: Any,
    timecamp_task_id: Any,
    source_task: Dict[str, Any],
) -> TaskCustomFieldSyncResult:
    """Assign source custom_fields values onto a TimeCamp task."""
    custom_fields = get_task_custom_fields(source_task)
    assigned = 0

    for template_id, value in custom_fields.items():
        client.assign_custom_field_to_task(
            task_id=timecamp_task_id,
            template_id=template_id,
            value=value,
        )
        assigned += 1

    return TaskCustomFieldSyncResult(assigned=assigned)
