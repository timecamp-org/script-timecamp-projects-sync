import os
import json
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
from azure.devops.v7_1.work_item_tracking import WorkItemTrackingClient
from azure.devops.v7_1.core import CoreClient

# Load environment variables
load_dotenv()

class AzureDevOpsClient:
    """Client for interacting with Azure DevOps API"""
    
    def __init__(self, organization_url: str, personal_access_token: str):
        """
        Initialize the Azure DevOps client
        
        Args:
            organization_url: The Azure DevOps organization URL (e.g., https://dev.azure.com/myorg)
            personal_access_token: Personal Access Token for authentication
        """
        self.organization_url = organization_url
        credentials = BasicAuthentication('', personal_access_token)
        self.connection = Connection(base_url=organization_url, creds=credentials)
        self.wit_client = self.connection.clients.get_work_item_tracking_client()
        self.core_client = self.connection.clients.get_core_client()
        
    def get_projects(self) -> List[Dict[str, Any]]:
        """Get all projects from the Azure DevOps organization"""
        try:
            projects = self.core_client.get_projects()
            return [self._serialize_project(project) for project in projects]
        except Exception as e:
            print(f"Error fetching projects: {str(e)}")
            return []
    
    def get_work_items_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        """
        Get all work items for a specific project
        
        Args:
            project_id: The project ID or name
            
        Returns:
            List of work items with their details
        """
        try:
            # Get the project name for WIQL query
            project_name = project_id
            if isinstance(project_id, str) and len(project_id) > 30:  # Likely a GUID
                # If we have a GUID, we need to get the project name
                try:
                    project_obj = self.core_client.get_project(project_id)
                    project_name = project_obj.name
                except Exception:
                    # If we can't get the project name, use the ID as-is
                    project_name = project_id
            
            # Define WIQL query to get all work items
            wiql_query = {
                "query": f"""
                SELECT [System.Id], [System.WorkItemType], [System.Title], [System.State], 
                       [System.AssignedTo], [System.CreatedDate], [System.ChangedDate],
                       [System.AreaPath], [System.IterationPath], [Microsoft.VSTS.Common.Priority],
                       [Microsoft.VSTS.Scheduling.Effort], [Microsoft.VSTS.Scheduling.StoryPoints]
                FROM WorkItems 
                WHERE [System.TeamProject] = '{project_name}'
                ORDER BY [System.Id]
                """
            }
            
            # Execute the query
            query_result = self.wit_client.query_by_wiql(wiql_query)
            
            if not query_result.work_items:
                return []
            
            # Get work item IDs
            work_item_ids = [item.id for item in query_result.work_items]
            
            # Fetch detailed work items in batches (API limit is typically 200)
            batch_size = 200
            all_work_items = []
            
            for i in range(0, len(work_item_ids), batch_size):
                batch_ids = work_item_ids[i:i + batch_size]
                work_items = self.wit_client.get_work_items(
                    ids=batch_ids,
                    expand='Relations'
                )
                all_work_items.extend(work_items)
            
            return [self._serialize_work_item(wi) for wi in all_work_items]
            
        except Exception as e:
            print(f"Error fetching work items for project {project_id}: {str(e)}")
            return []
    
    def get_work_item_hierarchy(self, project_id: str) -> Dict[str, Any]:
        """
        Get work items organized in a hierarchical structure (Epics -> Features -> Stories -> Tasks)
        
        Args:
            project_id: The project ID or name
            
        Returns:
            Hierarchical structure of work items
        """
        work_items = self.get_work_items_for_project(project_id)
        
        # Create lookup dictionaries
        items_by_id = {item['id']: item for item in work_items}
        hierarchy = {
            'epics': [],
            'orphaned_items': []
        }
        
        # Build parent-child relationships
        for item in work_items:
            item['children'] = []
            
        # Process relationships
        for item in work_items:
            if 'relations' in item and item['relations']:
                for relation in item['relations']:
                    if relation['rel'] == 'System.LinkTypes.Hierarchy-Forward':
                        # This item is a parent
                        child_id = self._extract_id_from_url(relation['url'])
                        if child_id in items_by_id:
                            item['children'].append(items_by_id[child_id])
                    elif relation['rel'] == 'System.LinkTypes.Hierarchy-Reverse':
                        # This item has a parent
                        parent_id = self._extract_id_from_url(relation['url'])
                        if parent_id in items_by_id:
                            item['parent_id'] = parent_id
        
        # Organize top-level items
        for item in work_items:
            if not item.get('parent_id'):  # Top-level items
                if item['work_item_type'].lower() == 'epic':
                    hierarchy['epics'].append(item)
                else:
                    hierarchy['orphaned_items'].append(item)
        
        return hierarchy
    
    def _serialize_project(self, project) -> Dict[str, Any]:
        """Convert Azure DevOps Project object to dictionary"""
        return {
            'id': project.id,
            'name': project.name,
            'description': getattr(project, 'description', ''),
            'url': project.url,
            'state': project.state,
            'visibility': getattr(project, 'visibility', 'private'),
            'last_update_time': project.last_update_time.isoformat() if project.last_update_time else None
        }
    
    def _serialize_work_item(self, work_item) -> Dict[str, Any]:
        """Convert Azure DevOps WorkItem object to dictionary"""
        fields = work_item.fields
        
        serialized = {
            'id': work_item.id,
            'rev': work_item.rev,
            'url': work_item.url,
            'work_item_type': fields.get('System.WorkItemType', ''),
            'title': fields.get('System.Title', ''),
            'state': fields.get('System.State', ''),
            'assigned_to': self._serialize_identity(fields.get('System.AssignedTo')),
            'created_date': self._serialize_date(fields.get('System.CreatedDate')),
            'changed_date': self._serialize_date(fields.get('System.ChangedDate')),
            'area_path': fields.get('System.AreaPath', ''),
            'iteration_path': fields.get('System.IterationPath', ''),
            'priority': fields.get('Microsoft.VSTS.Common.Priority'),
            'effort': fields.get('Microsoft.VSTS.Scheduling.Effort'),
            'story_points': fields.get('Microsoft.VSTS.Scheduling.StoryPoints'),
            'description': fields.get('System.Description', ''),
            'tags': fields.get('System.Tags', ''),
        }
        
        # Add relations if they exist
        if hasattr(work_item, 'relations') and work_item.relations:
            serialized['relations'] = [
                {
                    'rel': relation.rel,
                    'url': relation.url,
                    'title': getattr(relation, 'title', ''),
                    'attributes': dict(relation.attributes) if hasattr(relation, 'attributes') and relation.attributes else {}
                }
                for relation in work_item.relations
            ]
        
        return serialized
    
    def _serialize_identity(self, identity) -> Optional[Dict[str, Any]]:
        """Serialize Azure DevOps identity object"""
        if not identity:
            return None
        
        if isinstance(identity, dict):
            return {
                'display_name': identity.get('displayName', ''),
                'unique_name': identity.get('uniqueName', ''),
                'id': identity.get('id', '')
            }
        elif isinstance(identity, str):
            return {'display_name': identity}
        
        return {'display_name': str(identity)}
    
    def _serialize_date(self, date_obj) -> Optional[str]:
        """Serialize date object to ISO string"""
        if not date_obj:
            return None
        
        if hasattr(date_obj, 'isoformat'):
            return date_obj.isoformat()
        
        return str(date_obj)
    
    def _extract_id_from_url(self, url: str) -> int:
        """Extract work item ID from Azure DevOps URL"""
        try:
            return int(url.split('/')[-1])
        except (ValueError, IndexError):
            return 0

