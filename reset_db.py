from docdb_ingestion.database import DatabaseManager, get_dsn_from_env

def reset_database():
    dsn = get_dsn_from_env()
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

    db.init_schema()
    print("Reinitialized fresh schema.")

if __name__ == "__main__":
    reset_database()
