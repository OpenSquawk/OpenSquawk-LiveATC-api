"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

FLOWS_DIR = Path(os.getenv("FLOWS_DIR", "./flows"))
SESSION_STORE_TYPE = os.getenv("SESSION_STORE_TYPE", "memory")
MAX_FLOW_STACK_DEPTH = int(os.getenv("MAX_FLOW_STACK_DEPTH", "5"))
MAX_AUTO_ADVANCE_HOPS = int(os.getenv("MAX_AUTO_ADVANCE_HOPS", "50"))
READBACK_TIMEOUT_MS = int(os.getenv("READBACK_TIMEOUT_MS", "30000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
