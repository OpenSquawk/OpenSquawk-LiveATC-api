"""Evaluate pilot readback correctness.

Simple mode: check whether the current value of each required variable
appears in the pilot utterance — either as the literal value or in any
of its standard spoken (phonetic) forms.

Phonetic forms are inferred from the value's pattern:
  - Frequency   "121.805"  → "one two one decimal eight zero five"
  - Flight level "FL150"   → "flight level one five zero"
  - Runway       "25L"     → "two five left"
  - Integer      "5000"    → "five thousand" OR "five zero zero zero"
  - ICAO ident   "SULUS5S" → sequential phonetic regex (Sierra Uniform Lima …)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# ICAO phonetic alphabet + digit pronunciation tables
# ---------------------------------------------------------------------------

_LETTER_PHONETICS: Dict[str, str] = {
    'A': 'alpha',    'B': 'bravo',    'C': 'charlie', 'D': 'delta',
    'E': 'echo',     'F': 'foxtrot',  'G': 'golf',    'H': 'hotel',
    'I': 'india',    'J': 'juliet',   'K': 'kilo',    'L': 'lima',
    'M': 'mike',     'N': 'november', 'O': 'oscar',   'P': 'papa',
    'Q': 'quebec',   'R': 'romeo',    'S': 'sierra',  'T': 'tango',
    'U': 'uniform',  'V': 'victor',   'W': 'whiskey', 'X': 'x.?ray',
    'Y': 'yankee',   'Z': 'zulu',
}

# Each digit maps to a regex alternation that accepts:
#   - the digit character itself (STT may leave numbers as digits)
#   - the standard English word
#   - the ICAO aviation pronunciation variant
_DIGIT_PHONETICS: Dict[str, str] = {
    '0': r'(?:0|zero)',
    '1': r'(?:1|one|wun)',
    '2': r'(?:2|two|too)',
    '3': r'(?:3|three|tree)',
    '4': r'(?:4|four|fower)',
    '5': r'(?:5|five|fife)',
    '6': r'(?:6|six)',
    '7': r'(?:7|seven)',
    '8': r'(?:8|eight)',
    '9': r'(?:9|nine|niner)',
}

# Simple word list for building spoken altitude / frequency strings.
_DIGIT_WORDS = [
    'zero', 'one', 'two', 'three', 'four',
    'five', 'six', 'seven', 'eight', 'nine',
]


# ---------------------------------------------------------------------------
# Spoken-form generators
# ---------------------------------------------------------------------------

def _icao_digits(value: str) -> str:
    """Spell each digit individually: '2118' → 'two one one eight'."""
    return ' '.join(_DIGIT_WORDS[int(c)] for c in value if c.isdigit())


def _altitude_speak(value: str) -> str:
    """
    Group-thousands pronunciation: '5000' → 'five thousand',
    '1500' → 'one thousand five hundred'.
    Returns empty string when value is not a plain integer.
    """
    try:
        n = int(value)
    except ValueError:
        return ''
    if n == 0:
        return 'zero'
    parts: List[str] = []
    if n >= 1000:
        th = n // 1000
        if th < 10:
            parts.append(f'{_DIGIT_WORDS[th]} thousand')
        else:
            # e.g. 10000 → "one zero thousand" (uncommon but safe)
            parts.append(f'{_icao_digits(str(th))} thousand')
        rem = n % 1000
        if rem >= 100:
            parts.append(f'{_DIGIT_WORDS[rem // 100]} hundred')
    else:
        parts.append(_icao_digits(str(n)))
    return ' '.join(parts)


def _frequency_speak(value: str) -> str:
    """'121.805' → 'one two one decimal eight zero five'."""
    parts = value.split('.')
    integer_spoken = ' '.join(_DIGIT_WORDS[int(d)] for d in parts[0] if d.isdigit())
    if len(parts) > 1:
        decimal_spoken = ' '.join(_DIGIT_WORDS[int(d)] for d in parts[1] if d.isdigit())
        return f'{integer_spoken} decimal {decimal_spoken}'
    return integer_spoken


def _flight_level_speak(value: str) -> str:
    """'FL150' → 'flight level one five zero'."""
    digits = re.sub(r'^FL', '', value, flags=re.IGNORECASE)
    spoken = ' '.join(_DIGIT_WORDS[int(d)] for d in digits if d.isdigit())
    return f'flight level {spoken}'


def _runway_speak(value: str) -> str:
    """'25L' → 'two five left', '07' → 'zero seven'."""
    m = re.match(r'^(\d{2})([LCR]?)$', value, re.IGNORECASE)
    if not m:
        return ''
    digits = ' '.join(_DIGIT_WORDS[int(d)] for d in m.group(1))
    suffix = {'L': 'left', 'R': 'right', 'C': 'center'}.get(m.group(2).upper(), '')
    return f'{digits} {suffix}'.strip()


def _icao_identifier_regex(value: str) -> str:
    """
    Build a regex that matches the ICAO phonetic spelling of an alphanumeric
    identifier spoken character-by-character (SID names, waypoints, etc.).

    'SULUS5S' → pattern matching
        "Sierra Uniform Lima Uniform Sierra 5 Sierra"  (or with commas, etc.)

    Characters are separated by an optional whitespace/punctuation separator
    so natural TTS output such as "Sierra, Uniform, Lima, …" is accepted.
    """
    parts: List[str] = []
    for ch in value.upper():
        if ch in _LETTER_PHONETICS:
            parts.append(_LETTER_PHONETICS[ch])
        elif ch in _DIGIT_PHONETICS:
            parts.append(_DIGIT_PHONETICS[ch])
        else:
            parts.append(re.escape(ch))
    # Allow optional whitespace, commas, hyphens, dots between words
    sep = r'[\s,.\-]*'
    return sep.join(parts)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def spoken_forms(value: str) -> List[str]:
    """
    Return all accepted *literal* spoken forms for a variable value.
    Each item is a plain string (not a regex).  The caller may also need
    _icao_identifier_regex() for multi-character ICAO idents.
    """
    v = value.strip()
    forms: List[str] = [v.lower()]  # literal is always accepted

    if re.match(r'^\d{3}\.\d+$', v):
        forms.append(_frequency_speak(v))

    if re.match(r'^FL\d+$', v, re.IGNORECASE):
        forms.append(_flight_level_speak(v))

    if re.match(r'^\d{2}[LCR]?$', v, re.IGNORECASE):
        s = _runway_speak(v)
        if s:
            forms.append(s)

    if re.match(r'^\d+$', v):
        forms.append(_icao_digits(v))     # digit-by-digit: "five zero zero zero"
        forms.append(_altitude_speak(v))  # grouped: "five thousand"

    return [f for f in forms if f]


def _value_is_icao_ident(value: str) -> bool:
    """
    True when the value looks like a multi-character ICAO alphanumeric
    identifier (SID, STAR, waypoint, …) that would be spoken phonetically.

    Must contain at least one letter and consist only of A-Z / 0-9.
    Excludes pure-digit strings (those are altitudes/squawks) and FL* codes
    (handled by _flight_level_speak).
    """
    v = value.strip().upper()
    return (
        bool(re.match(r'^[A-Z0-9]{2,}$', v))
        and bool(re.search(r'[A-Z]', v))
        and not re.match(r'^FL\d+$', v)
        and not re.match(r'^\d{2}[LCR]?$', v)
    )


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------

def evaluate_readback_simple(
    pilot_utterance: str,
    readback_required: List[str],
    variables: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Returns (passed, missing_fields).

    A field passes if the current variable value is found in the utterance
    as any of:
      1. The literal value (case-insensitive substring match)
      2. Any standard spoken form (frequency, FL, runway, altitude, …)
      3. For ICAO alphanumeric idents: the ICAO phonetic sequential regex
         (e.g. "SULUS5S" → Sierra Uniform Lima Uniform Sierra 5 Sierra)
    """
    missing = []
    utterance = pilot_utterance

    for field in readback_required:
        expected = variables.get(field)
        if expected is None:
            missing.append(field)
            continue
        expected_str = str(expected).strip()
        if not expected_str:
            continue

        found = False

        # 1. Check literal value and all static spoken forms
        for form in spoken_forms(expected_str):
            if re.search(re.escape(form), utterance, re.IGNORECASE):
                found = True
                break

        # 2. Check ICAO phonetic sequential pattern for identifiers
        if not found and _value_is_icao_ident(expected_str):
            pattern = _icao_identifier_regex(expected_str)
            try:
                if re.search(pattern, utterance, re.IGNORECASE):
                    found = True
            except re.error:
                pass  # malformed pattern — skip

        if not found:
            missing.append(field)

    return (len(missing) == 0), missing


def check_readback(
    pilot_utterance: str,
    readback_required: List[str],
    readback_mode: str,
    variables: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Dispatch to the appropriate readback evaluator.

    Returns (passed, missing_fields).
    """
    if readback_mode == "none" or not readback_required:
        return True, []

    if readback_mode in ("simple", "strict"):
        # strict is reserved for future stricter matching; uses simple for now
        return evaluate_readback_simple(pilot_utterance, readback_required, variables)

    return True, []
