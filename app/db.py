from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import settings

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

# The default pool (4 connections) queues a webhook burst past the helpdesk's 2 s timeout.
pool = AsyncConnectionPool(settings.DATABASE_URL, open=False, min_size=2, max_size=20, kwargs={"row_factory": dict_row})


async def init() -> None:
    await pool.open(wait=True)
    async with pool.connection() as conn, conn.transaction():
        # app and worker start at the same time; concurrent CREATE ... IF NOT EXISTS can still collide
        await conn.execute("SELECT pg_advisory_xact_lock(1)")
        await conn.execute(SCHEMA)
