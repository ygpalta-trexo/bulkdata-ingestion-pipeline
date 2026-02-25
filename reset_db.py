from docdb_ingestion.database import DatabaseManager
import os
from dotenv import load_dotenv

def reset_database():
    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        user = os.getenv("POSTGRES_USER", "postgres")
        password = os.getenv("POSTGRES_PASSWORD", "password")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        dbname = os.getenv("POSTGRES_DB", "bulk-data")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    db = DatabaseManager(dsn)
    db.connect()

    with db.conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS application_master CASCADE;
            DROP TABLE IF EXISTS document_master CASCADE;
            DROP TABLE IF EXISTS application_reference CASCADE;
            DROP TABLE IF EXISTS priority_claims CASCADE;
            DROP TABLE IF EXISTS parties CASCADE;
            DROP TABLE IF EXISTS designation_of_states CASCADE;
            DROP TABLE IF EXISTS patent_classifications CASCADE;
            DROP TABLE IF EXISTS rich_citations_network CASCADE;
            DROP TABLE IF EXISTS citation_passage_mapping CASCADE;
            DROP TABLE IF EXISTS public_availability_dates CASCADE;
            DROP TABLE IF EXISTS abstracts_and_titles CASCADE;
            DROP TABLE IF EXISTS ingestion_checkpoints CASCADE;
        """)
        db.conn.commit()
        print("Dropped old tables.")

    # db.init_schema()
    # print("Reinitialized new VARCHAR(50) schema.")

if __name__ == "__main__":
    reset_database()
