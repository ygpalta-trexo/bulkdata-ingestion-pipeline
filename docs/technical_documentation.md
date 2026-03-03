# DOCDB Bulk Ingestion Pipeline - Technical Documentation

## 1. System Overview
The DOCDB Ingestion Pipeline is an automated, robust backend service designed to download, extract, parse, and ingest massive XML data dumps representing patent bibliographies from the European Patent Office (EPO). It translates deeply nested, complex XML objects into a clean, heavily-indexed, 10-table PostgreSQL relational schema without losing unhandled data tags, ensuring maximum analytic flexibility.

```mermaid
graph TD
    EPO[EPO API Server] -->|Zip Stream| Pipe[pipeline.py\nOrchestrator]
    Pipe -->|Extract| Zips[Inner Zips]
    Pipe -->|Manage State| DB_Track[(Postgres\nTracking Tables)]
    Zips -->|Stream Parse| Stream[stream_processor.py]
    Stream -->|Resolve Entities| DTD[EPO DTDs]
    Stream -->|Pydantic Models| DB_Logic[database.py\nBulk Upsert]
    DB_Logic --> DB[(Postgres\nPatent Relational Schema)]
```

### 1.1 Core Architecture Design
The pipeline is designed with the following principles:
- **Resiliency & Idempotency:** The pipeline orchestrates long-running jobs (GBs of zip files) over flaky networks. It maintains its exact operational state inside tracking tables (`delivery_files`, `ingestion_checkpoints`). If a node fails, restarts seamlessly pickup the exact inner zip volume it dropped.
- **Streaming Parsing:** The system uses `lxml.etree.iterparse` to stream the data off the disk rather than loading it into RAM, mapping nodes directly to batch upsert operations.
- **Zero Data Loss via JSONB:** Predictable bibliographical tags (Inventors, CPCs, Citations) map to relational fields. Unpredictable or undocumented XML elements are recursively converted to dictionaries and securely stored in Postgres `JSONB` columns on the parent records (`extra_data`), enabling NoSQL-style querying alongside relational Joins.

## 2. Process Orchestration (ETL Loop)
`pipeline.py` serves as the primary controller.

### 2.1 Sync (`python -m docdb_ingestion.pipeline sync`)
1. Connects to `EPO_API_BASE_URL` using consumer keys to fetch a manifest of available bulk `zip` files for a given delivery.
2. Synchronizes the manifest with the `delivery_files` table, marking newly discovered binaries as `PENDING`.

### 2.2 Run (`python -m docdb_ingestion.pipeline run`)
1. **Download:** Identifies the next `PENDING` file and streams it natively to `./tmp_downloads/` using HTTP chunks.
2. **Extract:** Unzips the master payload (which often contains dozens of internal zips). Once extracted, aggressively deletes the master zip to conserve disk io.
3. **Parse (The Inner Loop):** Iterates over the internal zips.
   - Using the `ingestion_checkpoints` tracker, checks if the internal zip was already successfully processed.
   - Passes the zip into `stream_processor.py`. The processor seamlessly resolves missing XML math/HTML entities (like `&delta;`) by auto-invoking the EPO DTD dictionaries bundled alongside the XML.
   - Yields structured `Pydantic` models that match the DB Tables.
4. **Database Upsert:** Groups extracted records into batches (size=1000) and commits them to Postgres using `ON CONFLICT DO UPDATE` upserts for data safety.
5. **Clean:** Deletes the extracted artifacts and marks the master job `COMPLETED`.

## 3. Database Schema

```mermaid
erDiagram
    APPLICATION_MASTER ||--o{ DOCUMENT_MASTER : has
    DOCUMENT_MASTER ||--o{ PARTIES : has
    DOCUMENT_MASTER ||--o{ PRIORITY_CLAIMS : has
    DOCUMENT_MASTER ||--o{ PATENT_CLASSIFICATIONS : has
    DOCUMENT_MASTER ||--o{ RICH_CITATIONS_NETWORK : cites
    DOCUMENT_MASTER ||--o{ TITLES : has
    DOCUMENT_MASTER ||--o{ ABSTRACTS : has
```

