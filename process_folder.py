#!/usr/bin/env python3
"""
Process ZIP files directly from a folder without requiring index.xml parsing.

This script is useful for:
- Testing individual ZIP files
- Processing manually downloaded/extracted files
- Debugging specific documents
- Development and testing workflows

Usage:
    python process_folder.py /path/to/folder/containing/zips --dry-run --limit 5
    python process_folder.py /path/to/folder --dsn "postgresql://..." --output-json results.json
"""

import os
import argparse
import logging
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
import glob

from docdb_ingestion.stream_processor import process_zip_file
from docdb_ingestion.database import DatabaseManager, get_dsn_from_env
from docdb_ingestion.models import ExchangeDocument
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BATCH_SIZE = 2000

def find_zip_files(folder_path: str) -> List[str]:
    """Find all ZIP files in the specified folder and subfolders."""
    folder_path = Path(folder_path)

    if not folder_path.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    if not folder_path.is_dir():
        raise ValueError(f"Path is not a directory: {folder_path}")

    # Find all .zip files recursively
    zip_pattern = "**/*.zip"
    zip_files = list(folder_path.glob(zip_pattern))

    # Sort by filename for consistent processing order
    zip_files.sort(key=lambda x: x.name)

    logger.info(f"Found {len(zip_files)} ZIP files in {folder_path}")

    return [str(zip_file) for zip_file in zip_files]

def process_single_zip(
    zip_path: str,
    dtd_dir: Optional[str] = None,
    dry_run: bool = False,
    db: Optional['DatabaseManager'] = None,
    batch_size: int = BATCH_SIZE
) -> Dict[str, Any]:
    """Process a single ZIP file, streaming batches to the database.
    
    Documents are flushed in rolling batches to avoid memory accumulation.
    """
    logger.info(f"Processing {zip_path}...")

    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")

    stats = {
        'zip_path': zip_path,
        'filename': os.path.basename(zip_path),
        'documents_processed': 0,
        'errors': [],
        'extra_data_fields': set(),
    }

    try:
        batch = []
        doc_generator = process_zip_file(zip_path, dtd_dir=dtd_dir)

        for i, doc in enumerate(doc_generator):
            if i % 100 == 0 and i > 0:
                logger.debug(f"Processed {i} documents so far...")

            stats['documents_processed'] += 1

            # Collect extra_data field names for analysis
            if hasattr(doc, 'pub_master') and doc.pub_master.extra_data:
                stats['extra_data_fields'].update(doc.pub_master.extra_data.keys())

            if not dry_run:
                batch.append(doc)
                if len(batch) >= batch_size:
                    if db:
                        db.bulk_upsert_safe(batch)
                    batch = []

        # Flush remaining batch
        if not dry_run and batch and db:
            db.bulk_upsert_safe(batch)

        logger.info(f"Completed {zip_path}: {stats['documents_processed']} documents")

    except Exception as e:
        error_msg = f"Error processing {zip_path}: {str(e)}"
        logger.error(error_msg)
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        stats['errors'].append(error_msg)

    return stats

