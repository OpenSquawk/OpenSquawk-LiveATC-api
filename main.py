"""FastAPI application entrypoint."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import FLOWS_DIR, LOG_LEVEL
from app.flow_loader import load_all_flows
from app.flow_validator import validate_flow
from app.routes.decision_routes import router as decision_router
from app.routes.flow_routes import router as flow_router
from app.routes.session_routes import router as session_router

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="OpenSquawk LiveATC API",
    description="PM radio training backend — Phase 1/2 (deterministic routing)",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(flow_router)
app.include_router(session_router)
app.include_router(decision_router)


@app.on_event("startup")
def _startup():
    if not FLOWS_DIR.exists():
        logger.warning("Flows directory '%s' does not exist — no flows loaded", FLOWS_DIR)
        return

    flows = load_all_flows(FLOWS_DIR)

    for slug, flow in flows.items():
        result = validate_flow(flow)
        for issue in result.issues:
            log = logger.error if issue.severity == "error" else logger.warning
            log("[flow=%s] %s: %s", slug, issue.severity.upper(), issue.message)
        if result.valid:
            logger.info("[flow=%s] validation passed", slug)
        else:
            logger.error("[flow=%s] validation FAILED — fix errors before use", slug)


@app.get("/")
def health():
    from app.flow_loader import get_all_flows
    return {
        "status": "ok",
        "loaded_flows": list(get_all_flows().keys()),
    }
