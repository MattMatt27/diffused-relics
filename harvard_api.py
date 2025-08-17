"""
Harvard Art Museums API Integration for Diffused Relics

This module provides functionality to search and fetch data from the Harvard Art Museums API.
"""

import os
import requests
import json
from typing import Dict, List, Optional

class HarvardAPIClient:
    """Client for interacting with Harvard Art Museums API"""
    
    def __init__(self):
        self.api_key = os.environ.get('HARVARD_API_KEY')
        self.base_url = 'https://api.harvardartmuseums.org'
        
        if not self.api_key:
            raise ValueError("HARVARD_API_KEY environment variable must be set")
    
    def search_objects(self, query: str, size: int = 10) -> List[Dict]:
        """
        Search for objects in Harvard Art Museums
        
        Args:
            query: Search query (ID, title, artist, etc.)
            size: Maximum number of results
            
        Returns:
            List of object data dictionaries
        """
        # If query is a number, try exact ID search first
        if query.isdigit():
            exact_result = self.get_object_by_id(int(query))
            if exact_result:
                return [self._format_search_result(exact_result)]
        
        # General search
        params = {
            'apikey': self.api_key,
            'size': size,
            'keyword': query,
            'hasimage': 1  # Only return objects with images
        }
        
        try:
            response = requests.get(f"{self.base_url}/object", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            results = []
            for record in data.get('records', []):
                results.append(self._format_search_result(record))
            
            return results
            
        except requests.exceptions.RequestException as e:
            print(f"API search error: {e}")
            return []
    
    def get_object_by_id(self, object_id: int) -> Optional[Dict]:
        """
        Get object by Harvard ID
        
        Args:
            object_id: Harvard object ID
            
        Returns:
            Object data dictionary or None if not found
        """
        params = {'apikey': self.api_key}
        
        try:
            response = requests.get(f"{self.base_url}/object/{object_id}", params=params, timeout=10)
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"API fetch error for object {object_id}: {e}")
            return None
    
    def _format_search_result(self, obj: Dict) -> Dict:
        """Format API object for search results display"""
        # Extract artist
        artist = "Unknown Artist"
        if obj.get('people'):
            for person in obj['people']:
                if person.get('role') == 'Artist':
                    artist = person.get('name', artist)
                    break
            if artist == "Unknown Artist" and obj['people']:
                artist = obj['people'][0].get('name', artist)
        
        # Get thumbnail URL respecting permissions
        thumbnail_url = None
        permission_level = obj.get('imagepermissionlevel', 0)
        
        if permission_level < 2:  # 0 or 1 = can display images
            if obj.get('primaryimageurl'):
                thumbnail_url = obj['primaryimageurl']
                # Use IIIF for size control if available
                images = obj.get('images', [])
                if images and images[0].get('iiifbaseuri'):
                    max_size = 256 if permission_level == 1 else 200
                    thumbnail_url = f"{images[0]['iiifbaseuri']}/full/{max_size},/0/default.jpg"
        
        # Permission text
        permission_text = {
            0: "Full display allowed",
            1: "Limited to 256px",
            2: "No image display"
        }.get(permission_level, "Unknown")
        
        return {
            'id': obj.get('objectid'),
            'object_number': obj.get('objectnumber'),
            'title': obj.get('title', 'Untitled'),
            'artist': artist,
            'dated': obj.get('dated', ''),
            'classification': obj.get('classification', ''),
            'medium': obj.get('medium', ''),
            'culture': obj.get('culture', ''),
            'thumbnail_url': thumbnail_url,
            'harvard_url': obj.get('url'),
            'image_permission_level': permission_level,
            'permission_text': permission_text,
            'can_display_image': permission_level < 2
        }
    
    def extract_artifact_data(self, api_data: Dict) -> Dict:
        """
        Extract and format artifact data for database insertion
        
        Args:
            api_data: Raw API response data
            
        Returns:
            Formatted data for database insertion
        """
        # Extract artist
        artist = None
        if api_data.get('people'):
            for person in api_data['people']:
                if person.get('role') == 'Artist':
                    artist = person.get('name')
                    break
            if not artist and api_data['people']:
                artist = api_data['people'][0].get('name')
        
        # Extract IIIF base URI
        iiif_base_uri = None
        images = api_data.get('images', [])
        if images and len(images) > 0:
            iiif_base_uri = images[0].get('iiifbaseuri')
        
        # Extract museum from creditline
        museum = 'Harvard Art Museums'
        creditline = api_data.get('creditline', '')
        if 'Harvard Art Museums/' in creditline:
            museum_part = creditline.split('Harvard Art Museums/')[1].split(',')[0].strip()
            if museum_part:
                museum = museum_part
        
        return {
            'harvard_object_id': api_data.get('objectid'),
            'harvard_object_number': api_data.get('objectnumber'),
            'title': api_data.get('title', 'Untitled'),
            'artist': artist,
            'culture': api_data.get('culture'),
            'period': api_data.get('period'),
            'medium': api_data.get('medium'),
            'museum': museum,
            'description': api_data.get('description'),
            'classification': api_data.get('classification'),
            'dated': api_data.get('dated'),
            'date_begin': api_data.get('datebegin'),
            'date_end': api_data.get('dateend'),
            'century': api_data.get('century'),
            'technique': api_data.get('technique'),
            'dimensions': api_data.get('dimensions'),
            'provenance': api_data.get('provenance'),
            'creditline': api_data.get('creditline'),
            'department': api_data.get('department'),
            'division': api_data.get('division'),
            'copyright': api_data.get('copyright'),
            'verification_level': api_data.get('verificationlevel'),
            'image_permission_level': api_data.get('imagepermissionlevel', 0),
            'access_level': api_data.get('accesslevel', 1),
            'harvard_url': api_data.get('url'),
            'primary_image_url': api_data.get('primaryimageurl'),
            'iiif_base_uri': iiif_base_uri
        }

