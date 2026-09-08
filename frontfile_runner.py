"""
frontfile_runner.py — EPO DOCDB front-file ingestion outer loop.

Usage
-----
# Process all weeks published after BACKFILE_TIME (first-run catch-up):
    python frontfile_runner.py --mode catchup

# Process only the latest unprocessed week (for weekly cron):
    python frontfile_runner.py --mode latest

# Dry-run: print the ordered delivery plan without executing anything:
    python frontfile_runner.py --mode catchup --dry-run
    python frontfile_runner.py --mode latest  --dry-run

Environment variables (all read from .env):
    EPO_FRONTFILE_PRODUCT_ID   — product ID for the front-file (e.g. 3)
    BACKFILE_TIME              — ISO-8601 cutoff; deliveries on or before this
                                 date are skipped (e.g. 2026-02-24T10:50:03.000+01:00)
    EPO_API_BASE_URL           — (optional) override the EPO API base URL
    EPO_TEMP_DIR               — (optional) temp download directory
    PIPELINE_LOG_FILE          — (optional) explicit log file path
    PIPELINE_WORKER_NAME       — (optional) label appended to the default log filename
    DOCDB_BATCH_SIZE           — (optional) upsert batch size passed to the pipeline
"""

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from dateutil.parser import isoparse
from dotenv import load_dotenv

# ── Import the existing pipeline machinery ────────────────────────────────────
from docdb_ingestion.pipeline import PipelineOrchestrator, resolve_pipeline_log_file

logger = logging.getLogger(__name__)

EPO_API_BASE_URL_DEFAULT = "https://publication-bdds.apps.epo.org/bdds/bdds-bff-service/prod/api/public"

# Regex that extracts the week token from a delivery name, e.g.
#   "14.7 DOCDB - EPO worldwide bibliographic data 2026/009 Amend"  →  "2026/009"
WEEK_PATTERN = re.compile(r"(\d{4}/\d{3})")

# Delivery type keywords (case-insensitive search in the delivery name)
TYPE_CRDEL_KEYWORDS = ("cr-del", "createdelete", "create-delete")
TYPE_AMEND_KEYWORDS = ("amend",)
TYPE_DELETEREKEY_KEYWORDS = ("deleterekey", "delete-rekey")


def _delivery_type_order(delivery_name: str) -> int:
    """
    Return a sort key that enforces the correct intra-week processing order:
        1 — DeleteRekey  (must come first, re-keys existing PKs)
        2 — Cr-Del / CreateDelete
        3 — Amend
        9 — Unknown (process last, do not block the week)
    """
    name_lower = delivery_name.lower()
    if any(kw in name_lower for kw in TYPE_DELETEREKEY_KEYWORDS):
        return 1
    if any(kw in name_lower for kw in TYPE_CRDEL_KEYWORDS):
        return 2
    if any(kw in name_lower for kw in TYPE_AMEND_KEYWORDS):
        return 3
    return 9


def fetch_all_deliveries(product_id: int) -> List[dict]:
    """Return raw delivery dicts from the EPO API for the given product."""
    base_url = os.environ.get("EPO_API_BASE_URL", EPO_API_BASE_URL_DEFAULT)
    url = f"{base_url}/products/{product_id}"
    logger.info(f"Fetching all deliveries for product {product_id} from {url}")
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get("deliveries", [])


def parse_delivery_week(delivery_name: str) -> str | None:
    """
    Extract the ISO week token from a delivery name.
    Returns e.g. "2026/009" or None if not found.
    """
    match = WEEK_PATTERN.search(delivery_name)
    return match.group(1) if match else None


