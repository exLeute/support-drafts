import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/support")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-opus-5-5")
LLM_RPM = int(os.environ.get("LLM_RPM", "60"))

WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "30"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "8"))

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
OPERATOR_PASSWORD = os.environ.get("OPERATOR_PASSWORD", "")

MOCK_FAIL_RATE = float(os.environ.get("MOCK_FAIL_RATE", "0.2"))
MOCK_SLOW_RATE = float(os.environ.get("MOCK_SLOW_RATE", "0.1"))
