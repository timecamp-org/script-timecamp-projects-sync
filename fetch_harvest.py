import os
import json
from datetime import datetime
from typing import Dict, List, Any
from dotenv import load_dotenv
import requests

# Load environment variables
load_dotenv(override=True)


class HarvestClient:
    """Client for interacting with Harvest V2 API"""

    def __init__(self, access_token: str, account_id: str):
        self.base_url = "https://api.harvestapp.com/v2"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Harvest-Account-Id": account_id,
            "Content-Type": "application/json",
            "User-Agent": "TimeCamp-Harvest-Sync"
        }

    def _paginate(self, url: str, key: str, params: dict = None) -> List[Dict[str, Any]]:
        """Generic paginated GET request"""
        results = []
        params = params or {}
        params["per_page"] = 100

        while url:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get(key, []))
            url = data.get("links", {}).get("next")
            params = {}  # pagination URL includes params
        return results

    def get_clients(self) -> List[Dict[str, Any]]:
        """Get all active clients from Harvest"""
        return self._paginate(f"{self.base_url}/clients", "clients", {"is_active": "true"})

    def get_projects(self) -> List[Dict[str, Any]]:
        """Get all active projects from Harvest"""
        return self._paginate(f"{self.base_url}/projects", "projects", {"is_active": "true"})



class HarvestFetcher:
    """Fetches clients and projects from Harvest and outputs tasks.json format"""

    def __init__(self):
        access_token = os.getenv("HARVEST_ACCESS_TOKEN")
        account_id = os.getenv("HARVEST_ACCOUNT_ID")

        if not access_token or not account_id:
            raise ValueError("HARVEST_ACCESS_TOKEN and HARVEST_ACCOUNT_ID must be set in .env")

        self.client = HarvestClient(access_token, account_id)

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """Fetch clients and projects, return flattened task structure"""
        flattened_data = []

        print("Fetching clients from Harvest...")
        clients = self.client.get_clients()
        print(f"  Found {len(clients)} active clients")

        print("Fetching projects from Harvest...")
        projects = self.client.get_projects()
        print(f"  Found {len(projects)} active projects")

        # Add clients as top-level items
        client_ids = {c["id"] for c in clients}
        for client in clients:
            flattened_data.append({
                "name": client["name"],
                "task_id": f"harvest_client_{client['id']}",
                "parent_id": 0
            })

        # Add projects under their clients
        for project in projects:
            project_client = project.get("client")
            if project_client and project_client["id"] in client_ids:
                parent_id = f"harvest_client_{project_client['id']}"
            else:
                parent_id = 0

            flattened_data.append({
                "name": project["name"],
                "task_id": f"harvest_project_{project['id']}",
                "parent_id": parent_id
            })

        return flattened_data

    def save_to_json(self, data: List[Dict[str, Any]], filename: str = "tasks.json") -> str:
        """Save data to JSON file"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return filename


def main():
    """Fetch clients and projects from Harvest -> tasks.json"""
    print("Starting Harvest data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = HarvestFetcher()
    data = fetcher.fetch_all_data()
    filename = fetcher.save_to_json(data)

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")

    clients = len([d for d in data if d["parent_id"] == 0])
    projects = len(data) - clients

    print(f"\nSummary:")
    print(f"  Clients: {clients}")
    print(f"  Projects: {projects}")
    print(f"  Total items: {len(data)}")

    if data:
        print(f"\nStructure preview:")
        for item in data[:20]:
            indent = "" if item["parent_id"] == 0 else "  "
            level = "[CLIENT]" if item["parent_id"] == 0 else "[PROJECT]"
            print(f"  {indent}{level} {item['name']} (ID: {item['task_id']})")
        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()
