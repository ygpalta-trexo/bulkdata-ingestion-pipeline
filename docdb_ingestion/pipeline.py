import os
import logging
import zipfile
import shutil
import glob
from datetime import datetime
from dotenv import load_dotenv

from .database import DatabaseManager, get_dsn_from_env
from .epo_api import get_delivery_files, download_file
from .stream_processor import process_zip_file

logger = logging.getLogger(__name__)


def resolve_pipeline_log_file(worker_name: str = None, explicit_log_file: str = None) -> str:
    """Resolve the log file path for a pipeline worker."""
    if explicit_log_file:
        log_file = explicit_log_file
    else:
        date_dir = os.path.join(os.getcwd(), 'logs', datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(date_dir, exist_ok=True)
        base_name = 'pipeline.log' if not worker_name else f'pipeline_{worker_name}.log'
        log_file = os.path.join(date_dir, base_name)

    log_dir = os.path.dirname(os.path.abspath(log_file))
    os.makedirs(log_dir, exist_ok=True)
    return log_file

class PipelineOrchestrator:
    def __init__(
        self,
        product_id: int = None,
        delivery_id: int = None,
        delivery_name: str = None,
        week_number: str = None,
        runner_mode: str = None,
    ):
        self.dsn = get_dsn_from_env()

        # Explicit args take priority over env vars — allows the front-file runner
        # to pass delivery IDs programmatically without touching the environment.
        self.product_id = product_id if product_id is not None else int(os.environ.get("EPO_PRODUCT_ID", 14))
        self.delivery_id = delivery_id if delivery_id is not None else int(os.environ.get("EPO_DELIVERY_ID", 3071))
        self.temp_dir = os.environ.get("EPO_TEMP_DIR", "./tmp_downloads")

        # Audit context — optional, populated by the frontfile_runner
        self.delivery_name = delivery_name
        self.week_number = week_number
        self.runner_mode = runner_mode

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

    def run(self, start_index=1, limit=None, retry_failed=False, batch_size_arg: int = None) -> bool:
        """Main execution loop for downloading, extracting, and processing files.
        
        Returns:
            True  — all files in this delivery completed successfully.
            False — one or more files failed (details logged and recorded in DB).
        """
        logger.info("Starting pipeline execution loop...")
        failed_files: list[dict] = []   # accumulates any file records that hit an error
        started_at = datetime.now()

        # Doc-count tallies (accumulated across all inner ZIPs in this delivery)
        docs_upserted = 0
        docs_deleted  = 0
        docs_skipped  = 0

        all_files = self.db.get_all_delivery_files(self.product_id, self.delivery_id)
        if not all_files:
            logger.info("No delivery files found. Pipeline is idle.")
            return True
            
        # Apply start-index (1-based index)
        if start_index > 1:
            skip_count = start_index - 1
            if skip_count >= len(all_files):
                logger.warning(f"start_index {start_index} is greater than total delivery files ({len(all_files)}). Nothing to process.")
                return True
            all_files = all_files[skip_count:]
            logger.info(f"Skipped first {skip_count} files. Starting at index {start_index} out of total files.")
        
        if limit is not None:
            all_files = all_files[:limit]
            logger.info(f"Applying limit: processing {len(all_files)} files.")
            
        logger.info(f"Found {len(all_files)} files to process.")
        
        # Determine effective batch size: CLI arg > env var > default
        batch_size = int(os.environ.get('DOCDB_BATCH_SIZE', str(batch_size_arg or 1000)))

        for file_rec in all_files:
            file_id = file_rec['file_id']
            filename = file_rec['filename']
            status = file_rec['status']
            
            skip_statuses = ('COMPLETED',) if retry_failed else ('COMPLETED', 'FAILED')
            if status in skip_statuses:
                logger.info(f"Skipping already '{status}' file ID {file_id}: {filename}")
                continue

            if filename.lower().endswith('.csv'):
                logger.info(f"Skipping coherence CSV (not a ZIP): {filename}")
                self.db.update_file_status(file_id, 'COMPLETED')
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
                    if not dtd_dir:
                        # Some deliveries extract with a subdirectory wrapper
                        # e.g. extract_dir/docdb_xml_202608_Amend_002/Root/DTDS/
                        hits = glob.glob(os.path.join(extract_dir, '*', 'Root', 'DTDS'))
                        if hits:
                            dtd_dir = hits[0]
                            logger.info(f"Found DTD directory (nested): {dtd_dir}")
                    
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
                        first_doc_number = None
                        last_doc_number = None
                        for doc in doc_generator:
                            current_doc_number = doc.pub_master.doc_number
                            if first_doc_number is None:
                                first_doc_number = current_doc_number
                            last_doc_number = current_doc_number
                            # ── Tally by operation for the audit log ──────────
                            if doc.operation == 'SKIP':
                                docs_skipped += 1
                            elif doc.operation in ('D', 'DV', 'V'):
                                docs_deleted += 1
                            else:
                                docs_upserted += 1
                            batch.append(doc)
                            if len(batch) >= batch_size:
                                self.db.bulk_upsert_safe(batch, stage_key=inner_zip_name)
                                batch = []
                                
                        if batch:
                            self.db.bulk_upsert_safe(batch, stage_key=inner_zip_name)
                            
                        self.db.mark_file_completed(
                            inner_zip_name,
                            first_doc_number=first_doc_number,
                            last_doc_number=last_doc_number,
                        )
                    
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
                # Store the short message (first line) alongside filename for the audit row
                failed_files.append({
                    'file_id': file_id,
                    'filename': filename,
                    'error': str(e).split('\n')[0][:200],  # first line, max 200 chars
                })
                
                # Try to clean up on failure
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir, ignore_errors=True)

        # ── Delivery-level outcome summary ────────────────────────────────────
        total_files = len([f for f in all_files if not f['filename'].lower().endswith('.csv')])

        if failed_files:
            logger.error(
                f"Delivery {self.delivery_id} finished with {len(failed_files)} FAILED file(s): "
                + ", ".join(f"[{f['file_id']}] {f['filename']}" for f in failed_files)
            )
            self._try_record_audit(
                status='FAILED', started_at=started_at, total_files=total_files,
                docs_upserted=docs_upserted, docs_deleted=docs_deleted, docs_skipped=docs_skipped,
                error_message='; '.join(
                    f"{f['filename']}: {f.get('error', 'unknown error')}"
                    for f in failed_files
                ),
            )
            return False

        logger.info(f"Delivery {self.delivery_id} — all files completed successfully.")
        self._try_record_audit(
            status='COMPLETED', started_at=started_at, total_files=total_files,
            docs_upserted=docs_upserted, docs_deleted=docs_deleted, docs_skipped=docs_skipped,
        )
        return True

    def _try_record_audit(self, *, status, started_at, total_files,
                          docs_upserted, docs_deleted, docs_skipped, error_message=None):
        """Write a delivery audit row. Errors here are logged but never propagate."""
        try:
            self.db.record_delivery_audit(
                delivery_id=self.delivery_id,
                product_id=self.product_id,
                delivery_name=self.delivery_name,
                week_number=self.week_number,
                runner_mode=self.runner_mode,
                started_at=started_at,
                status=status,
                total_files=total_files,
                docs_upserted=docs_upserted,
                docs_deleted=docs_deleted,
                docs_skipped=docs_skipped,
                error_message=error_message,
            )
        except Exception as audit_err:
            logger.warning(f"Could not write delivery audit row: {audit_err}")

                    
