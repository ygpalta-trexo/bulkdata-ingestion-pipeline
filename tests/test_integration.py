
# Integration Test Script

# 1. Setup minimal test DB (requires docker or local running)
# For this environment, we assume the user has a running Postgres.
# If not, we can mock it or ask user. 
# "The USER's OS version is linux." - usually we can expect some DB.
# But safest is to unittest the components first.

import unittest
import os
from unittest.mock import MagicMock, patch
from docdb_ingestion.index_parser import parse_index
from docdb_ingestion.stream_processor import process_zip_file

# Sample paths
SAMPLE_INDEX = "/home/ygpalta/repos/bdds/docdb_xml_bck_202534_001_A/Root/index.xml"
SAMPLE_ZIP = "/home/ygpalta/repos/bdds/docdb_xml_bck_202534_001_A/Root/DOC/DOCDB-202534-001-AM-0001.zip"

class TestComponents(unittest.TestCase):
    def test_index_parsing(self):
        # We expect a relative path resolution
        files = parse_index(SAMPLE_INDEX)
        self.assertTrue(len(files) > 0)
        first = files[0]
        self.assertTrue(first['filename'].endswith('.zip'))
        # Check if path is correctly resolved relative to DOC dir
        expected_dir = os.path.join(os.path.dirname(SAMPLE_INDEX), "DOC")
        self.assertTrue(first['path'].startswith(expected_dir))
        
    def test_zip_processing(self):
        # Read just the first document from the stream
        docs_iter = process_zip_file(SAMPLE_ZIP)
        try:
            doc = next(docs_iter)
        except StopIteration:
            self.fail("No documents found in ZIP")

        app = doc.app_master
        pub = doc.pub_master
        
        print("\n--- Extracted Document ---")
        print(f"App Doc ID: {app.app_doc_id}")
        print(f"Pub Doc ID: {pub.pub_doc_id}")
        print(f"Is Grant: {pub.is_grant}")
        print(f"Parties: {[p.party_name for p in doc.parties]}")
        print(f"Priorities: {[p.doc_number for p in doc.priorities]}")
        print(f"Citations: {len(doc.citations)}")
        print(f"Abstracts: {len(doc.abstracts_titles)}")

        self.assertEqual(pub.country, "AM")
        self.assertEqual(pub.exchange_status, "C")
        self.assertIsNotNone(app.app_doc_id)
        
        # Phase 4/5 Enhancements Verification
        applicants = [p for p in doc.parties if p.party_type == 'APPLICANT']
        inventors = [p for p in doc.parties if p.party_type == 'INVENTOR']

        # Not all sample docs have applicants/inventors
        if len(applicants) > 0:
             self.assertIsNotNone(applicants[0].party_name)
        
        if len(inventors) > 0:
             self.assertIsNotNone(inventors[0].party_name)

        print(f"Verified Application {app.app_number} -> Publication {pub.doc_number} ({pub.country}) with {len(applicants)} applicants, {len(inventors)} inventors, and {len(doc.priorities)} priorities.")

if __name__ == '__main__':
    unittest.main()
