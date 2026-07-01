"""Public airport tooling endpoints: feature geocoding and taxi routing.

These reproduce the old Nuxt `/api/service/tools/*` endpoints on the Python
backend. They are standalone OSM/Overpass tools and are intentionally decoupled
from the radio decision engine.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.airport_geocode import (
    GeocodeQuery,
    OverpassClient,
    OverpassError,
    RunwayPoint,
    geocode_airport,
)
from app.taxi_routing import TaxiRouteError, calculate_taxi_route

router = APIRouter(prefix="/api/service/tools", tags=["tools"])


def get_overpass_client() -> OverpassClient:
    """Overpass client provider — overridable in tests via dependency_overrides."""
    return OverpassClient()


def _coalesce_lon(lng: float | None, lon: float | None) -> float | None:
    """Old API accepts both `*_lng` and `*_lon`; `*_lng` wins when both given."""
    return lng if lng is not None else lon


def _build_query(
    name: str | None,
    lat: float | None,
    lon: float | None,
    runway_point: RunwayPoint,
) -> GeocodeQuery | None:
    cleaned = name.strip() if name else None
    if not cleaned and lat is None and lon is None:
        return None
    return GeocodeQuery(name=cleaned, lat=lat, lon=lon, runway_point=runway_point)


@router.get("/airport-geocode")
def airport_geocode(
    airport: str = Query(..., description="ICAO code"),
    origin_name: str | None = None,
    origin_lat: float | None = None,
    origin_lng: float | None = None,
    origin_lon: float | None = None,
    origin_runway_point: Literal["start", "end", "center"] = "start",
    dest_name: str | None = None,
    dest_lat: float | None = None,
    dest_lng: float | None = None,
    dest_lon: float | None = None,
    dest_runway_point: Literal["start", "end", "center"] = "start",
    client: OverpassClient = Depends(get_overpass_client),
):
    """Resolve named aerodrome features to coordinates, or coordinates to the
    nearest named feature, for a single airport."""

    if not airport.strip():
        return JSONResponse(
            status_code=400,
            content={"error": "missing_airport", "details": "airport is required"},
        )

    origin = _build_query(
        origin_name, origin_lat, _coalesce_lon(origin_lng, origin_lon), origin_runway_point
    )
    dest = _build_query(
        dest_name, dest_lat, _coalesce_lon(dest_lng, dest_lon), dest_runway_point
    )

    try:
        return geocode_airport(airport, origin, dest, client=client)
    except OverpassError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "overpass_error",
                "airport": airport.strip().upper(),
                "details": str(exc),
            },
        )


@router.get("/taxiroute")
def taxiroute(
    airport: str | None = None,
    origin_lat: float | None = None,
    origin_lng: float | None = None,
    origin_lon: float | None = None,
    origin_name: str | None = None,
    origin_runway_point: Literal["start", "end", "center"] = "start",
    dest_lat: float | None = None,
    dest_lng: float | None = None,
    dest_lon: float | None = None,
    dest_name: str | None = None,
    dest_runway_point: Literal["start", "end", "center"] = "start",
    radius: float = Query(5000, gt=0, le=50000, description="Taxiway search radius (m)"),
    include_connectors: bool = Query(
        False, description="Add parking/holding/apron ways to bridge taxiway gaps"
    ),
    client: OverpassClient = Depends(get_overpass_client),
):
    """Compute a taxi route between two points or named aerodrome features."""

    origin = GeocodeQuery(
        name=origin_name.strip() if origin_name else None,
        lat=origin_lat,
        lon=_coalesce_lon(origin_lng, origin_lon),
        runway_point=origin_runway_point,
    )
    dest = GeocodeQuery(
        name=dest_name.strip() if dest_name else None,
        lat=dest_lat,
        lon=_coalesce_lon(dest_lng, dest_lon),
        runway_point=dest_runway_point,
    )

    try:
        return calculate_taxi_route(
            origin=origin,
            dest=dest,
            airport=airport,
            radius_m=radius,
            include_connectors=include_connectors,
            client=client,
        )
    except TaxiRouteError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())
    except OverpassError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "overpass_error",
                "airport": (airport.strip().upper() if airport else None),
                "details": str(exc),
            },
        )
