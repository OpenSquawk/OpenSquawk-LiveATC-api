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
# Silence window on a readback state before ATC re-requests the readback.
# The frontend fires POST /session/{id}/timeout once this elapses with no utterance.
READBACK_SILENCE_MS = int(os.getenv("READBACK_SILENCE_MS", "40000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()

# --- LLM semantic router ----------------------------------------------------
# When deterministic regex routing fails to match a pilot transmission, the
# engine asks the LLM (via the Nuxt /api/decision/route endpoint) to pick the
# best candidate transition before conceding to bad_next. The call is routed
# through Nuxt so it lands in the central usage ledger and routing-review log.
LLM_ROUTER_ENABLED = os.getenv("LLM_ROUTER_ENABLED", "true").lower() in ("1", "true", "yes")
# Base URL of the Nuxt server that hosts /api/decision/route.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
# Shared secret sent as the x-service-secret header (must match Nuxt SERVICE_SECRET).
SERVICE_SECRET = os.getenv("SERVICE_SECRET", "")
# Time budget handed to the LLM call, in ms. Currently generous (10s) so real
# latency can be measured before tightening it.
LLM_ROUTER_TIMEOUT_MS = int(os.getenv("LLM_ROUTER_TIMEOUT_MS", "10000"))

# Compute a real OSM taxiway route at session creation for taxi flows and use it
# in place of the YAML-default taxi_route. Best-effort: any failure (no ICAO,
# Overpass down, feature not found, no path) silently keeps the flow default.
TAXI_ROUTE_AUTOCOMPUTE = os.getenv("TAXI_ROUTE_AUTOCOMPUTE", "true").lower() in ("1", "true", "yes")
# Per-Overpass-call time budget for taxi route computation, in ms. Two calls run
# (airport features + taxiway graph), so worst case is ~2x this before fallback.
TAXI_ROUTE_TIMEOUT_MS = int(os.getenv("TAXI_ROUTE_TIMEOUT_MS", "8000"))

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
