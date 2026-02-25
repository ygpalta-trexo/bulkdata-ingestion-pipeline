import os
import lxml.etree as ET
from typing import List, Dict

def parse_index(index_path: str) -> List[Dict[str, str]]:
    """
    Parses the index.xml file to get a list of package files.
    Returns a list of dicts with 'filename' and resolved 'path'.
    """
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"Index file not found: {index_path}")

    base_dir = os.path.dirname(index_path)
    # The DOC directory is assumed to be a sibling of index.xml or in a subfolder.
    # Based on discovery, index.xml is in Root/, and zips are in Root/DOC/
    doc_dir = os.path.join(base_dir, "DOC")
    
    context = ET.iterparse(index_path, events=("end",), tag="docdb-package-file")
    
    package_files = []
    
    for event, elem in context:
        filename_elem = elem.find("filename")
        if filename_elem is not None and filename_elem.text:
            filename = filename_elem.text
            # IGNORE the <file-location> from XML as it is an absolute path from the source system.
            # We assume the file is in the DOC/ directory relative to index.xml
            file_path = os.path.join(doc_dir, filename)
            
            package_files.append({
                "filename": filename,
                "path": file_path
            })
            
        elem.clear()
        
    return package_files
