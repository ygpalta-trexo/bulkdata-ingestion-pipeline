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

Delivery selection
------------------
--mode catchup always rebuilds the full plan from BACKFILE_TIME, so every run
starts from the beginning. Before any EPO or pipeline call it reads
delivery_files once and classifies each planned delivery:

    no rows                → never synced; sync from the EPO API, then process
    rows, some outstanding → sync (cheap and idempotent, and the only way to
                             notice files EPO added since), then let the
                             pipeline work through the pending/failed files
    rows, all COMPLETED    → skip entirely: no sync, no pipeline call, no
                             delivery_files entries, no audit row

That keeps a re-run cheap and stops finished weeks from being re-registered and
re-announced file by file. Pass --force-sync to process a delivery regardless.

--mode latest never skips the sync: the current week is still open, so EPO can
add files to a delivery already seen. It re-checks after the sync instead and
skips the pipeline run when nothing is outstanding, so a repeated cron run does
not write duplicate audit rows or claim work it did not do.

In both modes, a delivery holding FAILED files halts the run unless
--retry-failed is given: the pipeline ignores FAILED rows, so continuing would
report a partial week as complete and let later weeks land on top of it.

Run report
----------
Every non-dry run emails a completion or failure report to EMAIL_RECIPIENT
(suppress with --no-email). Mail configuration is shared with the DRM scripts;
see docdb_ingestion/notifications.py.

Environment variables (all read from .env):
    EPO_FRONTFILE_PRODUCT_ID   — product ID for the front-file (e.g. 3)
    BACKFILE_TIME              — ISO-8601 cutoff; deliveries on or before this
                                 date are skipped (e.g. 2026-02-24T10:50:03.000+01:00)
    EPO_API_BASE_URL           — (optional) override the EPO API base URL
    EPO_TEMP_DIR               — (optional) temp download directory
    PIPELINE_LOG_FILE          — (optional) explicit log file path
    PIPELINE_WORKER_NAME       — (optional) label appended to the default log filename
    DOCDB_BATCH_SIZE           — (optional) upsert batch size passed to the pipeline
    EMAIL_RECIPIENT            — comma-separated addresses for the run report
    EMAIL_CONFIG_HOST          — SMTP host
    EMAIL_CONFIG_PORT          — (optional) SMTP port, default 587
    EMAIL_CONFIG_AUTH_USER     — SMTP user, also used as the From address
    EMAIL_CONFIG_AUTH_PASSWORD — SMTP password