def filter_and_group_deliveries(
    deliveries: List[dict],
    backfile_cutoff: datetime,
) -> Dict[str, List[dict]]:
    """
    Filter deliveries published *after* backfile_cutoff, then group by week token.

    Returns an ordered dict:
        { "2026/009": [delivery_dict, ...],  "2026/010": [...], ... }
    where deliveries within each week are sorted by _delivery_type_order().
    """
    grouped: Dict[str, List[dict]] = defaultdict(list)

    for delivery in deliveries:
        pub_str = delivery.get("deliveryPublicationDatetime", "")
        if not pub_str:
            logger.warning(
                f"Delivery {delivery.get('deliveryId')} has no publication date — skipping."
            )
            continue

        pub_dt = isoparse(pub_str)

        # Ensure both are timezone-aware for comparison
        if backfile_cutoff.tzinfo is None:
            backfile_cutoff = backfile_cutoff.replace(tzinfo=timezone.utc)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        if pub_dt <= backfile_cutoff:
            logger.debug(
                f"Skipping delivery {delivery.get('deliveryId')} "
                f"({delivery.get('deliveryName')}) — published on or before backfile cutoff."
            )
            continue

        week = parse_delivery_week(delivery.get("deliveryName", ""))
        if week is None:
            logger.warning(
                f"Cannot parse week from delivery name "
                f"'{delivery.get('deliveryName')}' (ID={delivery.get('deliveryId')}) — skipping."
            )
            continue

        grouped[week].append(delivery)

    # Sort deliveries within each week by processing priority
    for week in grouped:
        grouped[week].sort(key=lambda d: _delivery_type_order(d.get("deliveryName", "")))

    # Return weeks in ascending chronological order
    return dict(sorted(grouped.items()))


def build_execution_plan(
    grouped: Dict[str, List[dict]],
) -> List[Tuple[str, str, int, str]]:
    """
    Flatten the grouped structure into an ordered list of
    (week, delivery_type_label, delivery_id, delivery_name) tuples.
    """
    plan = []
    for week, deliveries in grouped.items():
        for d in deliveries:
            plan.append(
                (
                    week,
                    d.get("deliveryName", ""),
                    d["deliveryId"],
                    d.get("deliveryPublicationDatetime", ""),
                )
            )
    return plan


