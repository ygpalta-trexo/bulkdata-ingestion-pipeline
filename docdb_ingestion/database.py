import psycopg
from psycopg.rows import dict_row
from typing import List
from .models import ExchangeDocument
import os
from dotenv import load_dotenv

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
    
    # Safe fallback if .env doesn't specify POSTGRES_DB
    return os.getenv("DATABASE_URL", f"postgresql://{user}:{password}@{host}:{port}/docdb")

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
            # Table 0: Delivery Files (File lifecycle state machine)
            cur.execute("""
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
                CREATE INDEX IF NOT EXISTS idx_delivery_status ON delivery_files (status);
            """)
            
            # Table 1: Application Master (NEW ROOT)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS application_master (
                    app_doc_id VARCHAR(50) PRIMARY KEY,
                    app_country VARCHAR(2),
                    app_number VARCHAR(50),
                    app_kind_code VARCHAR(5),
                    app_date DATE,
                    extra_data JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            # Table 2: Document Master (Child of Application Master)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS document_master (
                    pub_doc_id VARCHAR(50) PRIMARY KEY,
                    app_doc_id VARCHAR(50) REFERENCES application_master(app_doc_id) ON DELETE CASCADE,
                    country VARCHAR(2),
                    doc_number VARCHAR(50),
                    kind_code VARCHAR(5),
                    extended_kind VARCHAR(10),
                    date_publ DATE,
                    family_id VARCHAR(50),
                    is_representative BOOLEAN,
                    is_grant BOOLEAN DEFAULT FALSE,
                    originating_office VARCHAR(10),
                    date_added_docdb DATE,
                    date_last_exchange DATE,
                    extra_data JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            # Table 3: Priority Claims
            cur.execute("""
                CREATE TABLE IF NOT EXISTS priority_claims (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    format_type VARCHAR(10),
                    sequence INT,
                    priority_doc_id VARCHAR(50),
                    country VARCHAR(2),
                    doc_number VARCHAR(50),
                    date DATE,
                    linkage_type VARCHAR(1),
                    is_active BOOLEAN
                );
            """)
            # Table 4: Parties
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    party_type VARCHAR(10),
                    format_type VARCHAR(10),
                    sequence INT,
                    party_name VARCHAR(500),
                    residence VARCHAR(2),
                    address_text TEXT
                );
            """)
            # Table 5: Designation of States
            cur.execute("""
                CREATE TABLE IF NOT EXISTS designation_of_states (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    treaty_type VARCHAR(5),
                    designation_type VARCHAR(20),
                    region_code VARCHAR(2),
                    country_code VARCHAR(2)
                );
            """)
            # Table 6: Patent Classifications
            cur.execute("""
                CREATE TABLE IF NOT EXISTS patent_classifications (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    scheme_name VARCHAR(10),
                    sequence INT,
                    group_number INT,
                    rank_number INT,
                    symbol VARCHAR(50),
                    class_value VARCHAR(1),
                    symbol_pos VARCHAR(1),
                    generating_office VARCHAR(2)
                );
            """)
            # Table 7: Rich Citations Network
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rich_citations_network (
                    citation_id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    cited_phase VARCHAR(5),
                    sequence INT,
                    srep_office VARCHAR(2),
                    citation_type VARCHAR(10),
                    cited_doc_id VARCHAR(50),
                    dnum_type VARCHAR(20),
                    npl_type VARCHAR(5),
                    extracted_xp VARCHAR(50),
                    opponent_name VARCHAR(255),
                    citation_text TEXT
                );
            """)
            # Table 8: Citation Passage Mapping
            cur.execute("""
                CREATE TABLE IF NOT EXISTS citation_passage_mapping (
                    id BIGSERIAL PRIMARY KEY,
                    citation_id BIGINT REFERENCES rich_citations_network(citation_id) ON DELETE CASCADE,
                    category VARCHAR(10),
                    rel_claims VARCHAR(255),
                    passage_text TEXT
                );
            """)
            # Table 9: Public Availability Dates
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public_availability_dates (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    availability_type VARCHAR(50),
                    availability_date DATE
                );
            """)
            # Table 10: Abstracts and Titles
            cur.execute("""
                CREATE TABLE IF NOT EXISTS abstracts_and_titles (
                    id BIGSERIAL PRIMARY KEY,
                    pub_doc_id VARCHAR(50) REFERENCES document_master(pub_doc_id) ON DELETE CASCADE,
                    text_type VARCHAR(10),
                    lang VARCHAR(2),
                    format_type VARCHAR(10),
                    source VARCHAR(50),
                    content TEXT
                );
            """)
            
            # Checkpoints table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
                    filename TEXT PRIMARY KEY,
                    main_zip_id INTEGER,
                    main_zip_filename TEXT,
                    status TEXT,
                    processed_at TIMESTAMP DEFAULT NOW()
                );
            """)

            # -------------------------------------------------------
            # Production Indexes
            # -------------------------------------------------------
            # Core FK join: all publications for an application
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_app_doc_id ON document_master (app_doc_id);")
            
            # Human-readable number lookups (API-facing)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_am_app_number ON application_master (app_number);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_doc_number ON document_master (doc_number);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_country_doc_number ON document_master (country, doc_number);")
            
            # Grant filtering
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_is_grant ON document_master (is_grant) WHERE is_grant = TRUE;")
            
            # Date-range queries
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_date_publ ON document_master (date_publ);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_am_app_date ON application_master (app_date);")
            
            # Child table FK indexes (critical for CASCADE DELETE performance at scale)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_parties_pub_doc_id ON parties (pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_priorities_pub_doc_id ON priority_claims (pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_classifications_pub_doc_id ON patent_classifications (pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_citations_pub_doc_id ON rich_citations_network (pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_abstracts_pub_doc_id ON abstracts_and_titles (pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_avail_pub_doc_id ON public_availability_dates (pub_doc_id);")
            
            # Biblio-specific: party name (prefix search) and classification symbol
            cur.execute("CREATE INDEX IF NOT EXISTS idx_parties_name ON parties (party_name text_pattern_ops);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_parties_type ON parties (party_type, pub_doc_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_class_symbol ON patent_classifications (scheme_name, symbol);")
            
            # Ingestion recovery: find all STARTED (unfinished) files quickly
            cur.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON ingestion_checkpoints (status);")

            self.conn.commit()

    def is_file_processed(self, filename: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM ingestion_checkpoints WHERE filename = %s", (filename,))
            res = cur.fetchone()
            return res and res['status'] == 'COMPLETED'

    def mark_file_started(self, filename: str, main_zip_id: int = None, main_zip_filename: str = None):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ingestion_checkpoints (filename, main_zip_id, main_zip_filename, status, processed_at)
                VALUES (%s, %s, %s, 'STARTED', NOW())
                ON CONFLICT (filename) DO UPDATE SET 
                    status = 'STARTED', 
                    main_zip_id = EXCLUDED.main_zip_id,
                    main_zip_filename = EXCLUDED.main_zip_filename,
                    processed_at = NOW();
            """, (filename, main_zip_id, main_zip_filename))
            self.conn.commit()

    def mark_file_completed(self, filename: str):
         with self.conn.cursor() as cur:
            cur.execute("""
                UPDATE ingestion_checkpoints SET status = 'COMPLETED', processed_at = NOW()
                WHERE filename = %s;
            """, (filename,))
            self.conn.commit()
            
    # --- Bulk Delivery Tracking Methods ---
    def sync_delivery_files(self, product_id: int, delivery_id: int, files_data: List[dict]):
        """Persist newly discovered files from the API into our state tracking machine."""
        with self.conn.cursor() as cur:
            for f in files_data:
                cur.execute("""
                    INSERT INTO delivery_files (file_id, product_id, delivery_id, filename, status)
                    VALUES (%s, %s, %s, %s, 'PENDING')
                    ON CONFLICT (file_id) DO NOTHING;
                """, (f['file_id'], product_id, delivery_id, f['filename']))
            self.conn.commit()
            
    def get_actionable_files(self, product_id: int, delivery_id: int) -> List[dict]:
        """Fetch files that need processing (PENDING or partially complete)."""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM delivery_files 
                WHERE product_id = %s AND delivery_id = %s 
                  AND status NOT IN ('COMPLETED', 'FAILED')
                ORDER BY file_id ASC;
            """, (product_id, delivery_id))
            return [dict(row) for row in cur.fetchall()]
            
    def get_all_delivery_files(self, product_id: int, delivery_id: int) -> List[dict]:
        """Fetch all files for a delivery, regardless of status."""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM delivery_files 
                WHERE product_id = %s AND delivery_id = %s 
                ORDER BY file_id ASC;
            """, (product_id, delivery_id))
            return [dict(row) for row in cur.fetchall()]
            
    def update_file_status(self, file_id: int, status: str, error_message: str = None):
        """Transition file state, useful for the pipeline state machine."""
        with self.conn.cursor() as cur:
            cur.execute("""
                UPDATE delivery_files 
                SET status = %s, error_message = %s, updated_at = NOW()
                WHERE file_id = %s;
            """, (status, error_message, file_id))
            self.conn.commit()

    def bulk_upsert_safe(self, documents: List[ExchangeDocument]):
         if not documents:
            return
         
         with self.conn.cursor() as cur:
            for doc in documents:
                import json
                
                # Setup extra data variables for SQL
                doc_dict = doc.app_master.model_dump()
                doc_dict['extra_data'] = json.dumps(doc.app_master.extra_data) if doc.app_master.extra_data else '{}'
                
                # 1. Upsert Application Master (Root)
                cur.execute("""
                    INSERT INTO application_master (
                        app_doc_id, app_country, app_number, app_kind_code, app_date, extra_data
                    ) VALUES (
                        %(app_doc_id)s, %(app_country)s, %(app_number)s, %(app_kind_code)s, %(app_date)s, %(extra_data)s::jsonb
                    ) ON CONFLICT (app_doc_id) DO UPDATE SET
                        app_country = EXCLUDED.app_country,
                        app_number = EXCLUDED.app_number,
                        app_kind_code = EXCLUDED.app_kind_code,
                        app_date = EXCLUDED.app_date,
                        extra_data = EXCLUDED.extra_data,
                        updated_at = NOW()
                """, doc_dict)

                pub_doc_id = doc.pub_master.pub_doc_id
                
                # 2. Delete existing document (CASCADE removes all child rows)
                cur.execute("DELETE FROM document_master WHERE pub_doc_id = %s", (pub_doc_id,))
                
                # Route based on operation: 'D'/'DV'/'V' are Delete instructions
                if doc.operation.upper() in ('D', 'DV', 'V'):
                    logger.info(f"Delete operation for {pub_doc_id} — removed from DB.")
                    continue
                
                pub_dict = doc.pub_master.model_dump()
                pub_dict['extra_data'] = json.dumps(doc.pub_master.extra_data) if doc.pub_master.extra_data else '{}'
                
                # 3. Insert Document Master
                cur.execute("""
                    INSERT INTO document_master (
                        pub_doc_id, app_doc_id, country, doc_number, kind_code, extended_kind, 
                        date_publ, family_id, is_representative, is_grant,
                        originating_office, date_added_docdb, date_last_exchange, extra_data
                    ) VALUES (
                        %(pub_doc_id)s, %(app_doc_id)s, %(country)s, %(doc_number)s, %(kind_code)s, %(extended_kind)s,
                        %(date_publ)s, %(family_id)s, %(is_representative)s, %(is_grant)s,
                        %(originating_office)s, %(date_added_docdb)s, %(date_last_exchange)s, %(extra_data)s::jsonb
                    )
                """, pub_dict)

                # 4. Insert Priorities
                if doc.priorities:
                     pri_data = [ {**p.model_dump(), 'pub_doc_id': pub_doc_id} for p in doc.priorities ]
                     cur.executemany("""
                         INSERT INTO priority_claims (
                             pub_doc_id, format_type, sequence, priority_doc_id, country, doc_number, date, linkage_type, is_active
                         ) VALUES (
                             %(pub_doc_id)s, %(format_type)s, %(sequence)s, %(priority_doc_id)s, %(country)s, %(doc_number)s, %(priority_date)s, %(linkage_type)s, %(is_active)s
                         )
                     """, pri_data)

                # 5. Insert Parties
                if doc.parties:
                     party_data = [ {**p.model_dump(), 'pub_doc_id': pub_doc_id} for p in doc.parties ]
                     cur.executemany("""
                         INSERT INTO parties (
                             pub_doc_id, party_type, format_type, sequence, party_name, residence, address_text
                         ) VALUES (
                             %(pub_doc_id)s, %(party_type)s, %(format_type)s, %(sequence)s, %(party_name)s, %(residence)s, %(address_text)s
                         )
                     """, party_data)

                # 6. Insert Designations
                if doc.designations:
                     desig_data = [ {**d.model_dump(), 'pub_doc_id': pub_doc_id} for d in doc.designations ]
                     cur.executemany("""
                         INSERT INTO designation_of_states (
                             pub_doc_id, treaty_type, designation_type, region_code, country_code
                         ) VALUES (
                             %(pub_doc_id)s, %(treaty_type)s, %(designation_type)s, %(region_code)s, %(country_code)s
                         )
                     """, desig_data)

                # 7. Insert Classifications
                if doc.classifications:
                     class_data = [ {**c.model_dump(), 'pub_doc_id': pub_doc_id} for c in doc.classifications ]
                     cur.executemany("""
                         INSERT INTO patent_classifications (
                             pub_doc_id, scheme_name, sequence, group_number, rank_number, symbol, class_value, symbol_pos, generating_office
                         ) VALUES (
                             %(pub_doc_id)s, %(scheme_name)s, %(sequence)s, %(group_number)s, %(rank_number)s, %(symbol)s, %(class_value)s, %(symbol_pos)s, %(generating_office)s
                         )
                     """, class_data)

                # 8. Insert Citations and Passages
                for cit in doc.citations:
                     cit_data = {**cit.model_dump(exclude={'passages'}), 'pub_doc_id': pub_doc_id}
                     cur.execute("""
                         INSERT INTO rich_citations_network (
                             pub_doc_id, cited_phase, sequence, srep_office, citation_type, cited_doc_id, 
                             dnum_type, npl_type, extracted_xp, opponent_name, citation_text
                         ) VALUES (
                             %(pub_doc_id)s, %(cited_phase)s, %(sequence)s, %(srep_office)s, %(citation_type)s, %(cited_doc_id)s,
                             %(dnum_type)s, %(npl_type)s, %(extracted_xp)s, %(opponent_name)s, %(citation_text)s
                         ) RETURNING citation_id
                     """, cit_data)
                     citation_id = cur.fetchone()['citation_id']
                     
                     if cit.passages:
                          pas_data = [ {**p.model_dump(), 'citation_id': citation_id} for p in cit.passages ]
                          cur.executemany("""
                              INSERT INTO citation_passage_mapping (
                                  citation_id, category, rel_claims, passage_text
                              ) VALUES (
                                  %(citation_id)s, %(category)s, %(rel_claims)s, %(passage_text)s
                              )
                          """, pas_data)

                # 9. Insert Availability Dates
                if doc.availability_dates:
                     avail_data = [ {**d.model_dump(), 'pub_doc_id': pub_doc_id} for d in doc.availability_dates ]
                     cur.executemany("""
                         INSERT INTO public_availability_dates (
                             pub_doc_id, availability_type, availability_date
                         ) VALUES (
                             %(pub_doc_id)s, %(availability_type)s, %(availability_date)s
                         )
                     """, avail_data)

                # 10. Insert Abstracts and Titles
                if doc.abstracts_titles:
                     ab_data = [ {**ab.model_dump(), 'pub_doc_id': pub_doc_id} for ab in doc.abstracts_titles ]
                     cur.executemany("""
                         INSERT INTO abstracts_and_titles (
                             pub_doc_id, text_type, lang, format_type, source, content
                         ) VALUES (
                             %(pub_doc_id)s, %(text_type)s, %(lang)s, %(format_type)s, %(source)s, %(content)s
                         )
                     """, ab_data)

            self.conn.commit()