"""

import argparse
import html
import logging
import os
import re
import socket
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests
from dateutil.parser import isoparse
from dotenv import load_dotenv

# ── Import the existing pipeline machinery ────────────────────────────────────
from docdb_ingestion.database import DatabaseManager, get_dsn_from_env
from docdb_ingestion.notifications import get_default_recipient, send_email
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

# ── Delivery states derived from delivery_files, used by catch-up ─────────────
STATE_NEW = "NEW"                  # no rows in delivery_files — never synced
STATE_OUTSTANDING = "OUTSTANDING"  # synced, still has non-COMPLETED files
STATE_DONE = "DONE"                # synced, every file COMPLETED

STATE_LABELS = {STATE_NEW: "new", STATE_OUTSTANDING: "pending", STATE_DONE: "done"}


class RunAborted(Exception):
    """Configuration problem that stops the run before any delivery is touched."""


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


def classify_deliveries(
    counts_by_delivery: Dict[int, Dict[str, int]],
    delivery_ids: List[int],
) -> Dict[int, Tuple[str, dict]]:
    """
    Map each delivery ID to (state, file-status counts) using a delivery_files
    snapshot. A delivery absent from the snapshot has never been synced, which
    is why absence maps to STATE_NEW rather than STATE_DONE.
    """
    classified: Dict[int, Tuple[str, dict]] = {}
    for delivery_id in delivery_ids:
        counts = counts_by_delivery.get(delivery_id)
        if not counts:
            classified[delivery_id] = (STATE_NEW, {})
        elif counts["outstanding"] == 0:
            classified[delivery_id] = (STATE_DONE, counts)
        else:
            classified[delivery_id] = (STATE_OUTSTANDING, counts)
    return classified


def load_delivery_states(product_id: int, delivery_ids: List[int]) -> Dict[int, Tuple[str, dict]]:
    """Read delivery_files once and classify every planned delivery.

    Uses its own short-lived, read-only connection: the ingestion run that
    follows can take hours, each PipelineOrchestrator opens a connection of its
    own, and planning must not run the schema bootstrap — a --dry-run would
    otherwise issue the full CREATE TABLE/partition DDL.
    """
    db = DatabaseManager(get_dsn_from_env())
    db.connect(init_schema=False)
    try:
        counts = db.get_delivery_status_summary(product_id, delivery_ids)
    finally:
        db.close()
    return classify_deliveries(counts, delivery_ids)


def process_delivery(
    product_id: int,
    delivery_id: int,
    delivery_name: str,
    week_number: str = None,
    runner_mode: str = None,
    batch_size: int = None,
    retry_failed: bool = False,
    skip_when_complete: bool = False,
) -> Tuple[bool, dict, bool]:
    """Sync one delivery from the EPO API, then run the pipeline over its files.

    The sync always runs for a delivery we intend to process. It is idempotent
    (INSERT ... ON CONFLICT DO NOTHING), and it is the only way to notice files
    EPO added after an earlier partial sync — without it those files would never
    be registered and the delivery would silently settle as complete without them.

    :param skip_when_complete: after the sync, skip the pipeline run if every
        file is already COMPLETED. Used by --mode latest, which re-syncs the open
        week on every run but must not re-announce it or write a duplicate audit
        row when nothing changed.

    Returns (success, stats, ran). ran is False when the delivery was skipped as
    already complete; stats is the orchestrator's per-run tally
    (see PipelineOrchestrator._set_run_stats).
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

    if skip_when_complete:
        # Re-read after the sync: any file EPO added above is registered by now.
        counts = orchestrator.db.get_delivery_status_summary(
            product_id, [delivery_id]
        ).get(delivery_id)
        if counts and counts["outstanding"] == 0:
            logger.info(
                f"Nothing outstanding after sync — all {counts['total']} file(s) "
                f"already COMPLETED. Skipping pipeline run."
            )
            return True, {"total_files": counts["total"]}, False

    success = orchestrator.run(retry_failed=retry_failed, batch_size_arg=batch_size)
    return success, dict(orchestrator.last_run_stats), True


# ── Run report ────────────────────────────────────────────────────────────────

@dataclass
class RunSummary:
    """Everything the run report needs, accumulated as the run progresses."""

    mode: str
    started_at: datetime
    log_file: str = ""
    product_id: int = None
    backfile_cutoff: str = ""
    planned: int = 0
    skipped: List[dict] = field(default_factory=list)        # already fully COMPLETED
    processed: List[dict] = field(default_factory=list)      # ran to success in this run
    failed: dict = None                                      # the delivery that halted the run
    not_attempted: List[dict] = field(default_factory=list)  # plan entries after the halt
    fatal_error: str = ""
    finished_at: datetime = None

    @property
    def status(self) -> str:
        return "FAILED" if (self.fatal_error or self.failed) else "COMPLETED"

    @property
    def duration(self) -> str:
        end = self.finished_at or datetime.now()
        seconds = max(int((end - self.started_at).total_seconds()), 0)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours}h {minutes}m {secs}s"

    def doc_totals(self) -> Dict[str, int]:
        """Documents written by this run, including the delivery that failed.

        A delivery that fails on its last volume has still written everything
        from the earlier ones. Leaving that out of the totals would tell an
        operator the database was untouched when it is in fact half-loaded —
        the one fact that decides whether a re-run is safe.
        """
        totals = {"upserted": 0, "deleted": 0, "skipped": 0}
        for entry in self.processed + ([self.failed] if self.failed else []):
            stats = entry.get("stats") or {}
            totals["upserted"] += stats.get("docs_upserted", 0)
            totals["deleted"] += stats.get("docs_deleted", 0)
            totals["skipped"] += stats.get("docs_skipped", 0)
        return totals


def _rows(pairs: List[Tuple[str, str]]) -> str:
    """Render label/value pairs as a two-column HTML table."""
    cells = "".join(
        f"<tr><td style='padding:2px 12px 2px 0; vertical-align:top;'><b>{html.escape(str(label))}</b></td>"
        f"<td style='padding:2px 0;'>{html.escape(str(value))}</td></tr>"
        for label, value in pairs
    )
    return f"<table style='border-collapse:collapse;'>{cells}</table>"


