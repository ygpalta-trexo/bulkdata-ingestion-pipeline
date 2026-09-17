import logging
from dotenv import load_dotenv
from docdb_ingestion.database import DatabaseManager, get_dsn_from_env

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("setup_db")

def main():
    try:
        # Resolve the DSN exactly as the pipeline and the front-file runner do,
        # so setup always prepares the database they will read, including when
        # credentials come from AWS Secrets Manager (USE_SECRET_MANAGER=true).
        dsn = get_dsn_from_env()
        logger.info(f"Connecting to database at {dsn.split('@')[-1]}...") # Obscure creds

        db = DatabaseManager(dsn)
        db.connect() # This calls init_schema() internally
        db.close()
        logger.info("Database tables created successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        exit(1)

if __name__ == "__main__":
    main()