### 3.1 State Tracking Tables
- **`delivery_files`**: Tracks external volumes (e.g., `docdb_xml_bck_202607_001_B.zip`). Columns: `file_id`, `status`.
- **`ingestion_checkpoints`**: Tracks internal zip volumes (e.g., `DOCDB-202607-001-AU-something.zip`). Links back to `delivery_files` for holistic resume states.

### 3.2 Masters (The Hierarchy)
- **`application_master`**: Tracks the unifying patent application filing.
  - Key columns: `app_doc_id` (Primary Key), `app_number`, `app_date`, `extra_data` (JSONB).
- **`document_master`**: Tracks individual publications/grants branching off that application.
  - Key columns: `pub_doc_id` (PK), `app_doc_id` (FK), `is_grant` (Boolean), `date_publ`.

### 3.3 Relational Details (M:N Sub-Tables)
All 8 sub-tables relate via Foreign Key constraints (`ON DELETE CASCADE`) to the `document_master` (`pub_doc_id`):
- `parties`: Consolidates `APPLICANTS`/`INVENTORS`, mapping names and global residences.
- `priority_claims`: Prior art claims (earlier filings this patent claims dates from).
- `patent_classifications`: Assigned taxonomy symbols (IPC, CPC).
- `rich_citations_network`: The complex network graph of NPL references and cited Patent documents.
- `titles` / `abstracts`: Translations of the content.
- `designation_of_states`: Regional legal designations.
- `citation_passage_mapping`: Deep linking for specifically cited passages.

## 4. Dependencies & Setup
Configuration relies heavily on Environment Variables.

### Standard Local Environment
1. Initialize virtual envelope.
2. Configure `.env`:
```ini
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bulk-data
EPO_PRODUCT_ID=14
EPO_DELIVERY_ID=3071
EPO_API_BASE_URL=https://publication.b2b.epo.org/b2b/bulk-downloads/v1/
EPO_TEMP_DIR=./tmp_downloads
```
3. Initialize schemas via `python setup_db.py`.
Centralized auto-rolling logs will instantly route logic tracing to `logs/YYYY-MM-DD/pipeline.log`.

## 5. Available Scripts and Testing Utilities

In addition to the main pipeline, the repository contains several utility scripts for managing the system and inspecting the data.

### 5.1 Infrastructure Scripts
- `python setup_db.py`: Initializes the PostgreSQL database schema. Run this script once when setting up the environment. It safely creates all 10 schema tables, the 2 tracking tables, and dozens of heavily optimized b-tree indexes.
- `python reset_db.py`: **CAUTION**. Drops the entire public schema and permanently deletes all ingested patent data. Rebuilds the empty structure identical to `setup_db.py`. Excellent for destroying corrupted environments during testing.

### 5.2 Extraction & Query Scripts
- `python query_biblio.py`: A CLI development utility designed to perform comprehensive graph lookups of the relational schema. Hardcoded to select a single `pub_doc_id` from the database and print out a JSON representation mapping all M:N subtables. Useful for validating proper schema mapping.
- `python export_sample_excel.py`: An analytics script designed to query a massive randomized sample of the pipeline's data, joining the 10 relational tables back together into flattened objects inside pandas dataframes, and rendering them into an intricately linked 6-tab `.xlsx` Excel spreadsheet. This is heavily utilized for Product Manager and Stakeholder reviews.
  - Usage: `python export_sample_excel.py --limit <ROWS>` (default 100).
- `python inspect_xml.py`: Sandbox script used to physically query the EPO API, download a single zip, navigate into the extracted volume, and print raw chunking streams to Standard Out to analyze true DTD and DOCTYPE xml encodings visually.

### 5.3 General Pipeline Execution
- `python -m docdb_ingestion.pipeline sync`: Fetch newly published datasets from EPO API.
- `python -m docdb_ingestion.pipeline run`: Stream, Parse, Upsert, Checkpoint.