def run_pipeline_for_delivery(
    product_id: int,
    delivery_id: int,
    delivery_name: str,
    week_number: str = None,
    runner_mode: str = None,
    batch_size: int = None,
    retry_failed: bool = False,
) -> bool:
    """Instantiate the existing PipelineOrchestrator and run sync + run for one delivery.

    Returns True if all files completed successfully, False if any file failed.
    """
    logger.info(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    logger.info(f"  Processing delivery: {delivery_name}")
    logger.info(f"  product_id={product_id}  delivery_id={delivery_id}")
    logger.info(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    orchestrator = PipelineOrchestrator(
        product_id=product_id,
        delivery_id=delivery_id,
        delivery_name=delivery_name,
        week_number=week_number,
        runner_mode=runner_mode,
    )
    orchestrator.sync()
    success = orchestrator.run(retry_failed=retry_failed, batch_size_arg=batch_size)
    return success



def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "EPO DOCDB front-file ingestion runner. "
            "Groups weekly deliveries and calls the main pipeline in correct order."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["catchup", "latest"],
        required=True,
        help=(
            "catchup — process ALL weeks published after BACKFILE_TIME (initial catch-up run). "
            "latest  — process only the most-recent week (for weekly cron)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the ordered execution plan without downloading or processing anything.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry delivery files that previously failed.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Documents per upsert batch (overrides DOCDB_BATCH_SIZE env).",
    )
    parser.add_argument(
        "--log-file",
        help="Explicit log file path for this run.",
    )
    parser.add_argument(
        "--worker-name",
        help="Worker label used in the default log filename.",
    )
    args = parser.parse_args()

    # ── Logging setup ─────────────────────────────────────────────────────────
    worker_name = args.worker_name or os.environ.get("PIPELINE_WORKER_NAME", "frontfile")
    explicit_log_file = args.log_file or os.environ.get("PIPELINE_LOG_FILE")
    log_file = resolve_pipeline_log_file(
        worker_name=worker_name, explicit_log_file=explicit_log_file
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logger.info(f"Front-file runner started. Log: {log_file}  mode={args.mode}")

    # ── Read required env vars ─────────────────────────────────────────────────
    product_id_str = os.environ.get("EPO_FRONTFILE_PRODUCT_ID")
    if not product_id_str:
        logger.error("EPO_FRONTFILE_PRODUCT_ID is not set in .env. Aborting.")
        sys.exit(1)
    product_id = int(product_id_str)

    backfile_time_str = os.environ.get("BACKFILE_TIME")
    if not backfile_time_str:
        logger.error("BACKFILE_TIME is not set in .env. Aborting.")
        sys.exit(1)
    backfile_cutoff = isoparse(backfile_time_str)
    logger.info(f"Backfile cutoff: {backfile_cutoff.isoformat()}")

    # ── Fetch + filter + group ─────────────────────────────────────────────────
    all_deliveries = fetch_all_deliveries(product_id)
    logger.info(f"Total deliveries returned by API: {len(all_deliveries)}")

    grouped = filter_and_group_deliveries(all_deliveries, backfile_cutoff)
    logger.info(f"Weeks to process after filtering: {len(grouped)}  ({', '.join(grouped.keys())})")

    if not grouped:
        logger.info("No new weeks to process. Exiting.")
        sys.exit(0)

    # ── Apply mode ─────────────────────────────────────────────────────────────
    if args.mode == "latest":
        # Keep only the last week
        last_week = list(grouped.keys())[-1]
        grouped = {last_week: grouped[last_week]}
        logger.info(f"Mode=latest: limiting to week {last_week}.")

    plan = build_execution_plan(grouped)

    # ── Print execution plan (always shown, even without --dry-run) ────────────
    logger.info("")
    logger.info("═══ Execution Plan ═══════════════════════════════════════════")
    for idx, (week, delivery_name, delivery_id, pub_date) in enumerate(plan, 1):
        logger.info(
            f"  [{idx:02d}] week={week}  delivery_id={delivery_id}"
            f"  pub={pub_date[:10]}  name={delivery_name}"
        )
    logger.info("══════════════════════════════════════════════════════════════")
    logger.info("")

    if args.dry_run:
        logger.info("--dry-run flag set. Exiting without processing.")
        sys.exit(0)

    # ── Execute ────────────────────────────────────────────────────────────────
    batch_size = args.batch_size or int(os.environ.get("DOCDB_BATCH_SIZE", "1000"))

    for idx, (week, delivery_name, delivery_id, _pub_date) in enumerate(plan, 1):
        logger.info(f"[{idx}/{len(plan)}] Starting week={week}  delivery_id={delivery_id}")
        try:
            success = run_pipeline_for_delivery(
                product_id=product_id,
                delivery_id=delivery_id,
                delivery_name=delivery_name,
                week_number=week,
                runner_mode=args.mode,
                batch_size=batch_size,
                retry_failed=args.retry_failed,
            )
        except Exception as exc:
            logger.error(
                f"[{idx}/{len(plan)}] Unexpected exception — week={week}  delivery_id={delivery_id}: {exc}",
                exc_info=True,
            )
            success = False

        if not success:
            # ── HALT: do NOT proceed to the next delivery ─────────────────────
            remaining = len(plan) - idx
            logger.error("")
            logger.error("╔═══════════════════════════════════════════════════════════╗")
            logger.error("║              ⚠  DELIVERY FAILED — HALTING  ⚠              ║")
            logger.error("╠═══════════════════════════════════════════════════════════╣")
            logger.error(f"║  Week         : {week:<42}║")
            logger.error(f"║  Delivery ID  : {str(delivery_id):<42}║")
            logger.error(f"║  Delivery     : {delivery_name[:42]:<42}║")
            logger.error(f"║  Skipping     : {remaining} remaining delivery(s)                  ║")
            logger.error("╠═══════════════════════════════════════════════════════════╣")
            logger.error("║  Processing subsequent weeks with a partial/failed week   ║")
            logger.error("║  could corrupt the patent_documents table. Fix the failed ║")
            logger.error("║  files in delivery_files, then re-run with --retry-failed.║")
            logger.error("╚═══════════════════════════════════════════════════════════╝")
            logger.error("")
            sys.exit(1)

        logger.info(f"[{idx}/{len(plan)}] ✓ Completed week={week}  delivery_id={delivery_id}")

    logger.info("All deliveries processed successfully.")



if __name__ == "__main__":
    main()
