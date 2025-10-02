import os
import json
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from jira import JIRA
import hashlib

# Load environment variables
load_dotenv()

class JiraClient:
    """Client for interacting with Jira API"""
    
    def __init__(self, server: str, email: str, api_token: str):
        """
        Initialize the Jira client
        
        Args:
            server: The Jira server URL (e.g., https://your-domain.atlassian.net)
            email: Email address for authentication
            api_token: API token for authentication
        """
        self.server = server
        self.jira = JIRA(
            server=server,
            basic_auth=(email, api_token)
        )
        
    def get_projects(self) -> List[Dict[str, Any]]:
        """Get all projects from the Jira instance"""
        try:
            projects = self.jira.projects()
            return [self._serialize_project(project) for project in projects]
        except Exception as e:
            print(f"Error fetching projects: {str(e)}")
            return []
    
    def get_issues_for_project(self, project_key: str) -> List[Dict[str, Any]]:
        """
        Get all issues for a specific project
        
        Args:
            project_key: The project key (e.g., 'TCD')
            
        Returns:
            List of issues with their details
        """
        try:
            # Fetch all issues for the project
            # Using pagination to handle large projects
            all_issues = []
            start_at = 0
            max_results = 100
            
            while True:
                issues = self.jira.search_issues(
                    f'project = {project_key}',
                    startAt=start_at,
                    maxResults=max_results,
                    expand='names'
                )
                
                if not issues:
                    break
                
                all_issues.extend([self._serialize_issue(issue) for issue in issues])
                
                if len(issues) < max_results:
                    break
                    
                start_at += max_results
            
            return all_issues
            
        except Exception as e:
            print(f"Error fetching issues for project {project_key}: {str(e)}")
            return []
    
    def _serialize_project(self, project) -> Dict[str, Any]:
        """Convert Jira Project object to dictionary"""
        return {
            'id': project.id,
            'key': project.key,
            'name': project.name,
            'description': getattr(project, 'description', ''),
            'lead': getattr(project, 'lead', None),
            'project_type_key': getattr(project, 'projectTypeKey', '')
        }
    
    def _serialize_issue(self, issue) -> Dict[str, Any]:
        """Convert Jira Issue object to dictionary"""
        fields = issue.fields
        
        serialized = {
            'id': issue.id,
            'key': issue.key,
            'issue_type': fields.issuetype.name if hasattr(fields, 'issuetype') else '',
            'summary': fields.summary if hasattr(fields, 'summary') else '',
            'status': fields.status.name if hasattr(fields, 'status') else '',
            'priority': fields.priority.name if hasattr(fields, 'priority') and fields.priority else None,
            'assignee': fields.assignee.displayName if hasattr(fields, 'assignee') and fields.assignee else None,
            'reporter': fields.reporter.displayName if hasattr(fields, 'reporter') and fields.reporter else None,
            'created': str(fields.created) if hasattr(fields, 'created') else None,
            'updated': str(fields.updated) if hasattr(fields, 'updated') else None,
            'project_key': fields.project.key if hasattr(fields, 'project') else '',
            'parent': None,
            'subtasks': []
        }
        
        # Get parent issue if exists
        if hasattr(fields, 'parent') and fields.parent:
            serialized['parent'] = fields.parent.key
        
        # Get subtasks
        if hasattr(fields, 'subtasks') and fields.subtasks:
            serialized['subtasks'] = [subtask.key for subtask in fields.subtasks]
        
        # For Epic relationship (if using Jira Cloud/Server with Epic Link)
        if hasattr(fields, 'customfield_10014') and fields.customfield_10014:
            # Epic Link (common custom field for epic relationship)
            serialized['epic_link'] = str(fields.customfield_10014)
        
        return serialized

class JiraFetcher:
    """Main class for fetching data from multiple Jira instances"""
    
    def __init__(self):
        """Initialize with configuration from environment variables"""
        self.instances = self._load_instances_config()
        
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
        # Use hash to generate consistent numeric ID
        hash_value = int(hashlib.md5(url.encode()).hexdigest()[:6], 16)
        return f"org_{hash_value % 1000000}"
    
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
                    project_task_id = f"{org_id}_proj_{project['key']}"
                    
                    # Add project as child of organization
                    flattened_data.append({
                        'name': project['name'],
                        'task_id': project_task_id,
                        'parent_id': org_id
                    })
                    
                    # Get all issues for the project
                    issues = client.get_issues_for_project(project['key'])
                    
                    # Create issue task_ids and parent mapping
                    issue_key_to_task_id = {}
                    for issue in issues:
                        issue_task_id = f"{org_id}_proj_{project['key']}_{issue['key']}"
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
                            if parent_key in issue_key_to_task_id:
                                parent_id = issue_key_to_task_id[parent_key]
                        elif issue.get('epic_link'):
                            # Issue is linked to an epic
                            epic_key = issue['epic_link']
                            if epic_key in issue_key_to_task_id:
                                parent_id = issue_key_to_task_id[epic_key]
                        
                        flattened_data.append({
                            'name': issue['summary'],
                            'task_id': issue_task_id,
                            'parent_id': parent_id
                        })
                
            except Exception as e:
                print(f"Error fetching data from instance {instance_config['name']}: {str(e)}")
                import traceback
                traceback.print_exc()
        
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
    total_items = len(data)
    organizations = len([item for item in data if item['parent_id'] == 0])
    projects = len([item for item in data if isinstance(item['parent_id'], str) and item['parent_id'].startswith('org_') and '_proj_' in item['task_id'] and item['task_id'].count('_') == 2])
    issues = total_items - organizations - projects
    
    print(f"\nSummary:")
    print(f"  Total items: {total_items}")
    print(f"  Organizations: {organizations}")
    print(f"  Projects: {projects}")
    print(f"  Issues: {issues}")
    
    # Show structure preview
    if data:
        print(f"\nStructure preview:")
        for i, item in enumerate(data[:15]):
            # Determine indentation based on hierarchy level
            if item['parent_id'] == 0:
                indent = ""
                level = "[ORG]"
            elif isinstance(item['task_id'], str) and '_proj_' in item['task_id'] and item['task_id'].count('_') == 2:
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

