from pathlib import Path
import certifi

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

pool = ConnectionPool(settings.DATABASE_URL, min_size=0, max_size=4, open=False, timeout=15, kwargs={"row_factory": dict_row, "connect_timeout": 10, "sslmode": "verify-full", "sslrootcert": certifi.where()})


def migrate():
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(72401932)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            version = int(path.name.split("_")[0])
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (version,)).fetchone():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))


def initialize():
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL must be configured")
    pool.open()
    migrate()
    with pool.connection() as conn:
        conn.execute("UPDATE extractions SET status='interrupted', error='Processamento interrompido pelo reinício do servidor.', completed_at=now() WHERE status='processing'")
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        conn.execute("DELETE FROM auth_attempts WHERE window_start < now() - interval '1 day'")
