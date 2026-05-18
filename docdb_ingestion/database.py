import logging
import os
from typing import Any, Dict, List

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import ExchangeDocument

logger = logging.getLogger(__name__)

DEDICATED_COUNTRY_PARTITIONS = {"US", "EP", "WO", "CN", "JP", "KR", "DE", "GB", "FR"}
STAGE_COLUMNS = [
    "stage_key",
    "stage_seq",
    "country",
    "pub_doc_id",
    "doc_number",
    "kind_code",
    "extended_kind",
    "date_publ",
    "family_id",
    "is_representative",
    "is_grant",
    "originating_office",
    "date_added_docdb",
    "date_last_exchange",
    "app_doc_id",
    "app_country",
    "app_number",
    "app_kind_code",
    "app_date",
    "source_status",
    "app_extra_data",
    "pub_extra_data",
    "parties",
    "priorities",
    "classifications",
    "citations",
    "texts",
    "designations",
    "availability_dates",
]


def get_dsn_from_env() -> str:
    """
    Construct DSN explicitly from .env components to prevent
    stray DATABASE_URL environment variables in the user's shell
    from overriding the intended environment mapping.
    """
    load_dotenv(override=True)

    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "password")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB")

    if dbname:
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    return os.getenv(
        "DATABASE_URL",
        f"postgresql://{user}:{password}@{host}:{port}/docdb",
    )