def _delivery_table(entries: List[dict], show_files: bool = False, show_docs: bool = False) -> str:
    """Render processed/skipped/not-attempted deliveries as an HTML table.

    Skipped deliveries carry a file count but no doc counts — showing the file
    count is what justifies the skip to whoever reads the report.
    """
    if not entries:
        return "<p style='margin:0 0 0 1em;'>None.</p>"

    head = ["Week", "Delivery ID", "Delivery"]
    if show_files:
        head += ["Files"]
    if show_docs:
        head += ["Upserted", "Deleted", "Skipped"]

    header = "".join(
        f"<th style='text-align:left; padding:3px 10px 3px 0; border-bottom:1px solid #999;'>{col}</th>"
        for col in head
    )

    body = ""
    for entry in entries:
        stats = entry.get("stats") or {}
        values = [
            entry.get("week", ""),
            entry.get("delivery_id", ""),
            entry.get("delivery_name", ""),
        ]
        if show_files:
            values += [stats.get("total_files", entry.get("total_files", ""))]
        if show_docs:
            values += [
                f"{stats.get('docs_upserted', 0):,}",
                f"{stats.get('docs_deleted', 0):,}",
                f"{stats.get('docs_skipped', 0):,}",
            ]
        body += "<tr>" + "".join(
            f"<td style='padding:3px 10px 3px 0; border-bottom:1px solid #eee;'>{html.escape(str(v))}</td>"
            for v in values
        ) + "</tr>"

    return (
        "<table style='border-collapse:collapse; font-size:11pt;'>"
        f"<tr>{header}</tr>{body}</table>"
    )


def build_report_email(summary: RunSummary) -> Tuple[str, str]:
    """Return (subject, html_body) describing one front-file runner execution."""
    totals = summary.doc_totals()

    if summary.fatal_error:
        subject = f"DOCDB front-file ingestion FAILED ({summary.mode}) — run aborted"
        headline = "The front-file ingestion run was aborted before completing."
    elif summary.failed:
        subject = (
            f"DOCDB front-file ingestion FAILED ({summary.mode}) — "
            f"week {summary.failed.get('week', '?')}"
        )
        headline = (
            f"The front-file ingestion run halted on week {summary.failed.get('week', '?')}. "
            "Later deliveries were not attempted."
        )
    elif summary.processed:
        subject = (
            f"DOCDB front-file ingestion COMPLETED ({summary.mode}) — "
            f"{len(summary.processed)} delivery(s) processed"
        )
        headline = "The front-file ingestion run completed successfully."
    else:
        subject = f"DOCDB front-file ingestion COMPLETED ({summary.mode}) — nothing to do"
        headline = "The front-file ingestion run completed. There was no outstanding work."

    run_details = _rows(
        [
            ("Status", summary.status),
            ("Mode", summary.mode),
            ("Host", socket.gethostname()),
            ("Product ID", summary.product_id if summary.product_id is not None else "—"),
            ("Backfile cutoff", summary.backfile_cutoff or "—"),
            ("Started", summary.started_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Finished", (summary.finished_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")),
            ("Duration", summary.duration),
            ("Log file", summary.log_file or "—"),
        ]
    )

    counts = _rows(
        [
            ("Deliveries planned", summary.planned),
            ("Skipped (already complete)", len(summary.skipped)),
            ("Processed in this run", len(summary.processed)),
            ("Failed", 1 if summary.failed else 0),
            ("Not attempted", len(summary.not_attempted)),
            ("Documents upserted", f"{totals['upserted']:,}"),
            ("Documents deleted", f"{totals['deleted']:,}"),
            ("Documents skipped", f"{totals['skipped']:,}"),
            ("Doc counts include", "the failed delivery's partial work" if summary.failed
                                   else "all deliveries processed"),
        ]
    )

    failure_section = ""
    if summary.fatal_error:
        failure_section = (
            "<b>Reason for failure</b>"
            f"<p style='margin:4px 0 0 1em; color:#a00;'>{html.escape(summary.fatal_error)}</p><br>"
        )
    elif summary.failed:
        failed_stats = summary.failed.get("stats") or {}
        failed_files = failed_stats.get("failed_files") or []
        file_lines = "".join(
            f"<p style='margin:0 0 0 2em;'>&bull; [{html.escape(str(f.get('file_id', '')))}] "
            f"{html.escape(str(f.get('filename', '')))} — {html.escape(str(f.get('error', '')))}</p>"
            for f in failed_files
        )
        failure_section = (
            "<b>Failed delivery</b>"
            + _rows(
                [
                    ("Week", summary.failed.get("week", "")),
                    ("Delivery ID", summary.failed.get("delivery_id", "")),
                    ("Delivery", summary.failed.get("delivery_name", "")),
                    ("Error", summary.failed.get("error", "")),
                    (
                        "Written before the failure",
                        f"{failed_stats.get('docs_upserted', 0):,} upserted, "
                        f"{failed_stats.get('docs_deleted', 0):,} deleted",
                    ),
                ]
            )
            + (f"<p style='margin:6px 0 0 1em;'><b>Failed files</b></p>{file_lines}" if file_lines else "")
            + "<p>Fix the failed files in delivery_files, then re-run with "
              "<code>--retry-failed</code>. Later weeks were deliberately not processed: "
              "ingesting them on top of a partial week could corrupt patent_documents.</p><br>"
        )

    body = f"""
    <div style="font-family: Calibri, sans-serif; font-size: 12pt;">
    <p>{html.escape(headline)}</p>

    <b>Run details</b>
    {run_details}
    <br>

    <b>Summary</b>
    {counts}
    <br>

    {failure_section}

    <b>Deliveries processed in this run</b>
    {_delivery_table(summary.processed, show_files=True, show_docs=True)}
    <br>

    <b>Deliveries skipped (already complete)</b>
    {_delivery_table(summary.skipped, show_files=True)}
    <br>

    <b>Deliveries not attempted</b>
    {_delivery_table(summary.not_attempted)}
    <br>

    <p style="color:#666; font-size:10pt;">Sent automatically by frontfile_runner.py.</p>
    </div>
    """
    return subject, body


