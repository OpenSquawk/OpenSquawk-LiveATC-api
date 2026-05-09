"""Render say_template / expected_pilot_template strings.

Template syntax: {{variable_name}} — replaced with session variable values.
Unknown variables are left as-is with a warning marker.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_template(template: Optional[str], variables: Dict[str, Any]) -> Optional[str]:
    if template is None:
        return None

    def replace(match: re.Match) -> str:
        key = match.group(1)
        val = variables.get(key)
        if val is None:
            return f"[{key}?]"  # Visible marker for missing variables
        return str(val)

    return _PLACEHOLDER.sub(replace, template)
