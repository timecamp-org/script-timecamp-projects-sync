import requests
import sys
from dotenv import dotenv_values
from datetime import datetime

def read_dotenv() -> dict:
    cfg = dotenv_values(".env")
    return cfg

class Harvest:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = "https://api.harvestapp.com/v2"
        self.headers = {
            "Authorization": f"Bearer {cfg['HARVEST_ACCESS_TOKEN']}",
            "Harvest-Account-Id": cfg['HARVEST_ACCOUNT_ID'],
            "Content-Type": "application/json",
            "User-Agent": "TimeCamp-Harvest-Sync"
        }
        self.user_mapping = self.create_user_mapping()
        self._project_task_cache = {}

    def create_user_mapping(self):
        print("Creating user mapping...")
        harvest_users = self._paginate(f"{self.base_url}/users", "users", {"is_active": "true"})
        timecamp_users = API(self.cfg).get_users()

        harvest_by_email = {u["email"].lower(): u["id"] for u in harvest_users}
        harvest_by_name = {}
        for u in harvest_users:
            full_name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip().lower()
            if full_name:
                harvest_by_name[full_name] = u["id"]

        user_mapping = {}
        for tc_user in timecamp_users:
            # Try email first
            email = tc_user.get("email", "").lower()
            if email and email in harvest_by_email:
                user_mapping[tc_user["user_id"]] = harvest_by_email[email]
                continue
            # Fallback to display name
            display_name = (tc_user.get("display_name") or "").strip().lower()
            if display_name and display_name in harvest_by_name:
                user_mapping[tc_user["user_id"]] = harvest_by_name[display_name]
                print(f"  Matched by name: {display_name}")

        print(f"  Mapped {len(user_mapping)} users")
        return user_mapping

    def _paginate(self, url, key, params=None):
        results = []
        params = params or {}
        params["per_page"] = 100
        while url:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get(key, []))
            url = data.get("links", {}).get("next")
            params = {}
        return results

    def get_harvest_task_for_project(self, project_id: int):
        if project_id in self._project_task_cache:
            return self._project_task_cache[project_id]

        default_task_id = self.cfg.get("HARVEST_DEFAULT_TASK_ID")
        if default_task_id:
            task_id = int(default_task_id)
            self._project_task_cache[project_id] = task_id
            return task_id

        assignments = self._paginate(
            f"{self.base_url}/projects/{project_id}/task_assignments",
            "task_assignments",
            {"is_active": "true"}
        )
        if assignments:
            task_id = assignments[0]["task"]["id"]
            self._project_task_cache[project_id] = task_id
            return task_id

        return None

    def get_existing_tc_ids(self, start_date, end_date):
        """Get TimeCamp entry IDs already exported to Harvest (via external_reference)"""
        entries = self._paginate(
            f"{self.base_url}/time_entries",
            "time_entries",
            {"from": str(start_date), "to": str(end_date)}
        )
        tc_ids = set()
        for entry in entries:
            ext_ref = entry.get("external_reference")
            if ext_ref and ext_ref.get("group_id") == "timecamp":
                tc_ids.add(str(ext_ref["id"]))
        return tc_ids

    def create_time_entry(self, data: dict, tc_tasks: dict) -> None:
        tc_task_id = str(data.get("task_id", ""))
        tc_task = tc_tasks.get(tc_task_id, {})
        external_task_id = tc_task.get("external_task_id", "")

        if not external_task_id.startswith("sync_harvest_project_"):
            return

        try:
            harvest_project_id = int(external_task_id.replace("sync_harvest_project_", ""))
        except ValueError:
            print(f"Ignoring entry {data['id']}: Invalid external_task_id {external_task_id}")
            return

        duration_seconds = int(data.get("duration", 0))
        if duration_seconds <= 0:
            return

        harvest_user_id = self.user_mapping.get(data.get("user_id"))
        if not harvest_user_id:
            print(f"Warning: No matching Harvest user for TimeCamp user ID {data.get('user_id')}")
            return

        harvest_task_id = self.get_harvest_task_for_project(harvest_project_id)
        if not harvest_task_id:
            print(f"Warning: No task assignment for Harvest project {harvest_project_id}")
            return

        hours = round(duration_seconds / 3600, 2)
        description = data.get("description", "")
        notes = f"[tc:{data['id']}] {description}" if description else f"[tc:{data['id']}]"

        body = {
            "project_id": harvest_project_id,
            "task_id": harvest_task_id,
            "spent_date": data["date"],
            "hours": hours,
            "notes": notes,
            "user_id": harvest_user_id,
            "external_reference": {
                "id": str(data["id"]),
                "group_id": "timecamp",
                "permalink": f"https://app.timecamp.com/app#/timesheets/timer/{data['date']}"
            }
        }

        response = requests.post(
            f"{self.base_url}/time_entries",
            headers=self.headers,
            json=body
        )
        response.raise_for_status()

        print(f"Created time entry for user {harvest_user_id}: {hours:.2f}h on project {harvest_project_id} ({data['date']})")

    def handle_time_entries(self, api_conn: 'API') -> None:
        start_date, end_date = self.get_date_range()

        # Get TimeCamp tasks for mapping task_id -> external_task_id
        tc_tasks = api_conn.get_tasks()

        # Get already-exported entry IDs to skip duplicates
        existing_tc_ids = self.get_existing_tc_ids(start_date, end_date)
        print(f"Found {len(existing_tc_ids)} existing entries in Harvest (will skip)")

        entries = api_conn.get_time_entries(start_date, end_date)
        print(f"Found {len(entries)} TimeCamp entries")

        created = 0
        skipped = 0
        for entry in entries:
            if str(entry.get("id", "")) in existing_tc_ids:
                skipped += 1
                continue
            try:
                self.create_time_entry(entry, tc_tasks)
                created += 1
            except Exception as e:
                print(f"Error creating entry {entry.get('id')}: {e}")

        print(f"\nCreated: {created}, Skipped (duplicates): {skipped}")

    def get_date_range(self) -> tuple:
        if len(sys.argv) != 3:
            print("Error: Please provide start and end dates as arguments.")
            print("Usage: python export_time_entries_harvest.py YYYY-MM-DD YYYY-MM-DD")
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

    def get_time_entries(self, start_date, end_date) -> list:
        url = f"{self.url_base}/entries"
        querystring = {"from": f"{start_date}", "to": f"{end_date}"}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN']}"
        }
        response = requests.get(url, headers=headers, params=querystring)
        response.raise_for_status()
        return response.json()

    def get_users(self) -> list:
        url = f"{self.url_base}/users"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN']}"
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def get_tasks(self) -> dict:
        url = f"{self.url_base}/tasks"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.cfg['TIMECAMP_API_TOKEN']}"
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data
        return {str(t['task_id']): t for t in data}

if __name__ == "__main__":
    config = read_dotenv()

    harvest_connection = Harvest(config)
    api_connection = API(config)

    harvest_connection.handle_time_entries(api_connection)

    print("Time entries synced successfully.")