class AzureDevOpsFetcher:
    """Main class for fetching data from multiple Azure DevOps instances"""
    
    def __init__(self):
        """Initialize with configuration from environment variables"""
        self.instances = self._load_instances_config()
        
    def _load_instances_config(self) -> List[Dict[str, str]]:
        """
        Load Azure DevOps instances configuration from environment variables
        
        Expected format:
        AZUREDEVOPS_INSTANCES=instance1_name:url:token,instance2_name:url:token
        
        Or individual environment variables:
        AZUREDEVOPS_INSTANCE1_NAME, AZUREDEVOPS_INSTANCE1_URL, AZUREDEVOPS_INSTANCE1_TOKEN
        """
        instances = []
        
        # Try to load from single environment variable first
        instances_config = os.getenv('AZUREDEVOPS_INSTANCES')
        if instances_config:
            for instance_config in instances_config.split(','):
                parts = instance_config.strip().split(':')
                if len(parts) >= 3:
                    name = parts[0]
                    url = parts[1]
                    token = ':'.join(parts[2:])  # In case token contains colons
                    instances.append({
                        'name': name,
                        'url': url,
                        'token': token
                    })
        
        # Also check for individual instance environment variables
        instance_count = 1
        while True:
            name_key = f'AZUREDEVOPS_INSTANCE{instance_count}_NAME'
            url_key = f'AZUREDEVOPS_INSTANCE{instance_count}_URL'
            token_key = f'AZUREDEVOPS_INSTANCE{instance_count}_TOKEN'
            
            name = os.getenv(name_key)
            url = os.getenv(url_key)
            token = os.getenv(token_key)
            
            if not all([name, url, token]):
                break
            
            instances.append({
                'name': name,
                'url': url,
                'token': token
            })
            
            instance_count += 1
        
        return instances
    
    def fetch_all_data(self) -> List[Dict[str, Any]]:
        """
        Fetch data from all configured Azure DevOps instances and return flattened structure
        
        Returns:
            List of flattened work items with name, task_id, and parent_id
        """
        flattened_data = []
        
        for instance_config in self.instances:
            print(f"Fetching data from instance: {instance_config['name']}")
            
            try:
                client = AzureDevOpsClient(
                    instance_config['url'],
                    instance_config['token']
                )
                
                # Get all projects
                projects = client.get_projects()
                
                # Add organization as top-level item
                org_name = instance_config['name']
                org_id = f"org_{hash(instance_config['url']) % 1000000}"  # Generate unique org ID
                
                flattened_data.append({
                    'name': org_name,
                    'task_id': org_id,
                    'parent_id': 0
                })
                
                for project in projects:
                    print(f"  Processing project: {project['name']}")
                    
                    # Create project task_id with org prefix
                    project_task_id = f"{org_id}_{project['id']}"
                    
                    # Add project/board as child of organization
                    flattened_data.append({
                        'name': project['name'],
                        'task_id': project_task_id,
                        'parent_id': org_id
                    })
                    
                    # Get all work items for the project
                    work_items = client.get_work_items_for_project(project['id'])
                    
                    # Filter out Done items first
                    active_work_items = [item for item in work_items if item.get('state', '').lower() != 'done']
                    active_work_item_ids = {item['id'] for item in active_work_items}
                    
                    # Create a mapping to find parent relationships
                    work_item_parents = {}
                    
                    # First pass: identify parent relationships
                    for item in active_work_items:
                        parent_id = project_task_id  # Default to project/board as parent
                        
                        if 'relations' in item and item['relations']:
                            for relation in item['relations']:
                                if relation['rel'] == 'System.LinkTypes.Hierarchy-Reverse':
                                    # This item has a parent work item
                                    parent_work_item_id = client._extract_id_from_url(relation['url'])
                                    if parent_work_item_id and parent_work_item_id != 0:
                                        # Only use parent if it's not Done (i.e., it's in active_work_item_ids)
                                        if parent_work_item_id in active_work_item_ids:
                                            parent_id = f"{org_id}_{parent_work_item_id}"
                                        # If parent is Done, keep default (project as parent)
                                    break
                        
                        work_item_parents[item['id']] = parent_id
                    
                    # Add work items to flattened structure (Done items already filtered out)
                    for item in active_work_items:
                        parent_id = work_item_parents.get(item['id'], project_task_id)
                        work_item_task_id = f"{org_id}_{item['id']}"
                        
                        flattened_data.append({
                            'name': item['title'],
                            'task_id': work_item_task_id,
                            'parent_id': parent_id
                        })
                
            except Exception as e:
                print(f"Error fetching data from instance {instance_config['name']}: {str(e)}")
        
        return flattened_data
    
    def save_to_json(self, data: List[Dict[str, Any]], filename: Optional[str] = None) -> str:
        """
        Save data to JSON file
        
        Args:
            data: Data to save (list of flattened work items)
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
    """Main function to fetch and save Azure DevOps data"""
    print("Starting Azure DevOps data fetch...")
    print(f"Started at: {datetime.now()}")
    
    fetcher = AzureDevOpsFetcher()
    
    if not fetcher.instances:
        print("No Azure DevOps instances configured.")
        print("Please set up environment variables for your instances:")
        print("Option 1: AZUREDEVOPS_INSTANCES=name1:url1:token1,name2:url2:token2")
        print("Option 2: Individual variables like AZUREDEVOPS_INSTANCE1_NAME, AZUREDEVOPS_INSTANCE1_URL, AZUREDEVOPS_INSTANCE1_TOKEN")
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
    boards = len([item for item in data if str(item['parent_id']).startswith('org_') and len(str(item['parent_id']).split('_')) == 2])
    work_items = total_items - organizations - boards
    
    print(f"\nSummary:")
    print(f"  Total items: {total_items}")
    print(f"  Organizations: {organizations}")
    print(f"  Boards/Projects: {boards}")
    print(f"  Work items: {work_items}")
    
    # Show structure preview
    if data:
        print(f"\nStructure preview:")
        for i, item in enumerate(data[:15]):
            # Determine indentation based on hierarchy level
            if item['parent_id'] == 0:
                indent = ""
                level = "[ORG]"
            elif str(item['parent_id']).startswith('org_') and len(str(item['parent_id']).split('_')) == 2:
                indent = "  "
                level = "[BOARD]"
            else:
                indent = "    "
                level = "[ITEM]"
            
            print(f"  {indent}{level} {item['name']} (ID: {item['task_id']}, Parent: {item['parent_id']})")
        
        if len(data) > 15:
            print(f"  ... and {len(data) - 15} more items")

if __name__ == "__main__":
    main() 