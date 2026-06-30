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
import re
from dataclasses import dataclass
from typing import Any, Mapping

from app import config
from app.airport_geocode import GeocodeQuery, OverpassClient, RunwayPoint
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
    the corresponding endpoint resolves to a runway way.
    """

    origin_var: str
    dest_var: str
    origin_runway_point: RunwayPoint = "start"
    dest_runway_point: RunwayPoint = "start"


# Departure: park stand -> departure runway holding point.
# Arrival: vacated runway -> parking stand.
TAXI_FLOW_SPECS: dict[str, TaxiRoutingSpec] = {
    "taxi-v1": TaxiRoutingSpec(origin_var="stand", dest_var="runway"),
    "taxi-in-v1": TaxiRoutingSpec(
        origin_var="runway",
        dest_var="parking_stand",
        origin_runway_point="end",
    ),
}

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
    origin_name: str,
    dest_name: str,
    origin_runway_point: RunwayPoint = "start",
    dest_runway_point: RunwayPoint = "start",
    client: OverpassClient | None = None,
) -> dict[str, Any] | None:
    """Compute the taxi clearance variables for one route, or ``None``.

    Returns the session-variable overrides the flow expects:
      - ``taxi_route``: spoken collapsed taxiway sequence ("Lima 7, November")
      - ``crossing_runways``: list of crossed runways (for readback grading)
      - ``taxi_hold_clause``: ready-to-speak hold-short clause, or ""

    ``None`` on any resolution failure or empty route, so the caller keeps the
    flow's YAML defaults.
    """

    origin = GeocodeQuery(name=origin_name, runway_point=origin_runway_point)
    dest = GeocodeQuery(name=dest_name, runway_point=dest_runway_point)
    try:
        result = calculate_taxi_route(
            origin=origin,
            dest=dest,
            airport=icao,
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


def maybe_compute_taxi_route(
    *,
    flow_slug: str,
    icao: str | None,
    variables: Mapping[str, Any],
    client: OverpassClient | None = None,
) -> dict[str, Any] | None:
    """Compute taxi clearance variables for a flow, or ``None``.

    ``variables`` is the merged session variable map (YAML defaults + overrides),
    so per-session stand/runway overrides are respected. Returns ``None`` when
    autocompute is off, the flow is not a taxi flow, the ICAO is missing, or the
    endpoint variables are blank.
    """

    if not config.TAXI_ROUTE_AUTOCOMPUTE:
        return None
    spec = TAXI_FLOW_SPECS.get(flow_slug)
    if spec is None or not icao:
        return None

    origin_name = str(variables.get(spec.origin_var) or "").strip()
    dest_name = str(variables.get(spec.dest_var) or "").strip()
    if not origin_name or not dest_name:
        return None

    return resolve_taxi_clearance(
        icao=icao,
        origin_name=origin_name,
        dest_name=dest_name,
        origin_runway_point=spec.origin_runway_point,
        dest_runway_point=spec.dest_runway_point,
        client=client,
    )
