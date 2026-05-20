"""Load and cache DecisionFlow objects from YAML files.

Slugs follow the convention ``{base}-v{N}`` (e.g. ``clearance-v1``).
``get_flow("clearance")`` resolves to the highest available version automatically.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

import yaml

from app.models import DecisionFlow

logger = logging.getLogger(__name__)

_flow_cache: Dict[str, DecisionFlow] = {}

_VERSION_RE = re.compile(r"^(.+)-v(\d+)$")


def load_flow_from_file(path: Path) -> DecisionFlow:
    """Parse one YAML file into a DecisionFlow, injecting state IDs."""
    raw = yaml.safe_load(path.read_text())
    flow = DecisionFlow.model_validate(raw)
    logger.info("Loaded flow '%s' from %s (%d states)", flow.slug, path.name, len(flow.states))
    return flow


def load_all_flows(flows_dir: Path) -> Dict[str, DecisionFlow]:
    """Load all *.yaml files from flows_dir, keyed by versioned slug."""
    global _flow_cache
    _flow_cache = {}

    for yaml_file in sorted(flows_dir.glob("*.yaml")):
        try:
            flow = load_flow_from_file(yaml_file)
            if flow.slug in _flow_cache:
                logger.warning(
                    "Duplicate slug '%s' in %s — overwriting previous",
                    flow.slug,
                    yaml_file.name,
                )
            _flow_cache[flow.slug] = flow
        except Exception as exc:
            logger.error("Failed to load %s: %s", yaml_file.name, exc)
            raise

    logger.info("Loaded %d flows: %s", len(_flow_cache), list(_flow_cache.keys()))
    return _flow_cache


def _resolve_latest(base_slug: str) -> Optional[DecisionFlow]:
    """Return the highest-versioned flow matching ``{base_slug}-v{N}``, or None."""
    candidates = []
    for key, flow in _flow_cache.items():
        m = _VERSION_RE.match(key)
        if m and m.group(1) == base_slug:
            candidates.append((int(m.group(2)), flow))
    if not candidates:
        return None
    _, latest = max(candidates, key=lambda t: t[0])
    return latest


def get_flow(slug: str) -> DecisionFlow:
    """Retrieve a flow by slug.

    Accepts both the full versioned slug (``clearance-v1``) and the bare base
    name (``clearance``).  The bare name resolves to the highest available version.
    """
    if slug in _flow_cache:
        return _flow_cache[slug]

    flow = _resolve_latest(slug)
    if flow is not None:
        return flow

    raise KeyError(f"Flow '{slug}' not found. Available: {list(_flow_cache.keys())}")


def get_all_flows() -> Dict[str, DecisionFlow]:
    return dict(_flow_cache)


def reload_flows(flows_dir: Path) -> Dict[str, DecisionFlow]:
    """Re-load all flows from disk (for hot reload endpoint)."""
    logger.info("Reloading flows from %s", flows_dir)
    return load_all_flows(flows_dir)
