from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.flow_loader import get_all_flows, reload_flows
from app.flow_validator import validate_flow

router = APIRouter(prefix="/api/decision-flows", tags=["flows"])


@router.get("/runtime")
def get_runtime_flows():
    """Return all loaded flow definitions for frontend bootstrap.

    Shape matches the frontend RuntimeDecisionSystem interface:
      { main, order, flows }
    - main: slug of the primary flow (entry_mode == "main"), or first slug
    - order: ordered list of slugs
    - flows: full flow definitions keyed by slug
    """
    flows = get_all_flows()
    order = list(flows.keys())

    # The "main" flow is the one declared as entry_mode="main", falling back to first.
    main_slug = next(
        (slug for slug, f in flows.items() if f.entry_mode == "main"),
        order[0] if order else "",
    )

    return {
        "main": main_slug,
        "order": order,
        "flows": {slug: flow.model_dump() for slug, flow in flows.items()},
    }


@router.get("/runtime/{slug}")
def get_flow(slug: str):
    """Return a single flow definition."""
    flows = get_all_flows()
    if slug not in flows:
        raise HTTPException(status_code=404, detail=f"Flow '{slug}' not found")
    return flows[slug].model_dump()


@router.get("/runtime/{slug}/validate")
def validate_flow_route(slug: str):
    """Run the static validator on a flow and return issues."""
    flows = get_all_flows()
    if slug not in flows:
        raise HTTPException(status_code=404, detail=f"Flow '{slug}' not found")
    result = validate_flow(flows[slug])
    return result.model_dump()


@router.post("/admin/reload")
def reload_flows_route():
    """Hot-reload all flow files from disk."""
    from app.config import FLOWS_DIR
    flows = reload_flows(FLOWS_DIR)
    return {"reloaded": list(flows.keys()), "count": len(flows)}
