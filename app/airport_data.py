"""Airport reference data: spoken names + real radio frequencies.

Sources (bundled under ``data/``):
  - ``airports.min.csv``            OurAirports, trimmed to ICAO + city + country
  - ``airport_frequencies.min.csv`` OurAirports communication frequencies
  - ``airport_names_de.json``       curated German spoken-city overlay

Two things callers need at session start:

  resolve_airport(icao)
      → AirportInfo with the English spoken city (for ATC TTS), the German
        spoken city if known (for STT/pilot-side matching), and a frequency for
        each logical position.  Positions with no real frequency get a stable
        invented one (deterministic per airport+position) so a flow never renders
        a missing-frequency marker.

  spoken_place_aliases(value)
      → all accepted spoken forms for a place value (English + German), so the
        readback evaluator can match a pilot who mixes languages.

Data is loaded lazily on first use and cached for the process lifetime.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_AIRPORTS_CSV = _DATA_DIR / "airports.min.csv"
_FREQ_CSV = _DATA_DIR / "airport_frequencies.min.csv"
_DE_OVERLAY = _DATA_DIR / "airport_names_de.json"

# Logical position → OurAirports frequency `type` codes, in preference order.
# The frontend/flows speak in logical positions (frequency_name); the dataset
# uses ICAO-ish type codes.  First matching type with a real frequency wins.
_POSITION_TYPES: Dict[str, List[str]] = {
    "atis": ["ATIS"],
    "clearance": ["CLD", "DEL", "DELIVERY"],
    "ground": ["GND", "GROUND"],
    "tower": ["TWR", "TOWER"],
    "approach": ["APP", "ARR"],
    "departure": ["DEP"],
    "director": ["DIR"],
    "center": ["CTR", "CNTR", "ACC"],
}

# frequency_name (as used in the YAML flows) → logical position key above.
_FREQ_NAME_TO_POSITION: Dict[str, str] = {
    "atis": "atis",
    "clearance delivery": "clearance",
    "delivery": "clearance",
    "ground": "ground",
    "tower": "tower",
    "approach": "approach",
    "departure": "departure",
    "director": "director",
    "center": "center",
    "centre": "center",
    "radar": "approach",
}

# Invented-frequency band: civil airband VHF, 25 kHz channels, avoiding the
# guard frequency 121.500.  Used only when no real frequency exists.
_INVENT_LOW_KHZ = 118_000
_INVENT_HIGH_KHZ = 136_975
_GUARD_KHZ = 121_500


@dataclass
class AirportInfo:
    icao: str
    city_en: str                       # spoken city for ATC TTS (always English)
    city_de: Optional[str]             # spoken city in German, if known
    country: Optional[str]
    name: Optional[str]                # full facility name (e.g. "Munich Airport")
    # logical position -> "MHz string", every position present (real or invented)
    frequencies: Dict[str, str] = field(default_factory=dict)
    # logical position -> True when the frequency was invented (no real data)
    invented: Dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lazy data loading
# ---------------------------------------------------------------------------

_airports: Optional[Dict[str, Dict[str, str]]] = None
_freqs: Optional[Dict[str, Dict[str, List[str]]]] = None
_de_names: Optional[Dict[str, str]] = None


def _load() -> None:
    global _airports, _freqs, _de_names
    if _airports is not None:
        return

    airports: Dict[str, Dict[str, str]] = {}
    if _AIRPORTS_CSV.exists():
        with _AIRPORTS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                icao = row["icao"].strip().upper()
                if icao:
                    airports[icao] = {
                        "city": row.get("municipality", "").strip(),
                        "country": row.get("country", "").strip(),
                        "name": row.get("name", "").strip(),
                        "lat": row.get("lat", "").strip(),
                        "lon": row.get("lon", "").strip(),
                    }
    else:
        logger.warning("Airport dataset missing at %s — names/frequencies unavailable", _AIRPORTS_CSV)

    freqs: Dict[str, Dict[str, List[str]]] = {}
    if _FREQ_CSV.exists():
        with _FREQ_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                icao = row["icao"].strip().upper()
                ftype = row["type"].strip().upper()
                mhz = row["frequency_mhz"].strip()
                if not (icao and ftype and mhz):
                    continue
                freqs.setdefault(icao, {}).setdefault(ftype, []).append(mhz)

    de_names: Dict[str, str] = {}
    if _DE_OVERLAY.exists():
        raw = json.loads(_DE_OVERLAY.read_text(encoding="utf-8"))
        de_names = {k.upper(): v for k, v in raw.items() if not k.startswith("_")}

    _airports, _freqs, _de_names = airports, freqs, de_names
    logger.info(
        "Airport data loaded: %d airports, %d with frequencies, %d German names",
        len(airports), len(freqs), len(de_names),
    )


# ---------------------------------------------------------------------------
# Frequency helpers
# ---------------------------------------------------------------------------

def _format_mhz(mhz: str) -> str:
    """Normalise '121.8' / '121.83' to a 3-decimal MHz string '121.800'."""
    try:
        return f"{float(mhz):.3f}"
    except ValueError:
        return mhz


def invent_frequency(icao: str, position: str) -> str:
    """Deterministic plausible VHF frequency for a missing position.

    Stable for a given (airport, position) so it does not change between the
    handoff instruction and the readback within a session.
    """
    # Stable across processes/restarts (unlike the salted builtin hash()).
    digest = hashlib.md5(f"{icao.upper()}|{position}".encode()).hexdigest()
    span = (_INVENT_HIGH_KHZ - _INVENT_LOW_KHZ) // 25
    khz = _INVENT_LOW_KHZ + (int(digest, 16) % span) * 25
    if khz == _GUARD_KHZ:
        khz += 25
    return f"{khz / 1000:.3f}"


def _real_frequency(icao: str, position: str) -> Optional[str]:
    assert _freqs is not None
    by_type = _freqs.get(icao, {})
    for ftype in _POSITION_TYPES.get(position, []):
        if by_type.get(ftype):
            return _format_mhz(by_type[ftype][0])
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_airport(icao: str, positions: Optional[List[str]] = None) -> Optional[AirportInfo]:
    """Resolve names + frequencies for an ICAO code.

    ``positions`` limits which logical positions to resolve; defaults to all.
    Returns None if the code is not a known airport (caller decides fallback).
    """
    _load()
    assert _airports is not None and _de_names is not None
    code = icao.strip().upper()
    rec = _airports.get(code)
    if rec is None:
        return None

    wanted = positions or list(_POSITION_TYPES.keys())
    info = AirportInfo(
        icao=code,
        city_en=rec["city"] or rec["name"] or code,
        city_de=_de_names.get(code),
        country=rec.get("country") or None,
        name=rec.get("name") or None,
    )
    for pos in wanted:
        real = _real_frequency(code, pos)
        if real is not None:
            info.frequencies[pos] = real
            info.invented[pos] = False
        else:
            info.frequencies[pos] = invent_frequency(code, pos)
            info.invented[pos] = True
    return info


def airport_coords(icao: Optional[str]) -> Optional[Tuple[float, float]]:
    """(lat, lon) of an airport's reference point, or None if unknown.

    Used to derive distance_to_dep_nm / distance_to_dest_nm from live
    telemetry positions.
    """
    if not icao:
        return None
    _load()
    assert _airports is not None
    rec = _airports.get(icao.strip().upper())
    if rec is None or not rec.get("lat") or not rec.get("lon"):
        return None
    try:
        return float(rec["lat"]), float(rec["lon"])
    except ValueError:
        return None


def is_icao_airport(value: str) -> bool:
    """True when ``value`` is a known 4-letter ICAO airport code."""
    _load()
    assert _airports is not None
    v = value.strip().upper()
    return len(v) == 4 and v.isalpha() and v in _airports


def spoken_place_aliases(value: str) -> List[str]:
    """All accepted spoken forms for a place value.

    Accepts an ICAO code or an already-resolved city name.  Returns the English
    and (if known) German city names so a language-mixing pilot is matched.
    """
    _load()
    assert _airports is not None and _de_names is not None
    v = value.strip()
    forms: List[str] = [v]
    up = v.upper()
    rec = _airports.get(up)
    if rec:
        if rec["city"]:
            forms.append(rec["city"])
        de = _de_names.get(up)
        if de:
            forms.append(de)
    else:
        # value may already be a city name; add its German counterpart if any
        # airport's English city matches it.
        for icao, r in _airports.items():
            if r["city"].lower() == v.lower():
                de = _de_names.get(icao)
                if de:
                    forms.append(de)
                break
    # de-dupe preserving order
    seen, out = set(), []
    for f in forms:
        k = f.lower()
        if f and k not in seen:
            seen.add(k)
            out.append(f)
    return out


def freq_name_to_position(frequency_name: str) -> Optional[str]:
    """Map a flow ``frequency_name`` to a logical position key."""
    return _FREQ_NAME_TO_POSITION.get(frequency_name.strip().lower())


@dataclass
class AirportResolution:
    """Result of resolving the station/destination airports for a session."""
    variables: Dict[str, str] = field(default_factory=dict)
    station: Optional[AirportInfo] = None
    destination: Optional[AirportInfo] = None


def resolve_for_session(
    airport_icao: Optional[str], destination_icao: Optional[str]
) -> AirportResolution:
    """Build the variable overrides for a new session from airport codes.

    - The station airport supplies every ``<position>_freq`` variable
      (ground_freq, tower_freq, departure_freq, director_freq, …) from real
      data, inventing any position the field does not publish.
    - The destination airport supplies the spoken ``destination`` city (English).

    Unknown codes resolve to nothing (caller keeps the flow's YAML defaults).
    These values are *defaults*: explicit caller-supplied variables override them.
    """
    res = AirportResolution()
    if airport_icao:
        res.station = resolve_airport(airport_icao)
        if res.station:
            for pos, freq in res.station.frequencies.items():
                res.variables[f"{pos}_freq"] = freq
            res.variables["airport"] = res.station.city_en
    if destination_icao:
        res.destination = resolve_airport(destination_icao)
        if res.destination:
            res.variables["destination"] = res.destination.city_en
    return res
