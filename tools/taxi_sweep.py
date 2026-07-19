"""Systematic taxi-clearance sweep over German major airports.

Runs stand x runway-end combinations through the same code path the live
flows use (``calculate_taxi_route`` + the spoken-clearance derivation of
``taxi_route_service``) and checks a set of invariants on every result.
Violations land in a markdown report; confirmed defects become regression
tests.

Overpass responses are cached on disk under ``tests/fixtures/osm/`` so the
first run is live and every later run (including pytest) is offline and
reproducible.

Usage:
    poetry run python tools/taxi_sweep.py [--airports EDDF,EDDM] [--report PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.airport_geocode import (  # noqa: E402
    AirportFeature,
    GeocodeQuery,
    OverpassClient,
    fetch_airport_features,
)
from app.taxi_route_service import _hold_clause, phoneticize_route  # noqa: E402
from app.taxi_routing import TaxiRouteError, calculate_taxi_route  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "osm"

DEFAULT_AIRPORTS = [
    "EDDF", "EDDM", "EDDB", "EDDH", "EDDL",
    "EDDK", "EDDS", "EDDV", "EDDN", "EDDW",
]

MAX_STANDS_PER_AIRPORT = 12
MAX_RUNWAY_ENDS_PER_AIRPORT = 4
# Real taxi routes at even the largest German field stay well under this.
MAX_PLAUSIBLE_ROUTE_M = 8000.0
MIN_PLAUSIBLE_ROUTE_M = 30.0

RUNWAY_END_RE = re.compile(r"^\d{2}[LRC]?$")
# What a spoken route may contain after phoneticization.
SPOKEN_RE = re.compile(r"^[A-Za-z0-9 ,\-]+$")


class CachingOverpassClient(OverpassClient):
    """Overpass client with a persistent on-disk cache (fixture store).

    Cache key is the query hash; the stored file keeps the query itself so
    fixtures stay inspectable. ``offline=True`` raises instead of hitting the
    network — that is what the regression tests use.
    """

    def __init__(self, *, offline: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.offline = offline
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def _fixture_path(self, query: str) -> Path:
        digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
        return FIXTURE_DIR / f"overpass_{digest}.json"

    def fetch_json(self, query: str) -> dict[str, Any]:
        path = self._fixture_path(query)
        if path.exists():
            stored = json.loads(path.read_text())
            return stored["response"]
        if self.offline:
            raise RuntimeError(f"offline mode: no fixture for query hash {path.name}")
        # Public Overpass instances rate-limit aggressively; be polite on cache
        # misses and back off on 429/504 instead of recording a bogus failure.
        last_error: Exception | None = None
        for attempt in range(5):
            if attempt:
                time.sleep(5.0 * attempt)
            try:
                response = super().fetch_json(query)
            except Exception as exc:  # noqa: BLE001 - retry only transient codes
                last_error = exc
                if any(code in str(exc) for code in ("429", "504", "502")):
                    continue
                raise
            path.write_text(json.dumps({"query": query, "response": response}))
            time.sleep(1.0)
            return response
        raise last_error  # type: ignore[misc]


@dataclass
class Violation:
    airport: str
    direction: str
    origin: str
    dest: str
    kind: str
    detail: str


@dataclass
class SweepStats:
    combos: int = 0
    ok: int = 0
    no_route: int = 0
    errors: int = 0
    kinds: Counter = field(default_factory=Counter)


def runway_ends(features: list[AirportFeature]) -> list[str]:
    """All runway-end designators of the airport ("07C", "25L", ...)."""
    ends: set[str] = set()
    for feature in features:
        if feature.type != "runway":
            continue
        for alias in feature.aliases:
            for part in re.split(r"[/\s]+", alias.strip()):
                token = part.strip().upper()
                if RUNWAY_END_RE.match(token):
                    ends.add(token)
    return sorted(ends)


def stand_names(features: list[AirportFeature]) -> list[str]:
    return sorted({
        f.primary_alias.strip()
        for f in features
        if f.type in ("stand", "gate") and f.primary_alias.strip()
    })


def check_result(
    *,
    airport: str,
    direction: str,
    origin: str,
    dest: str,
    result: dict[str, Any],
    violations: list[Violation],
) -> bool:
    """Apply the invariants to one route result. Returns True when clean."""

    def flag(kind: str, detail: str) -> None:
        violations.append(Violation(airport, direction, origin, dest, kind, detail))

    if result.get("route") is None:
        diag = result.get("diagnostics") or {}
        flag(
            "no_route",
            f"no path (same_component={diag.get('same_component')}, "
            f"attach {diag.get('start_attach_distance_m')}m/{diag.get('end_attach_distance_m')}m)",
        )
        return False

    clean = True
    names = result.get("names") or []
    collapsed = result.get("names_collapsed") or []

    if not collapsed:
        flag("empty_names", "route found but no taxiway names at all")
        return False

    for name in collapsed:
        if not name.strip():
            flag("blank_name", f"blank segment in {collapsed}")
            clean = False
        if "unnamed" in name.lower():
            flag("unnamed_segment", f"'{name}' in {collapsed}")
            clean = False
        if RUNWAY_END_RE.match(name.strip().upper()):
            flag("runway_as_taxiway", f"route taxis 'via {name}' — a runway designator, in {collapsed}")
            clean = False
        if len(name) > 24:
            flag("suspicious_name", f"overlong segment '{name}'")
            clean = False

    for first, second in zip(collapsed, collapsed[1:]):
        if first == second:
            flag("consecutive_duplicate", f"'{first}' twice in a row in {collapsed}")
            clean = False

    spoken = phoneticize_route(", ".join(collapsed))
    if not SPOKEN_RE.match(spoken):
        flag("unspeakable_route", f"'{spoken}'")
        clean = False

    crossings = [str(c) for c in (result.get("crossings") or [])]
    for crossing in crossings:
        if not RUNWAY_END_RE.match(crossing.strip().upper()):
            flag("bad_crossing_designator", f"'{crossing}' in {crossings}")
            clean = False
    hold = _hold_clause(crossings)
    if bool(crossings) != bool(hold):
        flag("hold_clause_mismatch", f"crossings={crossings} clause='{hold}'")
        clean = False

    distance = (result.get("route") or {}).get("total_distance_m")
    if distance is not None:
        if distance > MAX_PLAUSIBLE_ROUTE_M:
            flag("route_too_long", f"{distance:.0f} m via {collapsed}")
            clean = False
        if distance < MIN_PLAUSIBLE_ROUTE_M:
            flag("route_too_short", f"{distance:.0f} m via {collapsed} ({len(names)} raw segments)")
            clean = False

    return clean


def sweep_airport(
    icao: str,
    client: CachingOverpassClient,
    violations: list[Violation],
    stats: SweepStats,
    rng: random.Random,
) -> None:
    features = fetch_airport_features(icao, client=client)
    stands = stand_names(features)
    ends = runway_ends(features)

    if not stands:
        violations.append(Violation(icao, "-", "-", "-", "no_stands", "no stand/gate features in OSM"))
        return
    if not ends:
        violations.append(Violation(icao, "-", "-", "-", "no_runways", "no runway-end designators in OSM"))
        return

    picked_stands = stands if len(stands) <= MAX_STANDS_PER_AIRPORT else rng.sample(stands, MAX_STANDS_PER_AIRPORT)
    picked_ends = ends if len(ends) <= MAX_RUNWAY_ENDS_PER_AIRPORT else rng.sample(ends, MAX_RUNWAY_ENDS_PER_AIRPORT)

    for stand in picked_stands:
        for end in picked_ends:
            for direction in ("departure", "arrival"):
                stats.combos += 1
                if direction == "departure":
                    origin = GeocodeQuery(name=stand, prefer="stand")
                    dest = GeocodeQuery(name=end, runway_point="start", prefer="runway")
                    o_label, d_label = stand, end
                else:
                    origin = GeocodeQuery(name=end, runway_point="end", prefer="runway")
                    dest = GeocodeQuery(name=stand, prefer="stand")
                    o_label, d_label = end, stand
                try:
                    result = calculate_taxi_route(
                        origin=origin, dest=dest, airport=icao, client=client
                    )
                except TaxiRouteError as exc:
                    stats.errors += 1
                    stats.kinds[f"error_{exc.code}"] += 1
                    violations.append(
                        Violation(icao, direction, o_label, d_label, f"error_{exc.code}", str(exc))
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - report, keep sweeping
                    stats.errors += 1
                    stats.kinds["error_unexpected"] += 1
                    violations.append(
                        Violation(icao, direction, o_label, d_label, "error_unexpected", repr(exc))
                    )
                    continue

                before = len(violations)
                if check_result(
                    airport=icao,
                    direction=direction,
                    origin=o_label,
                    dest=d_label,
                    result=result,
                    violations=violations,
                ):
                    stats.ok += 1
                else:
                    if result.get("route") is None:
                        stats.no_route += 1
                for violation in violations[before:]:
                    stats.kinds[violation.kind] += 1


def write_report(path: Path, airports: list[str], stats: SweepStats, violations: list[Violation]) -> None:
    lines = [
        "# Taxi Sweep Report",
        "",
        f"Airports: {', '.join(airports)}",
        f"Combos checked: {stats.combos} — clean: {stats.ok}, "
        f"no-route: {stats.no_route}, errors: {stats.errors}",
        "",
        "## Violations by kind",
        "",
    ]
    for kind, count in stats.kinds.most_common():
        lines.append(f"- `{kind}`: {count}")
    if not stats.kinds:
        lines.append("- none 🎉")
    lines.append("")
    lines.append("## Details")
    lines.append("")
    current = None
    for v in violations:
        if v.airport != current:
            current = v.airport
            lines.append(f"### {v.airport}")
            lines.append("")
        lines.append(f"- **{v.kind}** [{v.direction} {v.origin} → {v.dest}]: {v.detail}")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airports", default=",".join(DEFAULT_AIRPORTS))
    parser.add_argument("--report", default=str(REPO_ROOT / "docs" / "taxi-sweep-report.md"))
    parser.add_argument("--offline", action="store_true", help="fail instead of hitting Overpass")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    airports = [a.strip().upper() for a in args.airports.split(",") if a.strip()]
    client = CachingOverpassClient(offline=args.offline, timeout_seconds=60.0)
    rng = random.Random(args.seed)
    violations: list[Violation] = []
    stats = SweepStats()

    for icao in airports:
        started = time.time()
        try:
            sweep_airport(icao, client, violations, stats, rng)
        except Exception as exc:  # noqa: BLE001 - airport-level failure, keep sweeping
            violations.append(Violation(icao, "-", "-", "-", "airport_failed", repr(exc)))
            stats.kinds["airport_failed"] += 1
        print(f"{icao}: done in {time.time() - started:.1f}s "
              f"(total violations so far: {len(violations)})", flush=True)

    write_report(Path(args.report), airports, stats, violations)
    print(f"\nReport: {args.report}")
    print(f"Combos: {stats.combos}, clean: {stats.ok}, violations: {len(violations)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