# Global client instance
_api_client = None

def get_api_client() -> HarvardAPIClient:
    """Get or create the Harvard API client singleton"""
    global _api_client
    if _api_client is None:
        try:
            _api_client = HarvardAPIClient()
        except ValueError as e:
            print(f"Harvard API client initialization failed: {e}")
            return None
    return _api_client

def get_display_image_url(artifact_data: Dict) -> str:
    """
    Get the appropriate image URL for display based on permissions
    
    Args:
        artifact_data: Artifact data with Harvard fields
        
    Returns:
        URL string for image display
    """
    # Check if this is a local upload (has local image_path)
    if artifact_data.get('image_path') and not artifact_data.get('harvard_object_id'):
        return f"/static/uploads/{artifact_data['image_path']}"
    
    # Harvard image handling
    permission_level = artifact_data.get('image_permission_level', 0)
    
    if permission_level == 2:  # No image display
        return "/static/no-image.png"  # You'll need to add this placeholder
    
    primary_url = artifact_data.get('primary_image_url')
    iiif_uri = artifact_data.get('iiif_base_uri')
    
    if permission_level == 1 and iiif_uri:  # Limited to 256px
        return f"{iiif_uri}/full/256,/0/default.jpg"
    elif primary_url:
        return primary_url
    else:
        return "/static/no-image.png"

def get_thumbnail_url(artifact_data: Dict, size: int = 200) -> str:
    """Get thumbnail URL for artifact"""
    # Local uploads
    if artifact_data.get('image_path') and not artifact_data.get('harvard_object_id'):
        return f"/static/uploads/{artifact_data['image_path']}"
    
    # Harvard images
    permission_level = artifact_data.get('image_permission_level', 0)
    if permission_level == 2:
        return "/static/no-image.png"
    
    iiif_uri = artifact_data.get('iiif_base_uri')
    if iiif_uri:
        max_size = min(size, 256 if permission_level == 1 else size)
        return f"{iiif_uri}/full/{max_size},/0/default.jpg"
    
    return artifact_data.get('primary_image_url', "/static/no-image.png")