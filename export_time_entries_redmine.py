import requests
import sys
from dotenv import dotenv_values
from datetime import datetime, timedelta
from redminelib import Redmine as RedmineLib

def read_dotenv() -> dict:
    cfg = dotenv_values(".env")
    return cfg

class Redmine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.redmine = RedmineLib(self.cfg['REDMINE_URL'], key=self.cfg['REDMINE_API_KEY'])
        self.user_mapping = self.create_user_mapping()

    def create_user_mapping(self):
        print("Creating user mapping...")
        redmine_users = self.redmine.user.all()
        timecamp_users = API(self.cfg).get_users()

        user_mapping = {}
        for timecamp_user in timecamp_users:
            for redmine_user in redmine_users:
                # print(f"{redmine_user.mail} {redmine_user.id}")
                if timecamp_user['email'].lower() == redmine_user.mail.lower():
                    user_mapping[timecamp_user['user_id']] = redmine_user.id
                    break
        return user_mapping

    def create_time_entry(self, data: dict) -> None:
        if 'addons_external_id' not in data or not data['addons_external_id'].startswith('redmine_'):
            print(f"Ignoring entry {data['id']}: No valid addons_external_id")
            return

        try:
            external_id, id_type = self.extract_id_from_addons_external_id(data['addons_external_id'])
        except ValueError as e:
            print(f"Ignoring entry {data['id']}: {str(e)}")
            return

        duration_seconds = int(data['duration'])
        
        redmine_user_id = self.user_mapping.get(data['user_id'])
        if not redmine_user_id:
            print(f"Warning: No matching Redmine user found for TimeCamp user ID {data['user_id']}")
            return
        
        comment = f"[tc:{data['id']}] {data['description']}"

        time_entry_data = {
            'spent_on': data['date'],
            'hours': duration_seconds / 3600,  # Convert seconds to hours
            'activity_id': self.cfg['REDMINE_ACTIVITY_ID'],
            'comments': comment,
            'user_id': redmine_user_id
        }

        if id_type == 'issue':
            time_entry_data['issue_id'] = external_id
        elif id_type == 'project':
            time_entry_data['project_id'] = external_id

        self.redmine.time_entry.create(**time_entry_data)

        print(f"Created time entry for user {redmine_user_id} on {id_type} {external_id} on {data['date']}")

    # def update_time_entry(self, time_entry_id: int, data: dict) -> None:
    #     self.redmine.time_entry.update(
    #         time_entry_id,
    #         issue_id=data['task_id'],
    #         spent_on=data['date'],
    #         hours=data['duration'] / 3600,  # Convert seconds to hours
    #         activity_id=self.cfg['REDMINE_ACTIVITY_ID'],
    #         comments=data['description']
    #     )

    # def delete_time_entry(self, time_entry_id: int) -> None:
    #     self.redmine.time_entry.delete(time_entry_id)

    def extract_id_from_addons_external_id(self, addons_external_id: str) -> tuple:
        if addons_external_id.startswith('redmine_task_'):
            return int(addons_external_id.split('_')[-1]), 'issue'
        elif addons_external_id.startswith('redmine_'):
            return int(addons_external_id.split('_')[-1]), 'project'
        else:
            raise ValueError(f"Invalid addons_external_id format: {addons_external_id}")

    def handle_time_entries(self, api_conn: 'API') -> None:
        start_date, end_date = self.get_date_range()

        entries = api_conn.get_time_entries(start_date, end_date)
        # entry_changes = api_conn.get_time_entry_changes(start_date, end_date)
        # entry_deletions = api_conn.get_time_entry_deletions(start_date, end_date)

        # return

        for entry in entries:
            self.create_time_entry(entry)

        # for change in entry_changes:
        #     self.update_time_entry(change['entry_id'], change)

        # for deletion in entry_deletions:
        #     self.delete_time_entry(deletion['entry_id'])

    def get_date_range(self) -> tuple:
        if len(sys.argv) != 3:
            print("Error: Please provide start and end dates as arguments.")
            print("Usage: python export_time_entries.py YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)

        try:
            start_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
            end_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        except ValueError:
            print("Error: Invalid date format. Please use YYYY-MM-DD.")
            sys.exit(1)

        if start_date > end_date:
            print("Error: Start date must be before or equal to end date.")
            sys.exit(1)

        return start_date, end_date

class API:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url_base = "https://app.timecamp.com/third_party/api"

    def get_time_entries(self, start_date: str or datetime.date, end_date: str or datetime.date) -> list:
        url = f"{self.url_base}/entries"
        querystring = {
            "from": f"{start_date}",
            "to": f"{end_date}"
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN2']}"
        }
        response = requests.get(url, headers=headers, params=querystring)
        entries = response.json()
        # print(entries)
        # for entry in entries:
        #     if 'addons_external_id' in entry:
        #         entry['task_id'] = extract_id_from_external_task_id(entry['addons_external_id'])
        #         print(entry['task_id'])
        return entries

    # def get_time_entry_changes(self, start_date: str or datetime.date, end_date: str or datetime.date) -> list:
    #     url = f"{self.url_base}/entries_changes"

    #     querystring = {"from": f"{start_date}", "to": f"{end_date}"}

    #     headers = {
    #         "Accept": "application/json",
    #         "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN2']}"
    #     }

    #     response = requests.get(url, headers=headers, params=querystring)
    #     return response.json()

    # def get_time_entry_deletions(self, start_date: str or datetime.date, end_date: str or datetime.date) -> list:
    #     url = f"{self.url_base}/entries_deletions"

    #     querystring = {"from": f"{start_date}", "to": f"{end_date}"}

    #     headers = {
    #         "Accept": "application/json",
    #         "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN2']}"
    #     }

    #     response = requests.get(url, headers=headers, params=querystring)
    #     return response.json()

    def get_users(self) -> list:
        url = f"{self.url_base}/users"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN2']}"
        }

        response = requests.get(url, headers=headers)
        # print(response.json())
        return response.json()

    # def get_tasks(self) -> dict:
    #     url = f"{self.url_base}/tasks"

    #     querystring = {"status": "all"}

    #     headers = {
    #         "Accept": "application/json",
    #         "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN2']}"
    #     }

    #     response = requests.get(url, headers=headers, params=querystring)
    #     return response.json()

if __name__ == "__main__":
    config = read_dotenv()

    redmine_connection = Redmine(config)
    api_connection = API(config)

    redmine_connection.handle_time_entries(api_connection)

    print("Time entries synced successfully.")