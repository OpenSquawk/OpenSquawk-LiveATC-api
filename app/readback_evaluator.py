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
from typing import Any, Dict, List, Optional, Tuple

import jellyfish


# ---------------------------------------------------------------------------
# ICAO phonetic alphabet + digit pronunciation tables
# ---------------------------------------------------------------------------

# Values are regex fragments (used inside larger patterns), so spelling variants
# that STT routinely emits are accepted via alternations: "alfa" for alpha,
# "juliett" for juliet.  Alternations are wrapped in (?:…) so they stay atomic
# when joined into a sequential identifier pattern.
_LETTER_PHONETICS: Dict[str, str] = {
    'A': '(?:alpha|alfa)', 'B': 'bravo',  'C': 'charlie', 'D': 'delta',
    'E': 'echo',     'F': 'foxtrot',  'G': 'golf',    'H': 'hotel',
    'I': 'india',    'J': '(?:juliet|juliett)', 'K': 'kilo', 'L': 'lima',
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

# Separator between digits in a spoken sequence: whitespace/punctuation, and
# optionally a spoken decimal point ("decimal"/"point"/"dot") so a frequency
# read "wun too fife decimal tree fife zero" matches the digit run "125350".
_DIGIT_SEQ_SEP = r'[\s,.\-]*(?:(?:decimal|point|dot|comma)[\s,.\-]*)?'

# Any single spoken digit (bare digit, English word, or ICAO variant). Used as a
# negative-lookahead anchor so a trailing-zero-dropped frequency only matches
# when no further digit follows (else a wrong final digit would match the prefix).
_ANY_DIGIT_WORD = (
    r'(?:[0-9]|zero|one|wun|won|two|too|three|tree|four|fower|'
    r'five|fife|six|seven|eight|nine|niner)'
)


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
        # Round flight levels are spoken in "hundreds": FL100 → "flight level
        # one hundred", FL200 → "two hundred".  Accept with or without the
        # leading "flight level".
        fl_num = int(re.sub(r'^FL', '', v, flags=re.IGNORECASE))
        if fl_num and fl_num % 100 == 0 and fl_num // 100 < 10:
            forms.append(f'flight level {_DIGIT_WORDS[fl_num // 100]} hundred')
            forms.append(f'{_DIGIT_WORDS[fl_num // 100]} hundred')

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
# Fuzzy SID/STAR matching
# ---------------------------------------------------------------------------
# Named procedures (SIDs/STARs) are spoken as a *pronounceable word* plus a
# revision digit and a final letter — "TOBAK2E" is said "Tobak two echo", not
# spelled "Tango Oscar Bravo Alpha Kilo two echo". STT then mangles the word
# ("Tobacco too Echo", "Maroon seven foxtrot"). Neither the literal nor the
# letter-by-letter phonetic regex matches that. We therefore decompose the
# ident into stem + digit(s) + final letter and match each part leniently: the
# digit and letter strictly (digit/word, letter/phonetic), the stem fuzzily
# via jellyfish (Metaphone equality or a high Jaro-Winkler similarity).

# Jaro-Winkler floor for accepting a stem.  tobak~toback=0.97, tobak~tobac=0.92,
# tobak~todac=0.79, while unrelated words sit at <=0.55.  The digit+letter anchors
# corroborate the match, so a fairly low floor stays safe from false positives.
_STEM_SIMILARITY_THRESHOLD = 0.78

# STT homophones for spoken revision digits — "two" is routinely transcribed as
# "to", "four" as "for", "zero" as "oh", etc.  Includes the ICAO radio variants
# (wun, tree, fife, niner, fower) so a SID read "Cindy wun Alfa" matches CINDY1A.
# Used (token-based, so "to" must be a whole word, not the "to" inside "tobacco")
# when checking the SID revision.
_DIGIT_HOMOPHONES: Dict[str, set] = {
    '0': {'0', 'zero', 'oh', 'o'},
    '1': {'1', 'one', 'won', 'wun'},
    '2': {'2', 'two', 'too', 'to'},
    '3': {'3', 'three', 'tree'},
    '4': {'4', 'four', 'for', 'fore', 'fower'},
    '5': {'5', 'five', 'fife'},
    '6': {'6', 'six'},
    '7': {'7', 'seven'},
    '8': {'8', 'eight', 'ate'},
    '9': {'9', 'nine', 'niner'},
}


def _decompose_ident(value: str) -> Optional[Tuple[str, str, str]]:
    """'TOBAK2E' → ('TOBAK', '2', 'E'); None when not SID-shaped."""
    m = re.match(r'^([A-Z]{2,})(\d{1,2})([A-Z])$', value.strip().upper())
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _fuzzy_ident_match(value: str, utterance: str) -> Optional[str]:
    """Lenient match for a word-pronounced SID/STAR.

    Requires the stem (fuzzy), every digit, and the final letter to be present.
    Returns a human-readable description of the match, or None.
    """
    parts = _decompose_ident(value)
    if parts is None:
        return None
    stem, digits, letter = parts

    # Final letter: phonetic word or the bare letter as a standalone token.
    letter_word = _LETTER_PHONETICS.get(letter, re.escape(letter.lower()))
    if not re.search(rf'\b(?:{letter_word}|{re.escape(letter.lower())})\b', utterance, re.IGNORECASE):
        return None

    # Digit(s): each revision digit must appear, in order, as a digit or one of
    # its spoken homophones.  Token-based so "to" (the homophone of two) is only
    # accepted as a whole word, never the "to" inside "tobacco".
    tokens = re.findall(r'[a-z]+|\d', utterance.lower())
    search_from = 0
    for d in digits:
        forms = _DIGIT_HOMOPHONES.get(d, {d})
        found_at = next((i for i in range(search_from, len(tokens)) if tokens[i] in forms), None)
        if found_at is None:
            return None
        search_from = found_at + 1

    # Stem: best spoken word by Metaphone equality or Jaro-Winkler similarity.
    stem_l = stem.lower()
    stem_mp = jellyfish.metaphone(stem_l)
    best_word: Optional[str] = None
    best_score = 0.0
    for word in re.findall(r'[a-z]{3,}', utterance.lower()):
        if word == stem_l:
            best_word, best_score = word, 1.0
            break
        score = jellyfish.jaro_winkler_similarity(word, stem_l)
        if stem_mp and jellyfish.metaphone(word) == stem_mp:
            score = max(score, 0.95)
        if score > best_score:
            best_word, best_score = word, score

    if best_word is None or best_score < _STEM_SIMILARITY_THRESHOLD:
        return None

    return f'fuzzy_sid:{best_word} {digits} {letter.lower()}'


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------

def _match_readback_value(
    expected_str: str,
    utterance: str,
) -> Tuple[bool, Optional[str], List[str]]:
    """Match a single expected value against the utterance.

    Returns ``(matched, matched_via, accepted_forms)``. Shared by scalar and
    list-valued readback fields so they grade identically.
    """
    forms = spoken_forms(expected_str) if expected_str else []
    matched = False
    matched_via: Optional[str] = None

    # 1. Check literal value and all static spoken forms
    for form in forms:
        if form and re.search(re.escape(form), utterance, re.IGNORECASE):
            matched = True
            matched_via = form
            break

    # 1b. Digit-by-digit phonetic, accepting ICAO radio variants
    #     (wun, tree, fife, niner, fower …) which the static spoken forms
    #     above don't include — e.g. QNH 1013 read as "wun zero wun tree",
    #     or frequency 125.350 as "wun too fife decimal tree fife zero"
    #     (the separator optionally swallows a spoken "decimal"/"point").
    if not matched and expected_str:
        digits_only = re.sub(r'\D', '', expected_str)
        # Frequencies are routinely read without the trailing zero: "125.35"
        # for 125.350, "118.7" for 118.700.  Accept the value with trailing
        # zeros of the decimal dropped, keeping at least the 3-digit MHz part.
        # (candidate digits, must_end) — the trailing-zero-dropped form must
        # not be followed by another digit, so a wrong final digit is caught.
        digit_candidates = [(digits_only, False)]
        if re.match(r'^\d{3}\.\d+$', expected_str) and digits_only.endswith('0'):
            trimmed = digits_only.rstrip('0')
            if len(trimmed) >= 3 and trimmed != digits_only:
                digit_candidates.append((trimmed, True))
        for cand, must_end in digit_candidates:
            if len(cand) >= 2:
                seq = _DIGIT_SEQ_SEP.join(_DIGIT_PHONETICS[d] for d in cand)
                if must_end:
                    seq += rf'(?!{_DIGIT_SEQ_SEP}{_ANY_DIGIT_WORD})'
                if re.search(seq, utterance, re.IGNORECASE):
                    matched = True
                    matched_via = "digit_phonetic"
                    break

    # 2. Check ICAO phonetic sequential pattern for identifiers
    if not matched and expected_str and _value_is_icao_ident(expected_str):
        pattern = _icao_identifier_regex(expected_str)
        try:
            if re.search(pattern, utterance, re.IGNORECASE):
                matched = True
                matched_via = "icao_phonetic"
        except re.error:
            pass  # malformed pattern — skip

    # 3. Fuzzy match for word-pronounced SID/STAR names ("Tobak two echo"
    #    transcribed as "Tobacco too Echo").
    if not matched and expected_str:
        fuzzy = _fuzzy_ident_match(expected_str, utterance)
        if fuzzy is not None:
            matched = True
            matched_via = fuzzy

    return matched, matched_via, forms


def evaluate_readback_simple(
    pilot_utterance: str,
    readback_required: List[str],
    variables: Dict[str, Any],
) -> Tuple[bool, List[str], List[Dict[str, Any]]]:
    """
    Returns (passed, missing_fields, reports).

    A field passes if the current variable value is found in the utterance
    as any of:
      1. The literal value (case-insensitive substring match)
      2. Any standard spoken form (frequency, FL, runway, altitude, …)
      3. For ICAO alphanumeric idents: the ICAO phonetic sequential regex
         (e.g. "SULUS5S" → Sierra Uniform Lima Uniform Sierra 5 Sierra)

    ``reports`` is a per-field diagnostic for debugging: what was expected, the
    accepted spoken forms tried, whether it matched, and which form matched the
    utterance (so the comm log can show "expected 25R ← matched 'two five right'").
    """
    missing: List[str] = []
    reports: List[Dict[str, Any]] = []
    utterance = pilot_utterance

    for field in readback_required:
        expected = variables.get(field)

        # List-valued field: every element must be read back. An empty list
        # requires nothing (e.g. crossing_runways == [] when the route crosses
        # no runways), so the field passes trivially.
        if isinstance(expected, list):
            for item in expected:
                item_str = "" if item is None else str(item).strip()
                if not item_str:
                    continue
                matched, matched_via, forms = _match_readback_value(item_str, utterance)
                reports.append({
                    "field": field,
                    "expected": item_str,
                    "matched": matched,
                    "matched_via": matched_via,
                    "accepted_forms": list(dict.fromkeys(forms)),
                })
                if not matched:
                    missing.append(f"{field}:{item_str}")
            continue

        expected_str = "" if expected is None else str(expected).strip()
        matched, matched_via, forms = _match_readback_value(expected_str, utterance)

        report: Dict[str, Any] = {
            "field": field,
            "expected": expected_str,
            "matched": matched,
            "matched_via": matched_via,
            # Distinct accepted forms tried (literal + phonetic variants).
            "accepted_forms": list(dict.fromkeys(forms)),
        }
        if expected is None:
            report["note"] = "variable not set"
        reports.append(report)

        if not matched:
            missing.append(field)

    return (len(missing) == 0), missing, reports


def check_readback(
    pilot_utterance: str,
    readback_required: List[str],
    readback_mode: str,
    variables: Dict[str, Any],
) -> Tuple[bool, List[str], List[Dict[str, Any]]]:
    """
    Dispatch to the appropriate readback evaluator.

    Returns (passed, missing_fields, reports).  ``reports`` is empty when no
    readback is required.
    """
    if readback_mode == "none" or not readback_required:
        return True, [], []

    if readback_mode in ("simple", "strict"):
        # strict is reserved for future stricter matching; uses simple for now
        return evaluate_readback_simple(pilot_utterance, readback_required, variables)

    return True, [], []