def send_run_report(summary: RunSummary) -> bool:
    """Email the run report. Never raises — a mail failure must not fail the run."""
    try:
        recipient = get_default_recipient()
        if not recipient:
            logger.warning("EMAIL_RECIPIENT is not set — skipping run report email.")
            return False
        subject, body = build_report_email(summary)
        return send_email(recipient, subject, body)
    except Exception as exc:
        logger.error(f"Could not build or send the run report email: {exc}", exc_info=True)
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args():
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
        "--force-sync",
        action="store_true",
        help=(
            "catchup only: re-sync every planned delivery from the EPO API and re-run it, "
            "even if all of its files are already COMPLETED."
        ),
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Do not send the completion/failure report email for this run.",
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
    return parser.parse_args()


def _setup_logging(args) -> str:
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
    return log_file


def _plan_entries(plan_slice) -> List[dict]:
    """Summary rows for a slice of the execution plan."""
    return [
        {"week": week, "delivery_id": delivery_id, "delivery_name": delivery_name}
        for week, delivery_name, delivery_id, _pub_date in plan_slice
    ]


def _log_halt_banner(week, delivery_id, delivery_name, remaining, reason):
    logger.error("")
    logger.error("╔═══════════════════════════════════════════════════════════╗")
    logger.error("║              ⚠  DELIVERY FAILED — HALTING  ⚠              ║")
    logger.error("╠═══════════════════════════════════════════════════════════╣")
    logger.error(f"║  Week         : {week:<42}║")
    logger.error(f"║  Delivery ID  : {str(delivery_id):<42}║")
    logger.error(f"║  Delivery     : {delivery_name[:42]:<42}║")
    logger.error(f"║  Reason       : {reason[:42]:<42}║")
    logger.error(f"║  Skipping     : {remaining} remaining delivery(s)                  ║")
    logger.error("╠═══════════════════════════════════════════════════════════╣")
    logger.error("║  Processing subsequent weeks with a partial/failed week   ║")
    logger.error("║  could corrupt the patent_documents table. Fix the failed ║")
    logger.error("║  files in delivery_files, then re-run with --retry-failed.║")
    logger.error("╚═══════════════════════════════════════════════════════════╝")
    logger.error("")


