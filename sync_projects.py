import os
from dotenv import load_dotenv
from datetime import datetime
from redminelib import Redmine
import requests

# Load environment variables
load_dotenv()

REDMINE_URL = os.getenv('REDMINE_URL')
REDMINE_API_KEY = os.getenv('REDMINE_API_KEY')
TIMECAMP_API_TOKEN = os.getenv('TIMECAMP_API_TOKEN2')
TIMECAMP_TASK_ID = os.getenv('TIMECAMP_TASK_ID')

def get_redmine_projects():
    redmine = Redmine(REDMINE_URL, key=REDMINE_API_KEY)
    all_projects = redmine.project.all()
    active_projects = [project for project in all_projects if project.status == 1]  # Assuming status 1 means active
    return active_projects

def get_timecamp_projects():
    url = "https://app.timecamp.com/third_party/api/tasks"
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {TIMECAMP_API_TOKEN}'}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    # Check if the response is a dictionary
    if isinstance(data, dict):
        # Extract the projects from the dictionary
        return list(data.values())
    elif isinstance(data, list):
        # If it's already a list, return it as is
        return data
    else:
        print(f"Unexpected response format: {type(data)}")
        return []

def create_timecamp_project(name, project_id):
    url = "https://app.timecamp.com/third_party/api/tasks"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {TIMECAMP_API_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {
        'name': name,
        'parent_id': TIMECAMP_TASK_ID,
        'external_task_id': f'redmine_{project_id}'
    }
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()

def create_timecamp_task(name, project_id, task_id):
    url = "https://app.timecamp.com/third_party/api/tasks"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {TIMECAMP_API_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {
        'name': name,
        'parent_id': project_id,
        'external_task_id': f'redmine_task_{task_id}'
    }
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()

def archive_timecamp_project(task_id):
    url = f"https://app.timecamp.com/third_party/api/tasks"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {TIMECAMP_API_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {
        'archived': 1,
        'task_id': task_id
    }
    response = requests.put(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()


def get_redmine_tasks(project_id):
    redmine = Redmine(REDMINE_URL, key=REDMINE_API_KEY)
    return redmine.issue.filter(project_id=project_id, status_id='open')

def sync_projects_and_tasks():
    redmine_projects = get_redmine_projects()
    timecamp_entries = get_timecamp_projects()

    timecamp_projects = {}
    timecamp_tasks = {}

    for entry in timecamp_entries:
        external_id = entry.get('external_task_id')
        if external_id:
            if external_id.startswith('redmine_task_'):
                timecamp_tasks[external_id] = entry
            elif external_id.startswith('redmine_'):
                timecamp_projects[external_id] = entry

    open_redmine_task_ids = set()

    for redmine_project in redmine_projects:
        external_project_id = f'redmine_{redmine_project.id}'
        if external_project_id not in timecamp_projects:
            print(f"Creating new TimeCamp project: {redmine_project.name}")
            new_project = create_timecamp_project(redmine_project.name, redmine_project.id)
            timecamp_project_id = new_project['task_id']
        else:
            print(f"Project already exists in TimeCamp: {redmine_project.name}")
            timecamp_project_id = timecamp_projects[external_project_id]['task_id']

        # Sync tasks for this project
        redmine_tasks = get_redmine_tasks(redmine_project.id)
        for redmine_task in redmine_tasks:
            external_task_id = f'redmine_task_{redmine_task.id}'
            open_redmine_task_ids.add(external_task_id)
            if external_task_id not in timecamp_tasks:
                print(f"Creating new TimeCamp task: {redmine_task.subject}")
                create_timecamp_task(redmine_task.subject, timecamp_project_id, redmine_task.id)
            else:
                print(f"Task already exists in TimeCamp: {redmine_task.subject}")

    # print(open_redmine_task_ids)
    # Archive TimeCamp tasks that are not open in Redmine
    for timecamp_task in timecamp_tasks.values():
        # print(timecamp_task)
        if (timecamp_task['external_task_id'].startswith(f'redmine_task_') and
            timecamp_task['external_task_id'] not in open_redmine_task_ids and
            not timecamp_task.get('archived')):
            print(f"Archiving TimeCamp task: {timecamp_task['name']}")
            archive_timecamp_project(timecamp_task['task_id'])

    # Archive TimeCamp projects that don't exist in Redmine
    active_redmine_project_ids = {f'redmine_{project.id}' for project in redmine_projects}
    for timecamp_project in timecamp_projects.values():
        if timecamp_project['external_task_id'] not in active_redmine_project_ids and not timecamp_project.get('archived'):
            print(f"Archiving TimeCamp project: {timecamp_project['name']}")
            archive_timecamp_project(timecamp_project['task_id'])

    print("Synchronization complete.")

if __name__ == "__main__":
    print(f"Starting synchronization at {datetime.now()}")
    sync_projects_and_tasks()
    print(f"Synchronization finished at {datetime.now()}")
