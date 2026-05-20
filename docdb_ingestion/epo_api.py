import os
import requests
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)

EPO_API_BASE_URL = os.environ.get("EPO_API_BASE_URL", "https://publication-bdds.apps.epo.org/bdds/bdds-bff-service/prod/api")

def _parse_iso_datetime(value: str):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            logger.warning(f"Unable to parse datetime value: {value}")
            return None


def get_product_deliveries(product_id: int) -> List[Dict]:
    """Fetch the list of deliveries for a given product, including per-file metadata."""
    url = f"{EPO_API_BASE_URL}/products/{product_id}"
    logger.info(f"Fetching deliveries for product {product_id} from {url}")

    response = requests.get(url)
    response.raise_for_status()
    product_data = response.json()

    deliveries = []
    for delivery in product_data.get('deliveries', []):
        delivery_publication_datetime = _parse_iso_datetime(delivery.get('deliveryPublicationDatetime'))
        expiry_datetime = _parse_iso_datetime(delivery.get('deliveryExpiryDatetime'))

        files = []
        for f in delivery.get('files', []):
            files.append({
                'file_id': f.get('fileId'),
                'filename': f.get('fileName'),
                'file_size': f.get('fileSize'),
                'file_checksum': f.get('fileChecksum'),
                'file_publication_datetime': _parse_iso_datetime(f.get('filePublicationDatetime')),
            })

        deliveries.append({
            'delivery_id': delivery.get('deliveryId'),
            'delivery_name': delivery.get('deliveryName'),
            'delivery_publication_datetime': delivery_publication_datetime,
            'delivery_expiry_datetime': expiry_datetime,
            'files': files,
        })

    return deliveries


def get_delivery_files(product_id: int, delivery_id: int) -> List[Dict]:
    """Fetch the list of files for a given delivery."""
    for delivery in get_product_deliveries(product_id):
        if delivery.get('delivery_id') == delivery_id:
            return delivery.get('files', [])

    logger.error(f"Delivery ID {delivery_id} not found in product {product_id}")
    return []

def download_file(product_id: int, delivery_id: int, file_id: int, dest_path: str):
    """Download a specific file ID directly to disk, streaming in chunks to save memory."""
    url = f"{EPO_API_BASE_URL}/products/{product_id}/delivery/{delivery_id}/file/{file_id}/download"
    logger.info(f"Downloading file ID {file_id} to {dest_path}")
    
    # We use stream=True to avoid loading large ZIPs into RAM
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    logger.info(f"Download complete for file ID {file_id}")