def _execute(args, summary: RunSummary) -> int:
    """Build the plan and process it. Returns the process exit code."""
    # ── Read required env vars ─────────────────────────────────────────────────
    product_id_str = os.environ.get("EPO_FRONTFILE_PRODUCT_ID")
    if not product_id_str:
        raise RunAborted("EPO_FRONTFILE_PRODUCT_ID is not set in .env.")
    product_id = int(product_id_str)
    summary.product_id = product_id

    backfile_time_str = os.environ.get("BACKFILE_TIME")
    if not backfile_time_str:
        raise RunAborted("BACKFILE_TIME is not set in .env.")
    backfile_cutoff = isoparse(backfile_time_str)
    summary.backfile_cutoff = backfile_cutoff.isoformat()
    logger.info(f"Backfile cutoff: {backfile_cutoff.isoformat()}")

    # ── Fetch + filter + group ─────────────────────────────────────────────────
    all_deliveries = fetch_all_deliveries(product_id)
    logger.info(f"Total deliveries returned by API: {len(all_deliveries)}")

    grouped = filter_and_group_deliveries(all_deliveries, backfile_cutoff)
    logger.info(f"Weeks to process after filtering: {len(grouped)}  ({', '.join(grouped.keys())})")

    if not grouped:
        logger.info("No new weeks to process. Exiting.")
        return 0

    # ── Apply mode ─────────────────────────────────────────────────────────────
    if args.mode == "latest":
        # Keep only the last week
        last_week = list(grouped.keys())[-1]
        grouped = {last_week: grouped[last_week]}
        logger.info(f"Mode=latest: limiting to week {last_week}.")

    plan = build_execution_plan(grouped)
    summary.planned = len(plan)

    # ── Classify against delivery_files ────────────────────────────────────────
    # Both modes classify. catchup skips a finished delivery outright; latest
    # still re-syncs it first, because the current week is open and EPO can add
    # files to a delivery we have already seen. Either way the FAILED-file guard
    # below needs these counts.
    states = load_delivery_states(product_id, [entry[2] for entry in plan])
    tally = defaultdict(int)
    for state, _counts in states.values():
        tally[state] += 1
    logger.info(
        f"Delivery states from delivery_files: "
        f"new={tally[STATE_NEW]}  outstanding={tally[STATE_OUTSTANDING]}  "
        f"already complete={tally[STATE_DONE]}"
    )

    # ── Print execution plan (always shown, even without --dry-run) ────────────
    logger.info("")
    logger.info("═══ Execution Plan ═══════════════════════════════════════════")
    for idx, (week, delivery_name, delivery_id, pub_date) in enumerate(plan, 1):
        state, counts = states.get(delivery_id, (None, {}))
        marker = f"[{STATE_LABELS[state]:<7}] " if state else ""
        detail = (
            f"  ({counts['completed']}/{counts['total']} files done)" if counts else ""
        )
        logger.info(
            f"  [{idx:02d}] {marker}week={week}  delivery_id={delivery_id}"
            f"  pub={pub_date[:10]}  name={delivery_name}{detail}"
        )
    logger.info("══════════════════════════════════════════════════════════════")
    logger.info("")

    if args.dry_run:
        logger.info("--dry-run flag set. Exiting without processing.")
        return 0

    # ── Execute ────────────────────────────────────────────────────────────────
    batch_size = args.batch_size or int(os.environ.get("DOCDB_BATCH_SIZE", "1000"))

    idx = 0
    try:
        for idx, (week, delivery_name, delivery_id, _pub_date) in enumerate(plan, 1):
            entry = {"week": week, "delivery_id": delivery_id, "delivery_name": delivery_name}
            state, counts = states.get(delivery_id, (STATE_NEW, {}))

            # catchup: a finished delivery is skipped outright — no EPO call, no
            # delivery_files entries, no pipeline run, no audit row. latest falls
            # through and re-syncs instead, then skips inside process_delivery if
            # the sync turned up nothing new.
            if state == STATE_DONE and args.mode == "catchup" and not args.force_sync:
                logger.info(
                    f"[{idx}/{len(plan)}] ✓ Skipping week={week}  delivery_id={delivery_id} — "
                    f"all {counts['total']} file(s) already COMPLETED."
                )
                entry["total_files"] = counts["total"]
                summary.skipped.append(entry)
                continue

            # A delivery still holding FAILED files cannot be completed without
            # --retry-failed: run() ignores FAILED rows and would report the week
            # as clean, letting the runner move on with a partial week.
            if counts.get("failed") and not args.retry_failed:
                reason = f"{counts['failed']} file(s) in FAILED state; --retry-failed not set"
                logger.error(f"[{idx}/{len(plan)}] {reason} — week={week} delivery_id={delivery_id}")
                entry["error"] = reason
                summary.failed = entry
                summary.not_attempted = _plan_entries(plan[idx:])
                _log_halt_banner(week, delivery_id, delivery_name, len(plan) - idx, reason)
                return 1

            logger.info(f"[{idx}/{len(plan)}] Starting week={week}  delivery_id={delivery_id}")
            stats: dict = {}
            ran = True
            try:
                success, stats, ran = process_delivery(
                    product_id=product_id,
                    delivery_id=delivery_id,
                    delivery_name=delivery_name,
                    week_number=week,
                    runner_mode=args.mode,
                    batch_size=batch_size,
                    retry_failed=args.retry_failed,
                    # latest re-syncs the open week every run; only run the
                    # pipeline if that sync actually turned up outstanding work.
                    skip_when_complete=(args.mode == "latest" and not args.force_sync),
                )
            except Exception as exc:
                logger.error(
                    f"[{idx}/{len(plan)}] Unexpected exception — week={week}  delivery_id={delivery_id}: {exc}",
                    exc_info=True,
                )
                success = False
                entry["error"] = f"Unexpected exception: {exc}"

            if success and not ran:
                logger.info(
                    f"[{idx}/{len(plan)}] ✓ Skipping week={week}  delivery_id={delivery_id} — "
                    f"nothing outstanding after sync."
                )
                entry["total_files"] = stats.get("total_files", counts.get("total", ""))
                summary.skipped.append(entry)
                continue

            entry["stats"] = stats

            if not success:
                # ── HALT: do NOT proceed to the next delivery ─────────────────
                entry.setdefault(
                    "error",
                    "; ".join(
                        f"{f.get('filename')}: {f.get('error')}"
                        for f in stats.get("failed_files", [])
                    )
                    or "One or more files failed.",
                )
                summary.failed = entry
                summary.not_attempted = _plan_entries(plan[idx:])
                _log_halt_banner(
                    week, delivery_id, delivery_name, len(plan) - idx, "file processing failed"
                )
                return 1

            summary.processed.append(entry)
            logger.info(f"[{idx}/{len(plan)}] ✓ Completed week={week}  delivery_id={delivery_id}")
    except Exception:
        # An error escaping the per-delivery handler (or raised between
        # deliveries) still leaves the rest of the plan untouched. Record it so
        # the report's arithmetic closes instead of claiming nothing was left.
        summary.not_attempted = _plan_entries(plan[max(idx - 1, 0):])
        raise

    logger.info("All deliveries processed successfully.")
    return 0


def main():
    load_dotenv()
    args = _parse_args()
    log_file = _setup_logging(args)

    summary = RunSummary(
        mode=args.mode,
        started_at=datetime.now(),
        log_file=log_file,
    )
    logger.info(f"Front-file runner started. Log: {log_file}  mode={args.mode}")

    try:
        exit_code = _execute(args, summary)
    except RunAborted as exc:
        logger.error(f"{exc} Aborting.")
        summary.fatal_error = str(exc)
        exit_code = 1
    except Exception as exc:
        logger.error(f"Front-file runner failed with an unhandled error: {exc}", exc_info=True)
        summary.fatal_error = f"Unhandled error: {exc}"
        exit_code = 1

    summary.finished_at = datetime.now()

    # The report is the only signal an unattended cron run gives, so it is sent
    # for success and failure alike. A dry run reports nothing: it changed nothing.
    if args.dry_run:
        logger.info("Dry run — no report email sent.")
    elif args.no_email:
        logger.info("--no-email set — no report email sent.")
    else:
        send_run_report(summary)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
