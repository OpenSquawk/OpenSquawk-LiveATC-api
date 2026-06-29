"""LLM semantic router client.

When regex routing fails, the engine calls this to ask the LLM (hosted behind
the Nuxt ``/api/decision/route`` endpoint) which candidate transition best
matches the pilot's STT transcript. Routing the call through Nuxt keeps cost
capture and the routing-review log centralized there.

The call is best-effort: any failure returns a ``None`` choice so the caller
falls back to the deterministic ``bad_next`` path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx

from app import config
from app.models import Transition

logger = logging.getLogger(__name__)


@dataclass
class RouterResult:
    chosen: Optional[str]  # candidate state id, or None (abstain/timeout/error)
    reason: str
    status: str  # decided | abstain | invalid | timeout | error | disabled | transport_error
    latency_ms: Optional[int] = None


def _candidates_payload(ok_next: List[Transition], bad_next: List[Transition]) -> list[dict]:
    payload: list[dict] = []
    for t in ok_next:
        payload.append({"id": t.to, "label": t.label, "kind": "ok"})
    for t in bad_next:
        payload.append({"id": t.to, "label": t.label, "kind": "bad"})
    return payload


def route(
    *,
    session_id: str,
    flow_slug: str,
    state_id: str,
    transcript: str,
    expected_phrase: Optional[str],
    ok_next: List[Transition],
    bad_next: List[Transition],
) -> RouterResult:
    """Ask the LLM to pick a candidate. Never raises — returns a RouterResult.

    Synchronous on purpose: the engine and its test suite are sync, and FastAPI
    runs the calling endpoint in a threadpool, so this blocking call does not
    stall the event loop.
    """

    if not config.LLM_ROUTER_ENABLED:
        return RouterResult(None, "router_disabled", "disabled")
    if not config.SERVICE_SECRET:
        logger.warning("LLM router enabled but SERVICE_SECRET is empty — skipping call.")
        return RouterResult(None, "service_secret_missing", "disabled")

    candidates = _candidates_payload(ok_next, bad_next)
    if not candidates:
        return RouterResult(None, "no_candidates", "disabled")

    url = f"{config.FRONTEND_BASE_URL}/api/decision/route"
    body = {
        "sessionId": session_id,
        "flowSlug": flow_slug,
        "stateId": state_id,
        "transcript": transcript,
        "expectedPhrase": expected_phrase,
        "candidates": candidates,
        "timeoutMs": config.LLM_ROUTER_TIMEOUT_MS,
    }
    headers = {"x-service-secret": config.SERVICE_SECRET}

    # Give Nuxt a little longer than the LLM budget so it can finish the call and
    # write its routing-review record (capturing the true latency) before we give
    # up — otherwise a too-short budget would never surface in the logs.
    http_timeout_s = (config.LLM_ROUTER_TIMEOUT_MS / 1000.0) + 5.0

    try:
        with httpx.Client(timeout=http_timeout_s) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("LLM router HTTP timeout after %.1fs (state=%s)", http_timeout_s, state_id)
        return RouterResult(None, "router_http_timeout", "transport_error")
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the engine
        logger.warning("LLM router call failed (state=%s): %s", state_id, exc)
        return RouterResult(None, f"router_error: {exc}", "transport_error")

    return RouterResult(
        chosen=data.get("chosen"),
        reason=data.get("reason") or "",
        status=data.get("status") or "error",
        latency_ms=data.get("latencyMs"),
    )
