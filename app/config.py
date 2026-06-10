"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

FLOWS_DIR = Path(os.getenv("FLOWS_DIR", "./flows"))
# "sqlite" (default) keeps a write-through SQLite file so sessions survive
# restarts/redeploys; "memory" is process-local (used by the test suite).
SESSION_STORE_TYPE = os.getenv("SESSION_STORE_TYPE", "sqlite")
SESSION_DB_PATH = Path(os.getenv("SESSION_DB_PATH", "./storage/sessions.db"))
# Sessions idle for longer than this are deleted on the next store access.
SESSION_TTL_HOURS = float(os.getenv("SESSION_TTL_HOURS", "5"))
MAX_FLOW_STACK_DEPTH = int(os.getenv("MAX_FLOW_STACK_DEPTH", "5"))
MAX_AUTO_ADVANCE_HOPS = int(os.getenv("MAX_AUTO_ADVANCE_HOPS", "50"))
READBACK_TIMEOUT_MS = int(os.getenv("READBACK_TIMEOUT_MS", "30000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()

_DEFAULT_ORIGINS = ",".join([
    "https://opensquawk.de",
    "https://www.opensquawk.de",
    "https://app.opensquawk.de",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
])
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]
