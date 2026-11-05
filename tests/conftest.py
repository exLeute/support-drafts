import pytest

from app import db, settings


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def pool():
    # tests truncate tables: refuse to run against anything but a test database
    assert settings.DATABASE_URL.rstrip("/").endswith("_test"), "set DATABASE_URL to a *_test database"
    await db.init()
    yield db.pool
    await db.pool.close()


@pytest.fixture
async def clean(pool):
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE tickets, llm_calls, mock_replies")
