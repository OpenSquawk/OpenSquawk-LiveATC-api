"""Tests for list-valued readback fields (e.g. crossing_runways)."""

from app.readback_evaluator import evaluate_readback_simple


def test_list_field_requires_every_element():
    passed, missing, _ = evaluate_readback_simple(
        "runway two five left, hold short runway zero seven, hold short runway two five right",
        ["runway", "crossing_runways"],
        {"runway": "25L", "crossing_runways": ["07", "25R"]},
    )
    assert passed
    assert missing == []


def test_list_field_fails_when_one_element_missing():
    passed, missing, _ = evaluate_readback_simple(
        "runway two five left, hold short runway zero seven",
        ["runway", "crossing_runways"],
        {"runway": "25L", "crossing_runways": ["07", "33"]},
    )
    assert not passed
    assert any("33" in m for m in missing)


def test_empty_list_field_requires_nothing():
    passed, missing, _ = evaluate_readback_simple(
        "runway two five left",
        ["runway", "crossing_runways"],
        {"runway": "25L", "crossing_runways": []},
    )
    assert passed
    assert missing == []


def test_scalar_field_still_graded():
    passed, _, _ = evaluate_readback_simple(
        "QNH one zero one three",
        ["qnh"],
        {"qnh": "1013"},
    )
    assert passed
