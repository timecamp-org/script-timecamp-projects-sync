import argparse
import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)


ACTIVE_TICKET_STATUSES = {"new", "open", "pending", "hold"}


class ZendeskClient:
    """Client for interacting with the Zendesk Support API."""

    def __init__(self, url: str, email: str, api_token: str):
        self.base_url = normalize_zendesk_url(url)
        self.auth = (f"{email}/token", api_token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TimeCamp-Zendesk-Sync",
        }

    def _paginate(
        self,
        path: str,
        key: str,
        params: Optional[dict] = None,
    ) -> List[Dict[str, Any]]:
        """Generic Zendesk cursor/offset paginated GET request."""
        url = f"{self.base_url}{path}"
        results: List[Dict[str, Any]] = []
        params = params or {}

        while url:
            response = requests.get(
                url,
                auth=self.auth,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            results.extend(data.get(key, []))

            url = data.get("links", {}).get("next") or data.get("next_page")
            params = {}

        return results

    def get_organizations(self) -> List[Dict[str, Any]]:
        """Get all organizations from Zendesk."""
        return self._paginate("/api/v2/organizations.json", "organizations")

    def get_tickets(self) -> List[Dict[str, Any]]:
        """Get tickets from Zendesk."""
        return self._paginate("/api/v2/tickets.json", "tickets")


class ZendeskFetcher:
    """Fetches Zendesk organizations and tickets and outputs tasks.json format."""

    def __init__(
        self,
        include_solved: bool = False,
        include_closed: bool = False,
        group_by_instance: bool = False,
    ):
        self.instances = load_instances_config()
        self.include_solved = include_solved
        self.include_closed = include_closed
        self.group_by_instance = group_by_instance

    def _ticket_is_active(self, ticket: Dict[str, Any]) -> bool:
        status = str(ticket.get("status", "")).lower()

        if status in ACTIVE_TICKET_STATUSES:
            return True

        if status == "solved" and self.include_solved:
            return True

        if status == "closed" and self.include_closed:
            return True

        return False

    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """Fetch Zendesk data from all configured instances as flattened tasks."""
        flattened_data: List[Dict[str, Any]] = []

        for instance in self.instances:
            print(f"Fetching data from instance: {instance['name']}")

            client = ZendeskClient(
                instance["url"],
                instance["email"],
                instance["token"],
            )

            instance_id = instance_task_id(instance["url"])
            root_parent_id = 0

            if self.group_by_instance:
                flattened_data.append({
                    "name": instance["name"],
                    "task_id": instance_id,
                    "parent_id": 0,
                })
                root_parent_id = instance_id

            print("  Fetching organizations...")
            organizations = client.get_organizations()
            organizations_by_id = {
                organization["id"]: organization
                for organization in organizations
                if organization.get("id")
            }
            print(f"  Found {len(organizations_by_id)} organizations")

            print("  Fetching tickets...")
            tickets = [
                ticket
                for ticket in client.get_tickets()
                if ticket.get("id") and self._ticket_is_active(ticket)
            ]
            print(f"  Found {len(tickets)} active tickets")

            organization_ids_with_tickets = {
                ticket.get("organization_id")
                for ticket in tickets
                if ticket.get("organization_id") in organizations_by_id
            }

            for organization_id in sorted(
                organization_ids_with_tickets,
                key=lambda item: str(organizations_by_id[item].get("name", "")).lower(),
            ):
                organization = organizations_by_id[organization_id]
                flattened_data.append({
                    "name": organization.get("name") or f"Organization {organization_id}",
                    "task_id": organization_task_id(instance_id, organization_id),
                    "parent_id": root_parent_id,
                })

            for ticket in sorted(tickets, key=lambda item: item.get("id", 0)):
                ticket_id = ticket["id"]
                subject = ticket.get("subject") or f"Ticket #{ticket_id}"
                organization_id = ticket.get("organization_id")
                parent_id = root_parent_id

                if organization_id in organizations_by_id:
                    parent_id = organization_task_id(instance_id, organization_id)

                flattened_data.append({
                    "name": f"#{ticket_id} {subject}",
                    "task_id": ticket_task_id(instance_id, ticket_id),
                    "parent_id": parent_id,
                })

        return flattened_data

    def save_to_json(self, data: List[Dict[str, Any]], filename: str = "tasks.json") -> str:
        """Save data to JSON file."""
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return filename


def normalize_zendesk_url(url: str) -> str:
    """Return a normalized Zendesk base URL."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def instance_task_id(url: str) -> str:
    """Return a stable task ID for a Zendesk instance."""
    normalized_url = normalize_zendesk_url(url)
    hash_value = hashlib.md5(normalized_url.encode()).hexdigest()[:8]
    return f"zendesk_instance_{hash_value}"


def organization_task_id(instance_id: str, organization_id: int) -> str:
    """Return a stable task ID for a Zendesk organization."""
    return f"{instance_id}_org_{organization_id}"


def ticket_task_id(instance_id: str, ticket_id: int) -> str:
    """Return a stable task ID for a Zendesk ticket."""
    return f"{instance_id}_ticket_{ticket_id}"


def load_instances_config() -> List[Dict[str, str]]:
    """
    Load Zendesk instances configuration from environment variables.

    Preferred format:
    ZENDESK_INSTANCES='[{"name": "Support", "url": "https://example.zendesk.com", "email": "user@example.com", "token": "api-token"}]'

    Single-instance fallback:
    ZENDESK_NAME=Support
    ZENDESK_URL=https://example.zendesk.com
    ZENDESK_EMAIL=user@example.com
    ZENDESK_API_TOKEN=api-token
    """
    instances_json = os.getenv("ZENDESK_INSTANCES")
    if instances_json:
        try:
            instances = json.loads(instances_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Error parsing ZENDESK_INSTANCES JSON: {exc}") from exc

        valid_instances = []
        for instance in instances:
            if not all(key in instance for key in ["name", "url", "email", "token"]):
                print(
                    "Warning: Zendesk instance missing required fields: "
                    f"{instance.get('name', 'Unknown')}"
                )
                continue
            valid_instances.append(instance)

        return valid_instances

    name = os.getenv("ZENDESK_NAME", "Zendesk")
    url = os.getenv("ZENDESK_URL")
    email = os.getenv("ZENDESK_EMAIL")
    token = os.getenv("ZENDESK_API_TOKEN")

    if all([url, email, token]):
        return [{
            "name": name,
            "url": url,
            "email": email,
            "token": token,
        }]

    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Zendesk organizations and tickets into tasks.json format "
            "for TimeCamp sync."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tasks.json",
        help="Output file path (default: tasks.json)",
    )
    parser.add_argument(
        "--include-solved",
        action="store_true",
        help="Include solved tickets in addition to new/open/pending/hold tickets",
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Include closed tickets in addition to active tickets",
    )
    parser.add_argument(
        "--group-by-instance",
        action="store_true",
        help="Add Zendesk instance as a top-level parent above organizations and ungrouped tickets",
    )

    return parser.parse_args()


def main():
    """Fetch Zendesk organizations and tickets -> tasks.json."""
    args = parse_args()

    print("Starting Zendesk data fetch...")
    print(f"Started at: {datetime.now()}")

    fetcher = ZendeskFetcher(
        include_solved=args.include_solved,
        include_closed=args.include_closed,
        group_by_instance=args.group_by_instance,
    )

    if not fetcher.instances:
        print("No Zendesk instances configured.")
        print(
            "Please set up ZENDESK_INSTANCES or ZENDESK_URL, ZENDESK_EMAIL, "
            "and ZENDESK_API_TOKEN in your .env file."
        )
        return

    print(f"Found {len(fetcher.instances)} instance(s) configured:")
    for instance in fetcher.instances:
        print(f"  - {instance['name']}: {normalize_zendesk_url(instance['url'])}")

    data = fetcher.fetch_all_data()
    filename = fetcher.save_to_json(data, args.output)

    organizations = len([
        item
        for item in data
        if "_org_" in str(item["task_id"])
    ])
    tickets = len([
        item
        for item in data
        if "_ticket_" in str(item["task_id"])
    ])

    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    print("\nSummary:")
    print(f"  Organizations: {organizations}")
    print(f"  Tickets: {tickets}")
    print(f"  Total items: {len(data)}")

    if data:
        print("\nStructure preview:")
        for item in data[:20]:
            task_id = str(item["task_id"])
            if item["parent_id"] == 0 and task_id.startswith("zendesk_instance_"):
                indent = ""
                level = "[INSTANCE]"
            elif "_org_" in task_id:
                indent = "  " if str(item["parent_id"]).startswith("zendesk_instance_") else ""
                level = "[ORG]"
            else:
                indent = "    " if "_org_" in str(item["parent_id"]) else "  "
                level = "[TICKET]"

            print(
                f"  {indent}{level} {item['name']} "
                f"(ID: {item['task_id']}, Parent: {item['parent_id']})"
            )

        if len(data) > 20:
            print(f"  ... and {len(data) - 20} more items")


if __name__ == "__main__":
    main()
