import os
import json
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

from src.jira_client import (
    JiraClient as SharedJiraClient,
    build_jira_issue_external_id,
    build_jira_project_external_id,
    generate_jira_org_id,
)

# Load environment variables
load_dotenv(override=True)

JiraClient = SharedJiraClient

class JiraFetcher:
    """Main class for fetching data from multiple Jira instances"""
    
    def __init__(self):
        """Initialize with configuration from environment variables"""
        self.instances = self._load_instances_config()
        self.prefix_issue_key_to_task_name = (
            os.getenv('JIRA_PREFIX_ISSUE_KEY_TO_TASK_NAME', '').strip().lower()
            in {'1', 'true', 'yes', 'y', 'on'}
        )
        
    def _load_instances_config(self) -> List[Dict[str, str]]:
        """
        Load Jira instances configuration from environment variable
        
        Expected format in .env:
        JIRA_INSTANCES='[{"name": "Instance 1", "url": "https://instance1.atlassian.net", "email": "user@example.com", "token": "your-token"}]'
        """
        instances = []
        
        # Load from single JSON environment variable
        instances_json = os.getenv('JIRA_INSTANCES')
        if instances_json:
            try:
                instances = json.loads(instances_json)
                # Validate required fields
                for instance in instances:
                    if not all(key in instance for key in ['name', 'url', 'email', 'token']):
                        print(f"Warning: Instance missing required fields: {instance.get('name', 'Unknown')}")
                        continue
            except json.JSONDecodeError as e:
                print(f"Error parsing JIRA_INSTANCES JSON: {str(e)}")
                return []
        
        return instances
    
    def _generate_org_id(self, url: str) -> str:
        """Generate a consistent org ID from URL"""
        return generate_jira_org_id(url)

    def _format_issue_name(self, issue: Dict[str, Any]) -> str:
        """Format an issue name according to the Jira task naming setting."""
        summary = issue['summary']
        if not self.prefix_issue_key_to_task_name:
            return summary

        return f"[{issue['key']}] {summary}".rstrip()
    
    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """
        Fetch data from all configured Jira instances and return flattened structure
        
        Returns:
            List of flattened items with name, task_id, and parent_id
        """
        flattened_data = []
        
        for instance_config in self.instances:
            print(f"Fetching data from instance: {instance_config['name']}")
            
            try:
                client = JiraClient(
                    instance_config['url'],
                    instance_config['email'],
                    instance_config['token']
                )
                
                # Get all projects
                projects = client.get_projects()
                
                # Add organization as top-level item
                org_name = instance_config['name']
                org_id = self._generate_org_id(instance_config['url'])
                
                flattened_data.append({
                    'name': org_name,
                    'task_id': org_id,
                    'parent_id': 0
                })
                
                for project in projects:
                    print(f"  Processing project: {project['name']} ({project['key']})")
                    
                    # Create project task_id with org prefix
                    project_task_id = build_jira_project_external_id(
                        org_id,
                        project['key'],
                    )
                    
                    # Add project as child of organization
                    flattened_data.append({
                        'name': project['name'],
                        'task_id': project_task_id,
                        'parent_id': org_id
                    })
                    
                    # Get all active issues for the project
                    issues = client.get_issues_for_project(project['key'])
                    
                    # Create issue task_ids and parent mapping
                    # Store active issue keys for parent validation
                    active_issue_keys = {issue['key'] for issue in issues}
                    issue_key_to_task_id = {}
                    for issue in issues:
                        issue_task_id = build_jira_issue_external_id(
                            org_id,
                            project['key'],
                            issue['key'],
                        )
                        issue_key_to_task_id[issue['key']] = issue_task_id
                    
                    # Add issues to flattened structure
                    for issue in issues:
                        issue_task_id = issue_key_to_task_id[issue['key']]
                        
                        # Determine parent
                        parent_id = project_task_id  # Default to project as parent
                        
                        # Check if this issue has a parent issue
                        if issue['parent']:
                            # Parent is another issue
                            parent_key = issue['parent']
                            # Only use parent if it's active (not completed)
                            if parent_key in active_issue_keys:
                                parent_id = issue_key_to_task_id[parent_key]
                            # If parent is completed, keep default (project as parent)
                        elif issue.get('epic_link'):
                            # Issue is linked to an epic
                            epic_key = issue['epic_link']
                            # Only use epic if it's active (not completed)
                            if epic_key in active_issue_keys:
                                parent_id = issue_key_to_task_id[epic_key]
                            # If epic is completed, keep default (project as parent)
                        
                        flattened_data.append({
                            'name': self._format_issue_name(issue),
                            'task_id': issue_task_id,
                            'parent_id': parent_id,
                            'original_estimate': issue.get('original_estimate'),
                            'original_estimate_seconds': issue.get('original_estimate_seconds'),
                        })
                
            except Exception as e:
                print(f"Error fetching data from instance {instance_config['name']}: {str(e)}")
                import traceback
                traceback.print_exc()
                raise
        
        return flattened_data
    
    def save_to_json(self, data: List[Dict[str, Any]], filename: Optional[str] = None) -> str:
        """
        Save data to JSON file
        
        Args:
            data: Data to save (list of flattened items)
            filename: Optional filename, defaults to 'tasks.json'
            
        Returns:
            The filename used
        """
        if not filename:
            filename = "tasks.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return filename

def main():
    """Main function to fetch and save Jira data"""
    print("Starting Jira data fetch...")
    print(f"Started at: {datetime.now()}")
    
    fetcher = JiraFetcher()
    
    if not fetcher.instances:
        print("No Jira instances configured.")
        print("Please set up JIRA_INSTANCES in your .env file:")
        print('JIRA_INSTANCES=\'[{"name": "Instance 1", "url": "https://instance1.atlassian.net", "email": "user@example.com", "token": "your-token"}]\'')
        return
    
    print(f"Found {len(fetcher.instances)} instance(s) configured:")
    for instance in fetcher.instances:
        print(f"  - {instance['name']}: {instance['url']}")
    
    # Fetch all data
    data = fetcher.fetch_all_data()
    
    # Save to JSON
    filename = fetcher.save_to_json(data)
    
    print(f"\nData fetch completed at: {datetime.now()}")
    print(f"Data saved to: {filename}")
    
    # Print summary
    def is_project_item(item: Dict[str, Any]) -> bool:
        return (
            item['parent_id'] != 0
            and isinstance(item['parent_id'], str)
            and isinstance(item['task_id'], str)
            and item['task_id'].startswith(f"{item['parent_id']}_proj_")
        )

    total_items = len(data)
    organizations = len([item for item in data if item['parent_id'] == 0])
    projects = len([item for item in data if is_project_item(item)])
    issues = total_items - organizations - projects
    
    print("\nSummary:")
    print(f"  Total items: {total_items}")
    print(f"  Organizations: {organizations}")
    print(f"  Projects: {projects}")
    print(f"  Issues: {issues}")
    
    # Show structure preview
    if data:
        print("\nStructure preview:")
        for i, item in enumerate(data[:15]):
            # Determine indentation based on hierarchy level
            if item['parent_id'] == 0:
                indent = ""
                level = "[ORG]"
            elif is_project_item(item):
                indent = "  "
                level = "[PROJECT]"
            else:
                indent = "    "
                level = "[ISSUE]"
            
            print(f"  {indent}{level} {item['name']} (ID: {item['task_id']}, Parent: {item['parent_id']})")
        
        if len(data) > 15:
            print(f"  ... and {len(data) - 15} more items")

if __name__ == "__main__":
    main()
