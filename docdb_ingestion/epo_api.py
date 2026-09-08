import os
import requests
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

EPO_API_BASE_URL = os.environ.get("EPO_API_BASE_URL", "https://publication-bdds.apps.epo.org/bdds/bdds-bff-service/prod/api/public")

def get_delivery_files(product_id: int, delivery_id: int) -> List[Dict]:
    """Fetch the list of files for a given delivery."""
    url = f"{EPO_API_BASE_URL}/products/{product_id}"
    
    response = requests.get(url)
    response.raise_for_status()
    product_data = response.json()
    
    # Find the specific delivery
    for delivery in product_data.get('deliveries', []):
        if delivery.get('deliveryId') == delivery_id:
            files = []
            for f in delivery.get('items', []):
                files.append({
                    'file_id': f.get('itemId'),
                    'filename': f.get('itemName')
                })
            return files
            
    logger.error(f"Delivery ID {delivery_id} not found in product {product_id}")
    return []

def download_file(product_id: int, delivery_id: int, file_id: int, dest_path: str):
    """Download a specific file ID directly to disk, streaming in chunks to save memory."""
    url = f"{EPO_API_BASE_URL}/products/{product_id}/delivery/{delivery_id}/item/{file_id}/download"
    logger.info(f"Downloading file ID {file_id} to {dest_path}")
    
    # We use stream=True to avoid loading large ZIPs into RAM
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    logger.info(f"Download complete for file ID {file_id}")
