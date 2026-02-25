# DOCDB Ingestion Pipeline

A high-performance pipeline for parsing, deduplicating, and ingesting massive, highly-nested EPO DOCDB bulk XML files into a normalized PostgreSQL database schema.

## Overview
This project replaces reliance on rate-limited EPO APIs/Espacenet by directly processing raw DOCDB XML data dumps provided by the European Patent Office. It extracts the full scope of bibliographic patent data and links it together through a heavily relational 10-table schema.

### Key Features
- **Streaming XML Parsing**: Processes massive `.zip` archives containing thousands of XML files using memory-efficient iterparse streams, enabling it to handle the ~1.5GB raw data dumps on normal hardware.
- **Relational Schema**: Flattens nested XML (entities like inventors, citations, priority claims, classifications) into 10 structured, heavily indexed PostgreSQL tables. 
- **Data Deduplication**: Specifically designed to handle dual namespaced/bare-tag DOCDB anomalies and format fallback cascading (`docdb` > `docdba` > `original`).
- **Idempotent Upserts**: Safely resume interrupted ingestions without corrupting data or hitting unique constraint errors utilizing transactional `ON CONFLICT DO UPDATE` patterns.
- **Data Export capabilities**: Built-in scripts to cleanly extract subsets of rich data out to flattened Multi-Tab Excel spreadsheets for non-technical stakeholders or Product Managers.

## Directory Structure
```
docdb_ingestion/
├── docdb_ingestion/
│   ├── database.py       # Async DB connection, schema init, & bulk UPSERT logic
│   ├── models.py         # Pydantic validation models reflecting the 10-table schema
│   └── stream_processor.py # Core XML extraction & deduplication logic mapping nodes to dicts
├── tests/                # Pytest suites
├── .env.example          # Template for required DB connection secrets
├── export_sample_excel.py# Command line utility to extract PM-friendly Excel snapshots
├── main.py               # Main CLI entrypoint for the ingestion pipeline
├── query_biblio.py       # Developer utility script for querying the structured database
├── reset_db.py           # Utility to wipe the schema completely
└── setup_db.py           # Utility to initialize the empty schema
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

**To ingest DOCDB XML data:**
Point the script directly at the folder containing the downloaded `.zip` files (do not extract them manually, the pipeline handles ZIP streaming directly).

```bash
python main.py /path/to/extracted/docdb_folder --limit 1000
```
*Optional Arguments:*
- `--dry-run`: Parse everything and hit validation, but do not actually write to the database.
- `--limit X`: Process a maximum subset of XML files for testing.
- `--resume`: Skip files that have already been recorded in the `processed_files` tracking table.

## Data Schema Summary

The pipeline manages a 2-tier master architecture (`application_master` and `document_master`), with 8 associated sub-tables representing complex M:N relationships:

1. `application_master`: Tracks the base application filing (Country, Number, Date).
2. `document_master`: Tracks all the publications/grants branching off an application. Included fields: `is_grant`, `is_representative`, `date_publ`.
3. `parties`: Unified table mapping `APPLICANT`s and `INVENTOR`s with granular sequence numbers and residence countries.
4. `priority_claims`: Historical priority linkages to earlier patent filings.
5. `patent_classifications`: Highly granular IPC and CPC schema codes.
6. `rich_citations_network`: Forward and backward citation linkages to both prior-art patents and general NPL documents.
7. `titles`: Multi-lingual document titles.
8. `abstracts`: Multi-lingual document abstracts.
9. `designation_of_states`: Granular tracking of EPC and PCT state designations (typically from WIPO/EP patent families).
10. `citation_passage_mapping`: Deep linking of citations to specific contextual passages within the referenced documents.

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
