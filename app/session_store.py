"""Session store with optional SQLite persistence.

``SESSION_STORE_TYPE=sqlite`` (the default) keeps an in-memory cache backed
by a write-through SQLite file so sessions survive process restarts and
redeploys.  Mount a volume at the ``SESSION_DB_PATH`` directory in
production, otherwise the file lives in the container layer and is lost on
redeploy.  ``SESSION_STORE_TYPE=memory`` keeps everything process-local
(used by the test suite).

Sessions that have not changed for ``SESSION_TTL_HOURS`` are swept on store
access; callers receive a 404 for expired session ids and are expected to
start a new session.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from app.config import SESSION_DB_PATH, SESSION_STORE_TYPE, SESSION_TTL_HOURS
from app.models import DecisionFlow, RuntimeSession

logger = logging.getLogger(__name__)

_sessions: Dict[str, RuntimeSession] = {}
_touched_at: Dict[str, float] = {}
_lock = threading.Lock()
_db: Optional[sqlite3.Connection] = None
_last_sweep = 0.0

_SWEEP_INTERVAL_SECONDS = 60.0


def _use_sqlite() -> bool:
    return SESSION_STORE_TYPE.strip().lower() == "sqlite"


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        SESSION_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(SESSION_DB_PATH), check_same_thread=False)
        _db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  session_id TEXT PRIMARY KEY,"
            "  data TEXT NOT NULL,"
            "  updated_at REAL NOT NULL"
            ")"
        )
        _db.commit()
    return _db


def _persist(session: RuntimeSession, now: float) -> None:
    if not _use_sqlite():
        return
    try:
        db = _get_db()
        db.execute(
            "INSERT INTO sessions (session_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (session.session_id, session.model_dump_json(), now),
        )
        db.commit()
    except Exception:
        logger.exception("Persisting session %.8s to SQLite failed", session.session_id)


def _load_from_db(session_id: str, cutoff: float) -> Optional[RuntimeSession]:
    if not _use_sqlite():
        return None
    try:
        db = _get_db()
        row = db.execute(
            "SELECT data, updated_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        data, updated_at = row
        if updated_at < cutoff:
            db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            db.commit()
            return None
        session = RuntimeSession.model_validate_json(data)
        _sessions[session_id] = session
        _touched_at[session_id] = updated_at
        return session
    except Exception:
        logger.exception("Loading session %.8s from SQLite failed", session_id)
        return None


def _sweep_expired(now: float) -> None:
    """Drop sessions idle for longer than SESSION_TTL_HOURS (rate-limited)."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    cutoff = now - SESSION_TTL_HOURS * 3600

    expired = [sid for sid, ts in _touched_at.items() if ts < cutoff]
    for sid in expired:
        _sessions.pop(sid, None)
        _touched_at.pop(sid, None)
    if expired:
        logger.info("Swept %d idle session(s) from cache", len(expired))

    if _use_sqlite():
        try:
            db = _get_db()
            cursor = db.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
            db.commit()
            if cursor.rowcount:
                logger.info("Swept %d idle session(s) from SQLite", cursor.rowcount)
        except Exception:
            logger.exception("SQLite session sweep failed")


def create_session(
    flow: DecisionFlow,
    variable_overrides: dict | None = None,
    no_chain: bool = False,
    airport_icao: str | None = None,
    destination_icao: str | None = None,
) -> RuntimeSession:
    """Create and persist a new session for the given flow.

    ``variable_overrides`` is applied on top of the YAML initial values.
    Only keys that are declared in the flow definition are accepted;
    unknown keys are silently ignored so the frontend can pass the full
    flight-plan object without pre-filtering.
    """
    now = datetime.now(timezone.utc).isoformat()
    sid = str(uuid.uuid4())

    # Initialise variables and flags from flow definitions
    variables = {k: v.initial for k, v in flow.variables.items()}
    flags = {k: f.initial for k, f in flow.flags.items()}

    # Apply caller-supplied overrides.
    # Declared keys are already initialised above; extra keys (e.g. frequencies
    # for downstream chained flows such as tower_freq, departure_freq) are stored
    # as-is so they survive the full session chain without needing to be declared
    # in each intermediate flow's schema.
    if variable_overrides:
        for key, value in variable_overrides.items():
            variables[key] = value

    session = RuntimeSession(
        session_id=sid,
        created_at=now,
        main_flow=flow.slug,
        active_flow=flow.slug,
        current_state=flow.start_state,
        airport_icao=(airport_icao.strip().upper() if airport_icao else None),
        destination_icao=(destination_icao.strip().upper() if destination_icao else None),
        variables=variables,
        flags=flags,
        no_chain=no_chain,
    )

    with _lock:
        ts = time.time()
        _sweep_expired(ts)
        _sessions[sid] = session
        _touched_at[sid] = ts
        _persist(session, ts)
    return session


def get_session(session_id: str) -> Optional[RuntimeSession]:
    with _lock:
        now = time.time()
        _sweep_expired(now)

        session = _sessions.get(session_id)
        if session is not None:
            ts = _touched_at.get(session_id, now)
            if ts < now - SESSION_TTL_HOURS * 3600:
                _sessions.pop(session_id, None)
                _touched_at.pop(session_id, None)
                return None
            return session

        # Cache miss — e.g. after a restart. Try the persistent store.
        return _load_from_db(session_id, cutoff=now - SESSION_TTL_HOURS * 3600)


def save_session(session: RuntimeSession) -> None:
    with _lock:
        now = time.time()
        _sessions[session.session_id] = session
        _touched_at[session.session_id] = now
        _persist(session, now)


def delete_session(session_id: str) -> bool:
    with _lock:
        existed = _sessions.pop(session_id, None) is not None
        _touched_at.pop(session_id, None)
        if _use_sqlite():
            try:
                db = _get_db()
                cursor = db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                db.commit()
                existed = existed or cursor.rowcount > 0
            except Exception:
                logger.exception("Deleting session %.8s from SQLite failed", session_id)
        return existed


def list_session_ids() -> list[str]:
    with _lock:
        ids = set(_sessions.keys())
        if _use_sqlite():
            try:
                db = _get_db()
                cutoff = time.time() - SESSION_TTL_HOURS * 3600
                rows = db.execute(
                    "SELECT session_id FROM sessions WHERE updated_at >= ?",
                    (cutoff,),
                ).fetchall()
                ids.update(row[0] for row in rows)
            except Exception:
                logger.exception("Listing sessions from SQLite failed")
        return list(ids)