def save_results_to_json(results: List[Dict[str, Any]], output_file: str):
    """Save processing results to a JSON file."""
    # Convert sets to lists for JSON serialization
    for result in results:
        if 'extra_data_fields' in result:
            result['extra_data_fields'] = list(result['extra_data_fields'])

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str, ensure_ascii=False)

    logger.info(f"Results saved to {output_file}")

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Process ZIP files directly from a folder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run first 3 files
  python process_folder.py /path/to/zips --dry-run --limit 3

  # Process all files and save to database
  python process_folder.py /path/to/zips --dsn "postgresql://user:pass@host/db"

  # Process and save results to JSON
  python process_folder.py /path/to/zips --output-json results.json

  # Process specific file pattern
  python process_folder.py /path/to/zips --pattern "*2023*.zip"
        """
    )

    parser.add_argument("folder", help="Path to folder containing ZIP files")
    parser.add_argument("--dsn", help="Postgres DSN (optional if using .env)")
    parser.add_argument("--start-index", type=int, default=1, help="1-based index to start processing from (to distribute workload across a team)")
    parser.add_argument("--limit", type=int, help="Limit number of ZIP files to process")
    parser.add_argument("--dry-run", action="store_true", help="Process files but do not write to DB")
    parser.add_argument("--output-json", help="Save processing results to JSON file")
    parser.add_argument("--pattern", default="*.zip", help="File pattern to match (default: *.zip)")
    parser.add_argument("--dtd-dir", help="Path to DTD directory for XML validation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        db = None
        # Find ZIP files
        zip_files = find_zip_files(args.folder)

        if not zip_files:
            logger.warning(f"No ZIP files found in {args.folder}")
            return

        # Apply start-index (1-based index)
        if args.start_index > 1:
            skip_count = args.start_index - 1
            if skip_count >= len(zip_files):
                logger.warning(f"--start-index {args.start_index} is greater than total files ({len(zip_files)}). Nothing to process.")
                return
            zip_files = zip_files[skip_count:]
            logger.info(f"Skipped first {skip_count} files. Starting at index {args.start_index} out of total files.")

        # Apply limit if specified
        if args.limit:
            zip_files = zip_files[:args.limit]
            logger.info(f"Limited processing to {args.limit} files")

        # Setup database connection if not dry run
        if not args.dry_run:
            dsn = args.dsn or get_dsn_from_env()

            db = DatabaseManager(dsn)
            db.connect()
            logger.info("Connected to database")

        # Process files
        results = []
        total_processed = 0

        for zip_path in zip_files:
            try:
                # Determine DTD directory
                dtd_dir = args.dtd_dir
                if not dtd_dir:
                    # Try to find DTDS directory relative to the ZIP file
                    zip_dir = os.path.dirname(zip_path)
                    potential_dtd_dir = os.path.join(zip_dir, "DTDS")
                    if os.path.exists(potential_dtd_dir):
                        dtd_dir = potential_dtd_dir
                        logger.debug(f"Using DTD directory: {dtd_dir}")

                # Process the ZIP file — streaming batch writes inside
                stats = process_single_zip(
                    zip_path,
                    dtd_dir=dtd_dir,
                    dry_run=args.dry_run,
                    db=db
                )
                results.append(stats)
                total_processed += stats['documents_processed']

                if not args.dry_run and db:
                    logger.info(f"Saved {stats['documents_processed']} documents for {stats['filename']}")

            except Exception as e:
                error_msg = f"Failed to process {zip_path}: {str(e)}"
                logger.error(error_msg)
                results.append({
                    'zip_path': zip_path,
                    'filename': os.path.basename(zip_path),
                    'documents_processed': 0,
                    'errors': [error_msg],
                    'extra_data_fields': []
                })

        # Save results to JSON if requested
        if args.output_json:
            save_results_to_json(results, args.output_json)

        # Print summary
        successful_files = len([r for r in results if not r.get('errors')])
        total_errors = sum(len(r.get('errors', [])) for r in results)

        logger.info("=" * 50)
        logger.info("PROCESSING SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Total ZIP files found: {len(find_zip_files(args.folder))}")
        logger.info(f"ZIP files processed: {len(results)}")
        logger.info(f"Successful files: {successful_files}")
        logger.info(f"Files with errors: {len(results) - successful_files}")
        logger.info(f"Total documents processed: {total_processed}")
        logger.info(f"Total errors: {total_errors}")

        # Show extra data fields found
        all_extra_fields = set()
        for result in results:
            all_extra_fields.update(result.get('extra_data_fields', []))

        if all_extra_fields:
            logger.info(f"Extra data fields found: {sorted(all_extra_fields)}")

        if args.output_json:
            logger.info(f"Detailed results saved to: {args.output_json}")

    except Exception as e:
        logger.error(f"Script failed: {str(e)}")
        raise
    finally:
        if db:
            db.close()
            logger.info("Database connection closed")

if __name__ == "__main__":
    main()