class DatabaseManager:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None

    def connect(self):
        self.conn = psycopg.connect(self.dsn, row_factory=dict_row)
        self.init_schema()

    def close(self):
        if self.conn:
            self.conn.close()

    def init_schema(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_files (
                    file_id BIGINT PRIMARY KEY,
                    product_id INT,
                    delivery_id INT,
                    filename VARCHAR(500),
                    status VARCHAR(20) DEFAULT 'PENDING',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_status ON delivery_files (status);"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
                    filename TEXT PRIMARY KEY,
                    main_zip_id INTEGER,
                    main_zip_filename TEXT,
                    first_doc_number TEXT,
                    last_doc_number TEXT,
                    status TEXT,
                    processed_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON ingestion_checkpoints (status);"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS patent_documents (
                    country VARCHAR(2) NOT NULL,
                    pub_doc_id VARCHAR(50) NOT NULL,

                    doc_number VARCHAR(50) NOT NULL,
                    kind_code VARCHAR(5) NOT NULL,
                    extended_kind VARCHAR(10),
                    date_publ DATE,
                    family_id VARCHAR(50),
                    is_representative BOOLEAN,
                    is_grant BOOLEAN NOT NULL DEFAULT FALSE,
                    originating_office VARCHAR(10),
                    date_added_docdb DATE,
                    date_last_exchange DATE,

                    app_doc_id VARCHAR(50),
                    app_country VARCHAR(2),
                    app_number VARCHAR(50),
                    app_kind_code VARCHAR(5),
                    app_date DATE,

                    source_status VARCHAR(5) DEFAULT 'C',
                    app_extra_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    pub_extra_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    parties JSONB NOT NULL DEFAULT '{"applicants":[],"inventors":[],"others":[]}'::jsonb,
                    priorities JSONB NOT NULL DEFAULT '[]'::jsonb,
                    classifications JSONB NOT NULL DEFAULT '[]'::jsonb,
                    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
                    texts JSONB NOT NULL DEFAULT '[]'::jsonb,
                    designations JSONB NOT NULL DEFAULT '[]'::jsonb,
                    availability_dates JSONB NOT NULL DEFAULT '[]'::jsonb,

                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                    PRIMARY KEY (country, pub_doc_id),

                    CONSTRAINT chk_parties_object
                        CHECK (jsonb_typeof(parties) = 'object'),
                    CONSTRAINT chk_priorities_array
                        CHECK (jsonb_typeof(priorities) = 'array'),
                    CONSTRAINT chk_classifications_array
                        CHECK (jsonb_typeof(classifications) = 'array'),
                    CONSTRAINT chk_citations_array
                        CHECK (jsonb_typeof(citations) = 'array'),
                    CONSTRAINT chk_texts_array
                        CHECK (jsonb_typeof(texts) = 'array'),
                    CONSTRAINT chk_designations_array
                        CHECK (jsonb_typeof(designations) = 'array'),
                    CONSTRAINT chk_availability_dates_array
                        CHECK (jsonb_typeof(availability_dates) = 'array'),
                    CONSTRAINT chk_app_extra_data_object
                        CHECK (jsonb_typeof(app_extra_data) = 'object'),
                    CONSTRAINT chk_pub_extra_data_object
                        CHECK (jsonb_typeof(pub_extra_data) = 'object')
                ) PARTITION BY LIST (country);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS patent_documents_default
                    PARTITION OF patent_documents DEFAULT;
                """
            )
            cur.execute(
                """
                CREATE UNLOGGED TABLE IF NOT EXISTS patent_documents_stage (
                    stage_key TEXT NOT NULL,
                    stage_seq BIGINT NOT NULL,
                    country VARCHAR(2),
                    pub_doc_id VARCHAR(50),
                    doc_number VARCHAR(50),
                    kind_code VARCHAR(5),
                    extended_kind VARCHAR(10),
                    date_publ DATE,
                    family_id VARCHAR(50),
                    is_representative BOOLEAN,
                    is_grant BOOLEAN,
                    originating_office VARCHAR(10),
                    date_added_docdb DATE,
                    date_last_exchange DATE,
                    app_doc_id VARCHAR(50),
                    app_country VARCHAR(2),
                    app_number VARCHAR(50),
                    app_kind_code VARCHAR(5),
                    app_date DATE,
                    source_status VARCHAR(5),
                    app_extra_data JSONB,
                    pub_extra_data JSONB,
                    parties JSONB,
                    priorities JSONB,
                    classifications JSONB,
                    citations JSONB,
                    texts JSONB,
                    designations JSONB,
                    availability_dates JSONB
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_patent_documents_stage_key
                    ON patent_documents_stage (stage_key);
                """
            )

            for country in sorted(DEDICATED_COUNTRY_PARTITIONS):
                self._ensure_country_partition(cur, country)

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_patent_documents_country_doc_number
                    ON patent_documents (country, doc_number);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_patent_documents_country_app_number
                    ON patent_documents (app_country, app_number);
                """
            )
            self.conn.commit()

    def _ensure_country_partition(self, cur, country: str):
        normalized_country = (country or "").strip().upper()
        if not normalized_country:
            return

        if normalized_country not in DEDICATED_COUNTRY_PARTITIONS:
            return

        partition_name = f"patent_documents_{normalized_country.lower()}"
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {partition}
                PARTITION OF patent_documents
                FOR VALUES IN ({country});
                """
            ).format(
                partition=sql.Identifier(partition_name),
                country=sql.Literal(normalized_country),
            )
        )

    def is_file_processed(self, filename: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM ingestion_checkpoints WHERE filename = %s",
                (filename,),
            )
            res = cur.fetchone()
            return res and res["status"] == "COMPLETED"

    def mark_file_started(self, filename: str, main_zip_id: int = None, main_zip_filename: str = None):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_checkpoints (
                    filename, main_zip_id, main_zip_filename,
                    first_doc_number, last_doc_number, status, processed_at
                )
                VALUES (%s, %s, %s, NULL, NULL, 'STARTED', NOW())
                ON CONFLICT (filename) DO UPDATE SET
                    status = 'STARTED',
                    main_zip_id = EXCLUDED.main_zip_id,
                    main_zip_filename = EXCLUDED.main_zip_filename,
                    first_doc_number = NULL,
                    last_doc_number = NULL,
                    processed_at = NOW();
                """,
                (filename, main_zip_id, main_zip_filename),
            )
            self.conn.commit()

    def mark_file_completed(self, filename: str, first_doc_number: str = None, last_doc_number: str = None):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_checkpoints
                SET
                    status = 'COMPLETED',
                    first_doc_number = %s,
                    last_doc_number = %s,
                    processed_at = NOW()
                WHERE filename = %s;
                """,
                (first_doc_number, last_doc_number, filename),
            )
            self.conn.commit()

    def sync_delivery_files(self, product_id: int, delivery_id: int, files_data: List[dict]):
        with self.conn.cursor() as cur:
            for item in files_data:
                cur.execute(
                    """
                    INSERT INTO delivery_files (file_id, product_id, delivery_id, filename, status)
                    VALUES (%s, %s, %s, %s, 'PENDING')
                    ON CONFLICT (file_id) DO NOTHING;
                    """,
                    (item["file_id"], product_id, delivery_id, item["filename"]),
                )
            self.conn.commit()

    def get_actionable_files(self, product_id: int, delivery_id: int) -> List[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM delivery_files
                WHERE product_id = %s AND delivery_id = %s
                  AND status NOT IN ('COMPLETED', 'FAILED')
                ORDER BY file_id ASC;
                """,
                (product_id, delivery_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_all_delivery_files(self, product_id: int, delivery_id: int) -> List[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM delivery_files
                WHERE product_id = %s AND delivery_id = %s
                ORDER BY file_id ASC;
                """,
                (product_id, delivery_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def update_file_status(self, file_id: int, status: str, error_message: str = None):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_files
                SET status = %s, error_message = %s, updated_at = NOW()
                WHERE file_id = %s;
                """,
                (status, error_message, file_id),
            )
            self.conn.commit()

    def _build_parties_payload(self, doc: ExchangeDocument) -> Dict[str, List[Dict[str, Any]]]:
        payload = {"applicants": [], "inventors": [], "others": []}

        for party in doc.parties:
            party_payload = {
                "name": party.party_name,
                "format": party.format_type,
                "sequence": party.sequence,
                "residence": party.residence,
                "address_text": party.address_text,
            }
            if party.party_type == "APPLICANT":
                payload["applicants"].append(party_payload)
            elif party.party_type == "INVENTOR":
                payload["inventors"].append(party_payload)
            else:
                party_payload["party_type"] = party.party_type
                payload["others"].append(party_payload)

        return payload

    def _build_document_row(self, doc: ExchangeDocument) -> Dict[str, Any]:
        row = {
            "country": doc.pub_master.country,
            "pub_doc_id": doc.pub_master.pub_doc_id,
            "doc_number": doc.pub_master.doc_number,
            "kind_code": doc.pub_master.kind_code,
            "extended_kind": doc.pub_master.extended_kind,
            "date_publ": doc.pub_master.date_publ,
            "family_id": doc.pub_master.family_id,
            "is_representative": doc.pub_master.is_representative,
            "is_grant": doc.pub_master.is_grant,
            "originating_office": doc.pub_master.originating_office,
            "date_added_docdb": doc.pub_master.date_added_docdb,
            "date_last_exchange": doc.pub_master.date_last_exchange,
            "app_doc_id": doc.app_master.app_doc_id,
            "app_country": doc.app_master.app_country,
            "app_number": doc.app_master.app_number,
            "app_kind_code": doc.app_master.app_kind_code,
            "app_date": doc.app_master.app_date,
            "source_status": doc.operation,
            "app_extra_data": Jsonb(doc.app_master.extra_data or {}),
            "pub_extra_data": Jsonb(doc.pub_master.extra_data or {}),
            "parties": Jsonb(self._build_parties_payload(doc)),
            "priorities": Jsonb([priority.model_dump(mode="json") for priority in doc.priorities]),
            "classifications": Jsonb(
                [classification.model_dump(mode="json") for classification in doc.classifications]
            ),
            "citations": Jsonb([citation.model_dump(mode="json") for citation in doc.citations]),
            "texts": Jsonb([text.model_dump(mode="json") for text in doc.abstracts_titles]),
            "designations": Jsonb(
                [designation.model_dump(mode="json") for designation in doc.designations]
            ),
            "availability_dates": Jsonb(
                [availability.model_dump(mode="json") for availability in doc.availability_dates]
            ),
        }
        return row

    def _build_stage_row(self, doc: ExchangeDocument, stage_key: str, stage_seq: int) -> Dict[str, Any]:
        row = self._build_document_row(doc)
        row["stage_key"] = stage_key
        row["stage_seq"] = stage_seq
        return row

    def _copy_stage_rows(self, cur, stage_rows: List[Dict[str, Any]]):
        column_sql = ", ".join(STAGE_COLUMNS)
        with cur.copy(f"COPY patent_documents_stage ({column_sql}) FROM STDIN") as copy:
            for row in stage_rows:
                copy.write_row(tuple(row[column] for column in STAGE_COLUMNS))

    def _merge_stage_rows(self, cur, stage_key: str):
        cur.execute(
            """
            WITH latest_rows AS (
                SELECT DISTINCT ON (country, pub_doc_id)
                    country, pub_doc_id, doc_number, kind_code, extended_kind, date_publ,
                    family_id, is_representative, is_grant, originating_office,
                    date_added_docdb, date_last_exchange,
                    app_doc_id, app_country, app_number, app_kind_code, app_date,
                    source_status, app_extra_data, pub_extra_data,
                    parties, priorities, classifications, citations, texts,
                    designations, availability_dates
                FROM patent_documents_stage
                WHERE stage_key = %s
                ORDER BY country, pub_doc_id, stage_seq DESC
            ),
            deleted AS (
                DELETE FROM patent_documents target
                USING latest_rows src
                WHERE src.source_status IN ('D', 'DV', 'V')
                  AND target.country = src.country
                  AND target.pub_doc_id = src.pub_doc_id
            )
            INSERT INTO patent_documents (
                country, pub_doc_id, doc_number, kind_code, extended_kind, date_publ,
                family_id, is_representative, is_grant, originating_office,
                date_added_docdb, date_last_exchange,
                app_doc_id, app_country, app_number, app_kind_code, app_date,
                source_status, app_extra_data, pub_extra_data,
                parties, priorities, classifications, citations, texts,
                designations, availability_dates, updated_at
            )
            SELECT
                country, pub_doc_id, doc_number, kind_code, extended_kind, date_publ,
                family_id, is_representative, is_grant, originating_office,
                date_added_docdb, date_last_exchange,
                app_doc_id, app_country, app_number, app_kind_code, app_date,
                source_status, app_extra_data, pub_extra_data,
                parties, priorities, classifications, citations, texts,
                designations, availability_dates, NOW()
            FROM latest_rows
            WHERE source_status NOT IN ('D', 'DV', 'V')
            ON CONFLICT (country, pub_doc_id) DO UPDATE
            SET
                doc_number = EXCLUDED.doc_number,
                kind_code = EXCLUDED.kind_code,
                extended_kind = EXCLUDED.extended_kind,
                date_publ = EXCLUDED.date_publ,
                family_id = EXCLUDED.family_id,
                is_representative = EXCLUDED.is_representative,
                is_grant = EXCLUDED.is_grant,
                originating_office = EXCLUDED.originating_office,
                date_added_docdb = EXCLUDED.date_added_docdb,
                date_last_exchange = EXCLUDED.date_last_exchange,
                app_doc_id = EXCLUDED.app_doc_id,
                app_country = EXCLUDED.app_country,
                app_number = EXCLUDED.app_number,
                app_kind_code = EXCLUDED.app_kind_code,
                app_date = EXCLUDED.app_date,
                source_status = EXCLUDED.source_status,
                app_extra_data = EXCLUDED.app_extra_data,
                pub_extra_data = EXCLUDED.pub_extra_data,
                parties = EXCLUDED.parties,
                priorities = EXCLUDED.priorities,
                classifications = EXCLUDED.classifications,
                citations = EXCLUDED.citations,
                texts = EXCLUDED.texts,
                designations = EXCLUDED.designations,
                availability_dates = EXCLUDED.availability_dates,
                updated_at = NOW();
            """,
            (stage_key,),
        )

    def bulk_upsert_safe(self, documents: List[ExchangeDocument], stage_key: str):
        if not documents:
            return

        logger.info(
            "Beginning staged batch upsert of %s documents for stage_key=%s.",
            len(documents),
            stage_key,
        )

        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM patent_documents_stage WHERE stage_key = %s;",
                (stage_key,),
            )
            stage_rows = [
                self._build_stage_row(doc, stage_key, stage_seq)
                for stage_seq, doc in enumerate(documents, start=1)
            ]
            self._copy_stage_rows(cur, stage_rows)
            self._merge_stage_rows(cur, stage_key)
            cur.execute(
                "DELETE FROM patent_documents_stage WHERE stage_key = %s;",
                (stage_key,),
            )
            self.conn.commit()
