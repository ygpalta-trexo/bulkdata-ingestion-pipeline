import os
import logging
from dotenv import load_dotenv
from docdb_ingestion.database import DatabaseManager

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("setup_db")

def get_dsn():
    """
    Construct DSN from environment variables.
    prio: DATABASE_URL > Component vars
    """
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return dsn
    
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "password")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "docdb")
    
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

def main():
    dsn = get_dsn()
    logger.info(f"Connecting to database at {dsn.split('@')[-1]}...") # Obscure creds
    
    try:
        db = DatabaseManager(dsn)
        db.connect() # This calls init_schema() internally
        db.close()
        logger.info("Database tables created successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        exit(1)

if __name__ == "__main__":
    main()
