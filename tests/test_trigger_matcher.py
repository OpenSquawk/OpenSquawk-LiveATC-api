"""Tests for trigger matching — the core routing logic."""

import pytest

from app.models import Guard, Transition
from app.trigger_matcher import select_transition


def t(to: str, trigger=None, is_emergency=False, condition=None) -> Transition:
    return Transition(to=to, trigger=trigger, is_emergency=is_emergency, condition=condition)


VARS: dict = {"runway": "25R", "callsign": "DLH359"}
FLAGS: dict = {"gates_clear": True, "ready": False}


class TestEmergencyOverride:
    def test_emergency_matched_first(self):
        candidates = [
            t("NORMAL", trigger="ready"),
            t("MAYDAY", trigger="mayday", is_emergency=True),
        ]
        trans, reason = select_transition("mayday mayday mayday", candidates, VARS, FLAGS)
        assert trans.to == "MAYDAY"
        assert reason == "emergency_override"

    def test_emergency_takes_priority_even_if_normal_also_matches(self):
        candidates = [
            t("NORMAL", trigger=".*"),  # would match anything
            t("MAYDAY", trigger="mayday", is_emergency=True),
        ]
        trans, reason = select_transition("mayday mayday", candidates, VARS, FLAGS)
        assert trans.to == "MAYDAY"
        assert reason == "emergency_override"

    def test_non_matching_emergency_does_not_block_normal(self):
        candidates = [
            t("MAYDAY", trigger="mayday", is_emergency=True),
            t("NORMAL", trigger="ready"),
        ]
        trans, reason = select_transition("ready for pushback", candidates, VARS, FLAGS)
        assert trans.to == "NORMAL"
        assert reason == "regex_match"


class TestGuardFiltering:
    def test_guard_fail_excludes_transition(self):
        guard = Guard(type="flag_check", name="ready")  # ready=False in FLAGS
        candidates = [
            t("GUARDED", trigger=".*", condition=guard),
        ]
        trans, reason = select_transition("anything", candidates, VARS, FLAGS)
        assert trans is None
        assert reason == "no_match"

    def test_guard_pass_includes_transition(self):
        guard = Guard(type="flag_check", name="gates_clear")  # True in FLAGS
        candidates = [
            t("GUARDED", trigger="ready", condition=guard),
        ]
        trans, reason = select_transition("ready for pushback", candidates, VARS, FLAGS)
        assert trans.to == "GUARDED"

    def test_no_guard_always_included(self):
        candidates = [t("OPEN", trigger="ready")]
        trans, reason = select_transition("ready", candidates, VARS, FLAGS)
        assert trans.to == "OPEN"


class TestRegexMatching:
    def test_single_match(self):
        candidates = [
            t("A", trigger="ready|pushback"),
            t("B", trigger="abort"),
        ]
        trans, reason = select_transition("ready for pushback", candidates, VARS, FLAGS)
        assert trans.to == "A"
        assert reason == "regex_match"

    def test_case_insensitive(self):
        candidates = [t("A", trigger="READY")]
        trans, reason = select_transition("ready for pushback", candidates, VARS, FLAGS)
        assert trans.to == "A"

    def test_no_match_returns_none(self):
        candidates = [t("A", trigger="abort")]
        trans, reason = select_transition("ready for pushback", candidates, VARS, FLAGS)
        assert trans is None
        assert reason == "no_match"

    def test_ambiguous_returns_first_with_reason(self):
        candidates = [
            t("A", trigger="ready"),
            t("B", trigger="ready"),
        ]
        trans, reason = select_transition("ready", candidates, VARS, FLAGS)
        assert trans.to == "A"
        assert reason == "ambiguous_first"

    def test_auto_transition_not_matched(self):
        # trigger=None means auto-transition — should not be picked by utterance matching
        candidates = [t("AUTO", trigger=None)]
        trans, reason = select_transition("anything", candidates, VARS, FLAGS)
        assert trans is None
        assert reason == "no_match"
