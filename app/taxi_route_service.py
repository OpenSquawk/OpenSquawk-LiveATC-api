"""Best-effort taxi route computation for session creation.

Wires the OSM taxiway router (``app.taxi_routing``) into the radio flows that
issue a taxi clearance. The goal is to replace the static YAML ``taxi_route``
default (e.g. "Alpha, Bravo") with a route computed from real OpenStreetMap
taxiway geometry for the session's airport.

Design constraints:

- **Best-effort only.** Any failure — autocompute disabled, no ICAO, Overpass
  unreachable/slow, stand or runway not found in OSM, or no path between them —
  returns ``None`` so the caller keeps the flow's YAML default. This never
  raises into session creation.
- **Computed at session creation, not mid-turn.** The ICAO is in scope there,
  the cost is paid once up front (and cached), and the pilot never waits on a
  live Overpass call in the middle of the conversation.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app import config
from app.airport_geocode import (
    AirportFeature,
    GeocodeQuery,
    OverpassClient,
    RunwayPoint,
    fetch_airport_features,
    match_feature_by_coordinate,
    match_feature_by_name,
)
from app.taxi_routing import calculate_taxi_route

logger = logging.getLogger(__name__)

_PHONETIC = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliet",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee",
    "Z": "Zulu",
}

# Taxiway designators OSM exposes as terse refs ("L7", "WA", "N"). ATC speaks the
# letters phonetically ("Lima 7", "Whiskey Alpha", "November") and reads the
# digits as-is. Only expand tokens that look like a designator; leave anything
# already spelled out (a real taxiway *name*) untouched.
_DESIGNATOR_RE = re.compile(r"^[A-Z]{1,3}\d{0,2}$")


def _phoneticize_token(token: str) -> str:
    compact = token.strip().upper()
    if not _DESIGNATOR_RE.match(compact):
        return token.strip()
    letters = "".join(c for c in compact if c.isalpha())
    digits = "".join(c for c in compact if c.isdigit())
    spoken = " ".join(_PHONETIC[c] for c in letters)
    return f"{spoken} {digits}".strip() if digits else spoken


def phoneticize_route(route: str) -> str:
    """Turn an OSM ref route ("L7, N") into spoken ATC form ("Lima 7, November")."""
    return ", ".join(_phoneticize_token(part) for part in route.split(",") if part.strip())


@dataclass(frozen=True)
class TaxiRoutingSpec:
    """How to derive a taxi route's endpoints from a flow's session variables.

    ``origin_var`` / ``dest_var`` name the session variables holding the feature
    designators (a stand/gate or a runway). ``*_runway_point`` only matters when
    the corresponding endpoint resolves to a runway way. ``stand_side`` marks
    which endpoint is the parking stand — that side gets the real-stand
    selection (reverse geocode from aircraft position, or a random real stand).
    """

    origin_var: str
    dest_var: str
    origin_runway_point: RunwayPoint = "start"
    dest_runway_point: RunwayPoint = "start"
    stand_side: Literal["origin", "dest"] = "origin"


# Departure: park stand -> departure runway holding point.
# Arrival: vacated runway -> parking stand.
TAXI_FLOW_SPECS: dict[str, TaxiRoutingSpec] = {
    "taxi-v1": TaxiRoutingSpec(origin_var="stand", dest_var="runway", stand_side="origin"),
    "taxi-in-v1": TaxiRoutingSpec(
        origin_var="runway",
        dest_var="parking_stand",
        origin_runway_point="end",
        stand_side="dest",
    ),
}

# Reverse-geocoded stand must be this close to the aircraft to trust it as the
# spoken stand name (a parked A320 sits within metres of its stand node).
_STAND_SNAP_MAX_M = 500.0

# One process-wide client so the in-memory Overpass TTL cache is shared across
# sessions (repeated demo flights at the same airport reuse cached OSM data).
_client: OverpassClient | None = None


def _get_client() -> OverpassClient:
    global _client
    if _client is None:
        _client = OverpassClient(timeout_seconds=config.TAXI_ROUTE_TIMEOUT_MS / 1000)
    return _client


def _hold_clause(crossings: list[str]) -> str:
    """Spoken hold-short clause for the crossed runways, or "" if none.

    Empty string is deliberate: rendered into a say_template it cleanly drops
    the clause, so "hold short runway X" is only spoken when a crossing exists.
    """
    return "".join(f", hold short runway {runway}" for runway in crossings)


def resolve_taxi_clearance(
    *,
    icao: str,
    origin_name: str | None = None,
    dest_name: str,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    origin_runway_point: RunwayPoint = "start",
    dest_runway_point: RunwayPoint = "start",
    origin_prefer: Literal["stand", "runway"] | None = None,
    dest_prefer: Literal["stand", "runway"] | None = None,
    client: OverpassClient | None = None,
) -> dict[str, Any] | None:
    """Compute the taxi clearance variables for one route, or ``None``.

    The origin is either a feature name or raw coordinates (live aircraft
    position from the sim bridge). Returns the session-variable overrides the
    flow expects:
      - ``taxi_route``: spoken collapsed taxiway sequence ("Lima 7, November")
      - ``crossing_runways``: list of crossed runways (for readback grading)
      - ``taxi_hold_clause``: ready-to-speak hold-short clause, or ""

    ``None`` on any resolution failure or empty route, so the caller keeps the
    flow's YAML defaults.
    """

    origin = GeocodeQuery(
        name=origin_name or None,
        lat=origin_lat,
        lon=origin_lon,
        runway_point=origin_runway_point,
        prefer=origin_prefer,
    )
    dest = GeocodeQuery(name=dest_name, runway_point=dest_runway_point, prefer=dest_prefer)
    try:
        result = calculate_taxi_route(
            origin=origin,
            dest=dest,
            airport=icao,
            include_connectors=config.TAXI_ROUTE_INCLUDE_CONNECTORS,
            client=client or _get_client(),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort; never break session create
        logger.warning("Taxi route computation failed for %s (%s -> %s): %s", icao, origin_name, dest_name, exc)
        return None

    if result.get("route") is None:
        logger.info("Taxi route: no path %s (%s -> %s)", icao, origin_name, dest_name)
        return None

    names = result.get("names_collapsed") or []
    if not names:
        return None

    crossings = [str(r) for r in (result.get("crossings") or [])]
    return {
        "taxi_route": phoneticize_route(", ".join(names)),
        "crossing_runways": crossings,
        "taxi_hold_clause": _hold_clause(crossings),
    }


def _stand_features(features: list[AirportFeature]) -> list[AirportFeature]:
    return [f for f in features if f.type in ("stand", "gate")]


def _feature_display_name(feature: AirportFeature, matched: str | None = None) -> str:
    return (matched or feature.primary_alias).strip()


def _select_stand(
    *,
    icao: str,
    stand_name: str,
    aircraft: tuple[float, float] | None,
    client: OverpassClient,
) -> tuple[str | None, tuple[float, float] | None, str | None]:
    """Pick the real stand to route from/to.

    Returns ``(name, coords, spoken_update)``:
      - bridge position available → route from the raw coordinates and, when a
        stand/gate lies within snap range, use its designator as the spoken name;
      - otherwise, if the session's stand designator exists in OSM, use it;
      - otherwise pick a **random real stand** of the airport, so the clearance
        and route always refer to a place that actually exists there.

    ``spoken_update`` is the designator the session variable should be updated
    to (None = keep the current value). Feature fetches hit the client's TTL
    cache, so this adds no extra Overpass round-trip.
    """
    features = fetch_airport_features(icao, client=client)
    stands = _stand_features(features)

    if aircraft is not None:
        spoken: str | None = None
        if stands:
            nearest = match_feature_by_coordinate(stands, aircraft[0], aircraft[1])
            if (
                nearest is not None
                and nearest.distance_meters is not None
                and nearest.distance_meters <= _STAND_SNAP_MAX_M
            ):
                spoken = _feature_display_name(nearest.feature, nearest.matched_alias)
        return None, aircraft, spoken

    if stand_name and stands and match_feature_by_name(stands, stand_name) is not None:
        return stand_name, None, None

    if stands:
        chosen = _feature_display_name(random.choice(stands))
        logger.info(
            "Taxi route: stand '%s' not in OSM for %s — using random real stand '%s'",
            stand_name, icao, chosen,
        )
        return chosen, None, chosen

    return (stand_name or None), None, None


def maybe_compute_taxi_route(
    *,
    flow_slug: str,
    icao: str | None,
    variables: Mapping[str, Any],
    aircraft_lat: float | None = None,
    aircraft_lon: float | None = None,
    client: OverpassClient | None = None,
) -> dict[str, Any] | None:
    """Compute taxi clearance variables for a flow, or ``None``.

    ``variables`` is the merged session variable map (YAML defaults + overrides),
    so per-session stand/runway overrides are respected. When the sim bridge
    supplies the aircraft position, the departure route starts from the real
    parking position; otherwise a random real stand of the airport is used when
    the session's stand designator doesn't exist in OSM. The returned dict may
    therefore also update the stand variable itself, keeping the spoken
    clearance consistent with the routed geometry.
    """

    if not config.TAXI_ROUTE_AUTOCOMPUTE:
        return None
    spec = TAXI_FLOW_SPECS.get(flow_slug)
    if spec is None or not icao:
        return None

    origin_name = str(variables.get(spec.origin_var) or "").strip()
    dest_name = str(variables.get(spec.dest_var) or "").strip()

    stand_var = spec.origin_var if spec.stand_side == "origin" else spec.dest_var
    stand_name = origin_name if spec.stand_side == "origin" else dest_name

    # Live position only stands in for the *origin* stand (a departure aircraft
    # is parked on it). For arrivals the stand is the destination — the aircraft
    # is nowhere near it at session creation.
    aircraft: tuple[float, float] | None = None
    if (
        spec.stand_side == "origin"
        and aircraft_lat is not None
        and aircraft_lon is not None
        # Guard against the bridge's null island default (0.0/0.0).
        and (abs(aircraft_lat) > 0.5 or abs(aircraft_lon) > 0.5)
    ):
        aircraft = (aircraft_lat, aircraft_lon)

    overpass = client or _get_client()
    try:
        selected_name, origin_coords, spoken_update = _select_stand(
            icao=icao, stand_name=stand_name, aircraft=aircraft, client=overpass
        )
    except Exception as exc:  # noqa: BLE001 - best-effort; never break session create
        logger.warning("Taxi route: stand selection failed for %s: %s", icao, exc)
        return None

    if spec.stand_side == "origin":
        origin_name = selected_name or ""
        origin_lat, origin_lon = (origin_coords or (None, None))
    else:
        dest_name = selected_name or ""
        origin_lat = origin_lon = None

    if (not origin_name and origin_lat is None) or not dest_name:
        return None

    computed = resolve_taxi_clearance(
        icao=icao,
        origin_name=origin_name or None,
        dest_name=dest_name,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        origin_runway_point=spec.origin_runway_point,
        dest_runway_point=spec.dest_runway_point,
        # The flow spec knows which side is the parking stand — that intent
        # disambiguates airports where a stand and a runway share a name.
        origin_prefer="stand" if spec.stand_side == "origin" else "runway",
        dest_prefer="stand" if spec.stand_side == "dest" else "runway",
        client=overpass,
    )
    if computed is not None and spoken_update:
        # Keep the spoken stand in step with the routed one ("stand V155,
        # request startup" must name the stand the route actually starts at).
        computed[stand_var] = spoken_update
    return computed
