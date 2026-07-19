"""Offline regression sweep over the cached OSM fixtures.

Runs the same invariants as ``tools/taxi_sweep.py`` for the German major
airports, but strictly offline: every Overpass response comes from
``tests/fixtures/osm/`` (recorded by a live sweep run). Airports whose
fixtures are missing are skipped, so a fresh checkout without fixtures still
has a green suite.

To (re)record fixtures: ``poetry run python tools/taxi_sweep.py``.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from taxi_sweep import (  # noqa: E402
    DEFAULT_AIRPORTS,
    CachingOverpassClient,
    SweepStats,
    Violation,
    sweep_airport,
)


@pytest.mark.parametrize("icao", DEFAULT_AIRPORTS)
def test_sweep_invariants_hold_offline(icao: str) -> None:
    client = CachingOverpassClient(offline=True)
    violations: list[Violation] = []
    stats = SweepStats()

    try:
        sweep_airport(icao, client, violations, stats, random.Random(42))
    except RuntimeError as exc:
        if "offline mode" in str(exc):
            pytest.skip(f"no OSM fixtures recorded for {icao}")
        raise

    offline_misses = [v for v in violations if "offline mode" in v.detail]
    if offline_misses and len(offline_misses) == len(violations):
        pytest.skip(f"incomplete OSM fixtures for {icao}")

    real = [v for v in violations if "offline mode" not in v.detail]
    details = "\n".join(f"- {v.kind} [{v.direction} {v.origin} -> {v.dest}]: {v.detail}" for v in real)
    assert not real, f"{icao}: {len(real)} sweep violations\n{details}"
    assert stats.combos > 0, f"{icao}: sweep ran no combinations"
