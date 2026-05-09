"""In-memory session store.

All sessions live in a module-level dict for Phase 1/2.
Replace this module with a database-backed implementation in later phases.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from app.models import DecisionFlow, RuntimeSession

_sessions: Dict[str, RuntimeSession] = {}


def create_session(flow: DecisionFlow) -> RuntimeSession:
    """Create and persist a new session for the given flow."""
    now = datetime.now(timezone.utc).isoformat()
    sid = str(uuid.uuid4())

    # Initialise variables and flags from flow definitions
    variables = {k: v.initial for k, v in flow.variables.items()}
    flags = {k: f.initial for k, f in flow.flags.items()}

    session = RuntimeSession(
        session_id=sid,
        created_at=now,
        main_flow=flow.slug,
        active_flow=flow.slug,
        current_state=flow.start_state,
        variables=variables,
        flags=flags,
    )
    _sessions[sid] = session
    return session


def get_session(session_id: str) -> Optional[RuntimeSession]:
    return _sessions.get(session_id)


def save_session(session: RuntimeSession) -> None:
    _sessions[session.session_id] = session


def delete_session(session_id: str) -> bool:
    return _sessions.pop(session_id, None) is not None


def list_session_ids() -> list[str]:
    return list(_sessions.keys())
