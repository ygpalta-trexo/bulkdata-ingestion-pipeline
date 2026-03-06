# DOCDB Ingestion Pipeline

A high-performance pipeline for parsing, deduplicating, and ingesting massive, highly-nested EPO DOCDB bulk XML files into a normalized PostgreSQL database schema.

## Overview
This project replaces reliance on rate-limited EPO APIs/Espacenet by directly processing raw DOCDB XML data dumps provided by the European Patent Office. It extracts the full scope of bibliographic patent data and links it together through a heavily relational 10-table schema.

### Key Features
- **Automated API Downloading**: Natively connects to the EPO Publication API to list, download, and stream large weekly `DOCDB / XML` ZIP archives down sequentially.
- **Resilient Pipeline Orchestration**: Uses a state machine backed by Postgres (`delivery_files`, `ingestion_checkpoints`) to ensure idempotent behavior. You can safely stop the pipeline at any time and it will instantly resume exactly where it left off, down to the inner zip granularity.
- **Streaming XML Parsing**: Processes massive `.zip` archives containing thousands of XML files using memory-efficient iterparse streams, injecting EPO-specific DTD schemas on the fly to seamlessly decode mathematical and special symbols.
- **Relational Schema + JSONB**: Flattens nested XML (entities like inventors, citations, priority claims, classifications) into 10 structured, heavily indexed PostgreSQL tables. Unhandled extra XML tags are recursively caught and stored as structured dictionaries within a `JSONB` `extra_data` column, ensuring zero data loss without requiring rigid schema alterations.
- **Data Deduplication**: Specifically designed to handle dual namespaced/bare-tag DOCDB anomalies and format fallback cascading (`docdb` > `docdba` > `original`).
- **Data Export capabilities**: Built-in scripts to cleanly extract subsets of rich data out to flattened Multi-Tab Excel spreadsheets for non-technical stakeholders or Product Managers.

## Directory Structure
```
docdb_ingestion/
├── docdb_ingestion/
│   ├── database.py       # Async DB connection, schema init, & bulk UPSERT logic
│   ├── models.py         # Pydantic validation models reflecting the 10-table schema
│   ├── epo_api.py        # EPO REST API Client for downloading deliveries
│   ├── pipeline.py       # Orchestrator state machine combining Downloads, Extractions & Parsing
│   └── stream_processor.py # Core XML extraction & deduplication logic mapping nodes to dicts
├── tests/                # Pytest suites
├── logs/                 # Auto-generated date-wise logging directories
├── docs/                 # Confluence Technical Documentation 
├── .env.example          # Template for required DB connection secrets
├── export_sample_excel.py# Command line utility to extract PM-friendly Excel snapshots
├── query_biblio.py       # Developer utility script for querying the structured database
├── reset_db.py           # Utility to wipe the schema completely
├── setup_db.py           # Utility to initialize the empty schema
├── process_folder.py     # Process ZIP files directly from a folder (for testing/debugging)
└── test_process_folder.py# Test script demonstrating process_folder.py usage
```

## Setup & Installation

**Prerequisites:** Python 3.9+ and PostgreSQL

1. Install dependencies
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure the database connection
```bash
cp .env.example .env
# Edit .env with your local PostgreSQL credentials
```

3. Initialize the database schema
```bash
python setup_db.py
```

## Running the Pipeline

**To orchestrate the automated End-to-End ingestion pipeline:**
The pipeline will query the API, identify pending files, download them into `tmp_downloads`, extract them, parse them, flush row batches to PostgreSQL, clean up temporal artifacts to save disk space, and checkpoint its success.

1. Fetch the latest file manifest from the EPO servers:
```bash
python -m docdb_ingestion.pipeline sync
```

2. Start the automated ingestion loop:
```bash
python -m docdb_ingestion.pipeline run
```

All process output will be saved both to STANDARD OUT and into daily rolling logs located in `logs/<YYYY-MM-DD>/pipeline.log`.

## Data Schema Summary

The pipeline manages a 2-tier master architecture (`application_master` and `document_master`), with 8 associated sub-tables representing complex M:N relationships, plus pipeline tracking.

**State Machine Tracking:**
1. `delivery_files`: External EPO zip volumes and their states (PENDING, DOWNLOADING, EXTRACTED, PARSING, COMPLETED).
2. `ingestion_checkpoints`: Internal zip volume granularity tracking to easily resume half-finished files.

**Core Data Entities:**
1. `application_master`: Tracks the base application filing (Country, Number, Date, JSONB `extra_data`).
2. `document_master`: Tracks all the publications/grants branching off an application. Included fields: `is_grant`, `is_representative`, `date_publ`, JSONB `extra_data`.
3. `parties`: Unified table mapping `APPLICANT`s and `INVENTOR`s with granular sequence numbers and residence countries.
4. `priority_claims`: Historical priority linkages to earlier patent filings.
5. `patent_classifications`: Highly granular IPC and CPC schema codes.
6. `rich_citations_network`: Forward and backward citation linkages to both prior-art patents and general NPL documents.
7. `titles`: Multi-lingual document titles.
8. `abstracts`: Multi-lingual document abstracts.
9. `designation_of_states`: Granular tracking of EPC and PCT state designations (typically from WIPO/EP patent families).
10. `citation_passage_mapping`: Deep linking of citations to specific contextual passages within the referenced documents.

## Processing Files from a Folder

For development, testing, or processing manually downloaded files, you can use `process_folder.py` to process ZIP files directly from any folder without requiring the index.xml parsing:

```bash
# Dry run - process files but don't save to database
python process_folder.py /path/to/folder/with/zips --dry-run --limit 3

# Process and save to database
python process_folder.py /path/to/folder --dsn "postgresql://user:pass@host/db"

# Process and save detailed results to JSON
python process_folder.py /path/to/folder --output-json results.json

# Process with verbose logging
python process_folder.py /path/to/folder --dry-run --verbose
```

**Use Cases:**
- Testing individual ZIP files before full pipeline runs
- Processing manually downloaded or extracted files
- Debugging specific documents or data issues
- Development workflows where you want to modify data and reprocess

**Features:**
- Recursively finds all `.zip` files in the specified folder
- Automatic DTD directory detection (looks for `DTDS` folder alongside ZIPs)
- Comprehensive error handling and reporting
- JSON export of processing results and statistics
- Shows unhandled `extra_data` fields found during processing

## Generating Excel Exports
You can use the built-in export tool to extract rich subsets of the database to flattened Excel sheets for stakeholder review:

```bash
python export_sample_excel.py --limit 100 --output docdb_sample_100.xlsx
```
This extracts the most data-rich rows (those with inventors, classifications, citations, and priorities present) and breaks them out across 6 detailed Excel tabs.

## Testing
To run the automated integration test suite ensuring the database models precisely match the expected XML parsing:
```bash
pytest tests/
```
