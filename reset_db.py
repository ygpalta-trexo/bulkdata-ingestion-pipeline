from docdb_ingestion.database import DatabaseManager, get_dsn_from_env

def reset_database():
    dsn = get_dsn_from_env()
    db = DatabaseManager(dsn)
    db.connect()

    with db.conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS patent_documents CASCADE;
            DROP TABLE IF EXISTS ingestion_checkpoints CASCADE;
            DROP TABLE IF EXISTS delivery_files CASCADE;
        """)
        db.conn.commit()
        print("Dropped single-table schema.")

    db.init_schema()
    print("Reinitialized fresh schema.")

if __name__ == "__main__":
    reset_database()
