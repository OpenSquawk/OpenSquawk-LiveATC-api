"""Load and cache DecisionFlow objects from YAML files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

import yaml

from app.models import DecisionFlow

logger = logging.getLogger(__name__)

_flow_cache: Dict[str, DecisionFlow] = {}


def load_flow_from_file(path: Path) -> DecisionFlow:
    """Parse one YAML file into a DecisionFlow, injecting state IDs."""
    raw = yaml.safe_load(path.read_text())
    flow = DecisionFlow.model_validate(raw)
    logger.info("Loaded flow '%s' from %s (%d states)", flow.slug, path.name, len(flow.states))
    return flow


def load_all_flows(flows_dir: Path) -> Dict[str, DecisionFlow]:
    """Load all *.yaml files from flows_dir, keyed by slug."""
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


def get_flow(slug: str) -> DecisionFlow:
    """Retrieve a cached flow by slug; raises KeyError if not found."""
    if slug not in _flow_cache:
        raise KeyError(f"Flow '{slug}' not found. Available: {list(_flow_cache.keys())}")
    return _flow_cache[slug]


def get_all_flows() -> Dict[str, DecisionFlow]:
    return dict(_flow_cache)


def reload_flows(flows_dir: Path) -> Dict[str, DecisionFlow]:
    """Re-load all flows from disk (for hot reload endpoint)."""
    logger.info("Reloading flows from %s", flows_dir)
    return load_all_flows(flows_dir)
