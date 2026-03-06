import os
import argparse
import logging
from typing import List

from docdb_ingestion.index_parser import parse_index
from docdb_ingestion.stream_processor import process_zip_file
from docdb_ingestion.database import DatabaseManager
from docdb_ingestion.models import ExchangeDocument
from dotenv import load_dotenv

from docdb_ingestion.database import DatabaseManager, get_dsn_from_env

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BATCH_SIZE = 2000

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="EPO DOCDB Ingestion Pipeline")
    parser.add_argument("--index", required=True, help="Path to index.xml")
    parser.add_argument("--dsn", help="Postgres DSN (optional if using .env)")
    parser.add_argument("--limit", type=int, help="Limit number of ZIP files to process (for testing)")
    parser.add_argument("--start-index", type=int, default=1, help="1-based index to start processing from (to distribute workload)")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--dry-run", action="store_true", help="Process files but do not write to DB")
    parser.add_argument("--force", action="store_true", help="Re-process files even if completed")

    args = parser.parse_args()

    # validate index
    package_files = parse_index(args.index)
    logger.info(f"Found {len(package_files)} files in index.")


    # Apply start-index (1-based index)
    if args.start_index > 1:
        skip_count = args.start_index - 1
        if skip_count >= len(package_files):
            logger.warning(f"--start-index {args.start_index} is greater than total files ({len(package_files)}). Nothing to process.")
            return
        package_files = package_files[skip_count:]
        logger.info(f"Skipped first {skip_count} files. Starting at index {args.start_index} out of total files.")

    if args.limit:
        package_files = package_files[:args.limit]
        logger.info(f"Limiting to first {args.limit} files.")

    db = None
    if not args.dry_run:
        dsn = args.dsn or get_dsn_from_env()
        
        db = DatabaseManager(dsn)
        db.connect()

    processed_count = 0
    
    for pkg in package_files:
        filename = pkg['filename']
        file_path = pkg['path']
        
        if not args.dry_run and not args.force and args.resume:
            if db.is_file_processed(filename):
                logger.info(f"Skipping {filename} (already processed)")
                continue

        logger.info(f"Processing {filename}...")
        
        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            continue

        if not args.dry_run:
            db.mark_file_started(filename)

        batch: List[ExchangeDocument] = []
        doc_count = 0
        
        # Derive DTD directory from index path. 
        # Index is in Root/index.xml, DTDs are in Root/DTDS
        index_dir = os.path.dirname(os.path.abspath(args.index))
        dtd_dir = os.path.join(index_dir, "DTDS")

        for doc in process_zip_file(file_path, dtd_dir=dtd_dir):
            batch.append(doc)
            doc_count += 1
            if len(batch) >= BATCH_SIZE:
                if not args.dry_run:
                    db.bulk_upsert_safe(batch)
                batch = []
        
        # Flush remaining
        if batch and not args.dry_run:
            db.bulk_upsert_safe(batch)
            
        if not args.dry_run:
            db.mark_file_completed(filename)
            
        logger.info(f"Completed {filename}: {doc_count} docs.")
        processed_count += 1

    if db:
        db.close()
    
    logger.info(f"Pipeline finished. Processed {processed_count} files.")

if __name__ == "__main__":
    main()