def main():
    import sys
    import argparse
    
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the EPO Pipeline Orchestrator")
    parser.add_argument("command", choices=["sync", "run"], help="Command to execute")
    parser.add_argument("--start-index", type=int, default=1, help="1-based index to point pipeline at the Nth actionable file")
    parser.add_argument("--limit", type=int, help="Limit the number of actionable files to process")
    parser.add_argument("--retry-failed", action="store_true", help="Retry processing for files with 'FAILED' status")
    parser.add_argument("--log-file", help="Explicit log file path for this worker/process")
    parser.add_argument("--worker-name", help="Worker label used in the default log filename, e.g. worker1")
    parser.add_argument("--batch-size", type=int, help="Number of documents to stage per upsert batch (overrides DOCDB_BATCH_SIZE env)")
    
    args = parser.parse_args()

    worker_name = args.worker_name or os.environ.get("PIPELINE_WORKER_NAME")
    explicit_log_file = args.log_file or os.environ.get("PIPELINE_LOG_FILE")
    log_file = resolve_pipeline_log_file(worker_name=worker_name, explicit_log_file=explicit_log_file)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ],
        force=True,
    )
    logger.info(f"Logging initialized. Outputting to {log_file}")
    if worker_name:
        logger.info(f"Worker name: {worker_name}")
        
    cmd = args.command
    logger.info(f"Initialized Pipeline Orchestrator for command: {cmd}")
    
    orchestrator = PipelineOrchestrator()
    
    if cmd == 'sync':
        orchestrator.sync()
    elif cmd == 'run':
        orchestrator.run(start_index=args.start_index, limit=args.limit, retry_failed=args.retry_failed, batch_size_arg=args.batch_size)
    else:
        logger.error(f"Unknown command: {cmd}")

if __name__ == '__main__':
    main()
