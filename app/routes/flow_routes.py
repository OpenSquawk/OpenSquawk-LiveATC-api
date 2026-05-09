from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.flow_loader import get_all_flows, reload_flows
from app.flow_validator import validate_flow

router = APIRouter(prefix="/api/decision-flows", tags=["flows"])


@router.get("/runtime")
def get_runtime_flows():
    """Return all loaded flow definitions for frontend bootstrap.

    Response shape matches what the frontend's fetchRuntimeTree expects:
      { flows: { slug: DecisionFlow }, main: "slug-of-default-flow" }
    """
    flows = get_all_flows()
    # Pick a main flow: prefer a flow called "icao_atc_decision_tree", else first loaded
    slugs = list(flows.keys())
    main_slug = "icao_atc_decision_tree" if "icao_atc_decision_tree" in flows else (slugs[0] if slugs else "")
    return {
        "flows": {slug: flow.model_dump() for slug, flow in flows.items()},
        "main": main_slug,
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
