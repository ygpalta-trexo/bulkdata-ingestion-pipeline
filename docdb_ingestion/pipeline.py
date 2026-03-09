import os
import logging
import zipfile
import shutil
import glob
from dotenv import load_dotenv

from .database import DatabaseManager, get_dsn_from_env
from .epo_api import get_delivery_files, download_file
from .stream_processor import process_zip_file

logger = logging.getLogger(__name__)

class PipelineOrchestrator:
    def __init__(self):
        self.dsn = get_dsn_from_env()
            
        self.product_id = int(os.environ.get("EPO_PRODUCT_ID", 14))
        self.delivery_id = int(os.environ.get("EPO_DELIVERY_ID", 3071))
        self.temp_dir = os.environ.get("EPO_TEMP_DIR", "./tmp_downloads")
        
        os.makedirs(self.temp_dir, exist_ok=True)
        
        self.db = DatabaseManager(self.dsn)
        self.db.connect()

    def __del__(self):
        if hasattr(self, 'db'):
            self.db.close()

    def sync(self):
        """Fetches the latest file list from the API and saves it to the DB."""
        logger.info("Synchronizing delivery files with EPO API...")
        files = get_delivery_files(self.product_id, self.delivery_id)
        if not files:
            logger.warning("No files found to sync.")
            return

        self.db.sync_delivery_files(self.product_id, self.delivery_id, files)
        logger.info(f"Successfully synchronized {len(files)} files to the database.")

    def run(self, start_index=1, limit=None, retry_failed=False):
        """Main execution loop for downloading, extracting, and processing files."""
        logger.info("Starting pipeline execution loop...")
        
        all_files = self.db.get_all_delivery_files(self.product_id, self.delivery_id)
        if not all_files:
            logger.info("No delivery files found. Pipeline is idle.")
            return
            
        # Apply start-index (1-based index)
        if start_index > 1:
            skip_count = start_index - 1
            if skip_count >= len(all_files):
                logger.warning(f"start_index {start_index} is greater than total delivery files ({len(all_files)}). Nothing to process.")
                return
            all_files = all_files[skip_count:]
            logger.info(f"Skipped first {skip_count} files. Starting at index {start_index} out of total files.")
            
        if limit is not None:
            all_files = all_files[:limit]
            logger.info(f"Applying limit: processing {len(all_files)} files.")
            
        logger.info(f"Found {len(all_files)} files to process.")
        
        for file_rec in all_files:
            file_id = file_rec['file_id']
            filename = file_rec['filename']
            status = file_rec['status']
            
            skip_statuses = ('COMPLETED',) if retry_failed else ('COMPLETED', 'FAILED')
            if status in skip_statuses:
                logger.info(f"Skipping already '{status}' file ID {file_id}: {filename}")
                continue
                
            if status == 'FAILED' and retry_failed:
                logger.info(f"Retrying 'FAILED' file ID {file_id}: {filename}")
                status = 'PENDING'
            
            # Paths
            dest_zip_path = os.path.join(self.temp_dir, filename)
            extract_dir = os.path.join(self.temp_dir, f"extract_{file_id}")
            
            try:
                # 1. DOWNLOAD
                if status in ('PENDING', 'DOWNLOADING'):
                    self.db.update_file_status(file_id, 'DOWNLOADING')
                    
                    # Delete partial download if exists
                    if os.path.exists(dest_zip_path):
                        os.remove(dest_zip_path)
                        
                    download_file(self.product_id, self.delivery_id, file_id, dest_zip_path)
                    self.db.update_file_status(file_id, 'DOWNLOADED')
                    status = 'DOWNLOADED'

                # 2. EXTRACT
                if status in ('DOWNLOADED', 'EXTRACTING'):
                    self.db.update_file_status(file_id, 'EXTRACTING')
                    
                    if os.path.exists(extract_dir):
                        shutil.rmtree(extract_dir)
                    os.makedirs(extract_dir, exist_ok=True)
                    
                    logger.info(f"Extracting {dest_zip_path} to {extract_dir}")
                    with zipfile.ZipFile(dest_zip_path, 'r') as zf:
                        zf.extractall(extract_dir)
                    
                    # Once extracted, aggressively delete the huge source ZIP to save disk space
                    os.remove(dest_zip_path)
                    logger.info(f"Deleted source zip: {dest_zip_path}")
                    
                    self.db.update_file_status(file_id, 'EXTRACTED')
                    status = 'EXTRACTED'

                # 3. PARSE
                if status in ('EXTRACTED', 'PARSING'):
                    self.db.update_file_status(file_id, 'PARSING')
                    
                    # Find all internal ZIPs. The structure is usually Root/DOC/something.zip
                    internal_zips_raw = glob.glob(os.path.join(extract_dir, '**/*.zip'), recursive=True)
                    
                    # Skill rule: Mandatory ZIP processing order to handle re-keys and prevent pk collisions
                    def zip_sort_priority(filename):
                        base = os.path.basename(filename)
                        if 'DeleteRekey' in base: return 1
                        if 'CreateDelete' in base: return 2
                        if 'Amend' in base: return 3
                        return 4 # Unknowns or others at the end
                        
                    internal_zips = sorted(internal_zips_raw, key=zip_sort_priority)
                    
                    dtd_dir = None
                    for d in ['Root/DTDS', 'DTDS', 'Schema']:
                        potential_dtd = os.path.join(extract_dir, d)
                        if os.path.exists(potential_dtd):
                            dtd_dir = potential_dtd
                            break
                    
                    if not internal_zips:
                        # Fallback just in case there are bare XML files instead of internal ZIPs
                        logger.warning(f"No internal ZIP volumes found in {extract_dir}. Check extraction logic.")
                    
                    for inner_zip in internal_zips:
                        inner_zip_name = os.path.basename(inner_zip)
                        
                        if self.db.is_file_processed(inner_zip_name):
                            logger.info(f"Skipping already processed internal volume: {inner_zip_name}")
                            continue
                            
                        self.db.mark_file_started(inner_zip_name, file_id, filename)
                        
                        logger.info(f"Parsing internal volume: {inner_zip}")
                        doc_generator = process_zip_file(inner_zip, dtd_dir)
                        
                        batch = []
                        for doc in doc_generator:
                            batch.append(doc)
                            if len(batch) >= 1000:
                                self.db.bulk_upsert_safe(batch)
                                batch = []
                                
                        if batch:
                            self.db.bulk_upsert_safe(batch)
                            
                        self.db.mark_file_completed(inner_zip_name)
                    
                    # We have fully processed this file!
                    self.db.update_file_status(file_id, 'COMPLETED')
                    status = 'COMPLETED'
                    
                    # Clean up the extracted directory
                    shutil.rmtree(extract_dir)
                    logger.info(f"Cleaned up {extract_dir}")
                    
            except Exception as e:
                logger.error(f"Error processing file ID {file_id}: {e}")
                import traceback
                error_msg = traceback.format_exc()
                self.db.update_file_status(file_id, 'FAILED', error_msg)
                
                # Try to clean up on failure
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    
def main():
    import sys
    from datetime import datetime
    import argparse
    
    # Setup Date-wise logging
    log_dir = os.path.join(os.getcwd(), 'logs', datetime.now().strftime('%Y-%m-%d'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'pipeline.log')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger.info(f"Logging initialized. Outputting to {log_file}")
    
    parser = argparse.ArgumentParser(description="Run the EPO Pipeline Orchestrator")
    parser.add_argument("command", choices=["sync", "run"], help="Command to execute")
    parser.add_argument("--start-index", type=int, default=1, help="1-based index to point pipeline at the Nth actionable file")
    parser.add_argument("--limit", type=int, help="Limit the number of actionable files to process")
    parser.add_argument("--retry-failed", action="store_true", help="Retry processing for files with 'FAILED' status")
    
    args = parser.parse_args()
        
    cmd = args.command
    logger.info(f"Initialized Pipeline Orchestrator for command: {cmd}")
    
    orchestrator = PipelineOrchestrator()
    
    if cmd == 'sync':
        orchestrator.sync()
    elif cmd == 'run':
        orchestrator.run(start_index=args.start_index, limit=args.limit, retry_failed=args.retry_failed)
    else:
        logger.error(f"Unknown command: {cmd}")

if __name__ == '__main__':
    main()
