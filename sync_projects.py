import os
import json
from dotenv import load_dotenv
from datetime import datetime

from src.mandatory_tags import (
    assign_mandatory_tags_to_task,
    ensure_mandatory_tags,
    get_task_mandatory_tags,
)
from src.timecamp_client import TimeCampClient

# Load environment variables
load_dotenv(override=True)

TIMECAMP_API_TOKEN = os.getenv('TIMECAMP_API_TOKEN')
TIMECAMP_TASK_ID = os.getenv('TIMECAMP_TASK_ID')

def get_timecamp_parent_task_id():
    """Return configured parent task ID, or 0 to create tasks at root level."""
    if not TIMECAMP_TASK_ID or TIMECAMP_TASK_ID.strip() == "0":
        return 0
    return TIMECAMP_TASK_ID

def get_source_external_task_id(task):
    """Return the TimeCamp external_task_id for a source task."""
    if task.get('external_task_id'):
        return str(task['external_task_id'])

    task_id = str(task['task_id'])

    # Monday IDs are already compatible with the native TimeCamp integration.
    if task_id.startswith('monday_'):
        return task_id

    return f"sync_{task_id}"

def load_tasks_from_json(filename='tasks.json'):
    """Load hierarchical tasks from JSON file"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {filename} not found. Generate the hierarchical tasks file first.")
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing {filename}: {e}")
        return []

def sync_hierarchical_tasks_to_timecamp():
    """Main sync function to sync hierarchical task data from tasks.json to TimeCamp"""
    
    # Load hierarchical task data from JSON file
    azure_tasks = load_tasks_from_json()
    if not azure_tasks:
        return

    client = TimeCampClient(TIMECAMP_API_TOKEN)
    
    # Get existing TimeCamp tasks
    timecamp_entries = client.get_tasks()

    # Ensure all mandatory tag lists/tags exist before tasks are assigned to them.
    mandatory_tag_sync = ensure_mandatory_tags(client, azure_tasks)

    source_external_ids = {
        get_source_external_task_id(task)
        for task in azure_tasks
    }
    
    # Create mapping of existing TimeCamp tasks by external_task_id
    timecamp_tasks_map = {}
    for entry in timecamp_entries:
        external_id = entry.get('external_task_id')
        if external_id and (
            external_id.startswith('sync_') or external_id in source_external_ids
        ):
            timecamp_tasks_map[external_id] = entry
    
    print(f"Found {len(timecamp_tasks_map)} existing sync/source tasks in TimeCamp")
    
    # Create mapping of source task_id to TimeCamp task_id for newly created items
    source_to_timecamp_map = {}
    
    # Track which external IDs we encounter (for cleanup later)
    active_external_ids = set()
    
    # Track sync statistics
    created_tasks = 0
    existing_tasks = 0
    archived_tasks = 0
    assigned_mandatory_tags = 0
    tag_assignment_errors = 0
    
    print("Starting hierarchical task synchronization to TimeCamp...")
    
    # Build hierarchy levels dynamically
    def get_hierarchy_level(task, all_tasks):
        """Calculate hierarchy level (0 = top level)"""
        if task['parent_id'] == 0:
            return 0
        
        # Find parent task
        parent_task = next((t for t in all_tasks if t['task_id'] == task['parent_id']), None)
        if not parent_task:
            return 0  # Orphaned task becomes top level
        
        return get_hierarchy_level(parent_task, all_tasks) + 1
    
    # Add hierarchy level to each task and sort by level
    for task in azure_tasks:
        task['_hierarchy_level'] = get_hierarchy_level(task, azure_tasks)
    
    # Sort tasks by hierarchy level (parents before children)
    azure_tasks_sorted = sorted(azure_tasks, key=lambda x: (x['_hierarchy_level'], x['task_id']))
    
    # Process all tasks in hierarchy order
    for task in azure_tasks_sorted:
        external_id = get_source_external_task_id(task)
        active_external_ids.add(external_id)
        
        # Determine parent TimeCamp task ID
        if task['parent_id'] == 0:
            # Top-level task - parent is the configured TimeCamp task
            parent_timecamp_id = get_timecamp_parent_task_id()
        else:
            # Child task - parent should be mapped from source system
            parent_timecamp_id = source_to_timecamp_map.get(task['parent_id'])
            if not parent_timecamp_id:
                # If parent wasn't created successfully, make this a top-level task
                print(f"Warning: Parent task not found for {task['name']}, making it top-level")
                parent_timecamp_id = get_timecamp_parent_task_id()
        
        if external_id not in timecamp_tasks_map:
            # Determine task type for logging
            task_type = "top-level" if task['parent_id'] == 0 else f"level-{task['_hierarchy_level']}"
            print(f"Creating {task_type} task: {task['name']}")
            
            try:
                new_task = client.create_task(
                    name=task['name'],
                    parent_id=parent_timecamp_id,
                    external_task_id=external_id
                )
                source_to_timecamp_map[task['task_id']] = new_task['task_id']
                timecamp_tasks_map[external_id] = new_task
                created_tasks += 1
            except Exception as e:
                print(f"Error creating task {task['name']}: {e}")
                continue
        else:
            existing_task = timecamp_tasks_map[external_id]
            source_to_timecamp_map[task['task_id']] = existing_task['task_id']
            existing_tasks += 1

        if get_task_mandatory_tags(task):
            try:
                assigned_mandatory_tags += assign_mandatory_tags_to_task(
                    client=client,
                    timecamp_task_id=source_to_timecamp_map[task['task_id']],
                    source_task=task,
                    tag_sync_result=mandatory_tag_sync,
                )
            except Exception as e:
                tag_assignment_errors += 1
                print(f"Error assigning mandatory tags to task {task['name']}: {e}")
    
    # Archive TimeCamp tasks that are no longer in source system
    for external_id, timecamp_task in timecamp_tasks_map.items():
        if external_id not in active_external_ids and not timecamp_task.get('archived'):
            print(f"Archiving TimeCamp task: {timecamp_task['name']}")
            try:
                client.archive_task(timecamp_task['task_id'])
                archived_tasks += 1
            except Exception as e:
                print(f"Error archiving task {timecamp_task['name']}: {e}")
    
    print(f"\nSynchronization completed successfully!")
    print(f"- Created: {created_tasks} new tasks")
    print(f"- Existing: {existing_tasks} tasks (no change needed)")
    print(f"- Archived: {archived_tasks} obsolete tasks")
    print(f"- Mandatory tags assigned/updated: {assigned_mandatory_tags}")
    if tag_assignment_errors:
        print(f"- Mandatory tag assignment errors: {tag_assignment_errors}")
    print(f"- Total processed: {len(azure_tasks_sorted)} tasks")

def show_sync_preview():
    """Show a preview of what would be synced without making changes"""
    tasks = load_tasks_from_json()
    if not tasks:
        return
    
    # Calculate hierarchy levels
    def get_hierarchy_level(task, all_tasks):
        if task['parent_id'] == 0:
            return 0
        parent_task = next((t for t in all_tasks if t['task_id'] == task['parent_id']), None)
        if not parent_task:
            return 0
        return get_hierarchy_level(parent_task, all_tasks) + 1
    
    for task in tasks:
        task['_hierarchy_level'] = get_hierarchy_level(task, tasks)
    
    # Group by hierarchy level
    level_counts = {}
    for task in tasks:
        level = task['_hierarchy_level']
        level_counts[level] = level_counts.get(level, 0) + 1
    
    print("Hierarchical Task Sync Preview:")
    print(f"Would sync {len(tasks)} total tasks:")
    for level in sorted(level_counts.keys()):
        print(f"  - Level {level}: {level_counts[level]} tasks")

    tagged_task_count = sum(1 for task in tasks if get_task_mandatory_tags(task))
    if tagged_task_count:
        print(f"  - Tasks with mandatory tags: {tagged_task_count}")
    
    print("\nHierarchy preview:")
    
    def print_task_hierarchy(task_id, tasks, level=0, printed=None):
        if printed is None:
            printed = set()
        
        if task_id in printed:
            return
        printed.add(task_id)
        
        task = next((t for t in tasks if t['task_id'] == task_id), None)
        if not task:
            return
        
        indent = "  " * level
        level_marker = f"[L{task['_hierarchy_level']}]"
        print(f"{indent}{level_marker} {task['name']} (ID: {task['task_id']})")
        
        # Print children
        children = [t for t in tasks if t['parent_id'] == task_id]
        for child in children:
            print_task_hierarchy(child['task_id'], tasks, level + 1, printed)
    
    # Start with top-level tasks
    top_level_tasks = [t for t in tasks if t['parent_id'] == 0]
    for task in top_level_tasks:
        print_task_hierarchy(task['task_id'], tasks)

if __name__ == "__main__":
    print(f"Starting hierarchical task sync to TimeCamp at {datetime.now()}")
    
    # Show preview of what would be synced
    show_sync_preview()
    
    # Run the actual sync (only if credentials are available)
    if TIMECAMP_API_TOKEN:
        sync_hierarchical_tasks_to_timecamp()
    else:
        print("\nTo run actual sync, set TIMECAMP_API_TOKEN in .env file")
    
    print(f"Sync finished at {datetime.now()}") 