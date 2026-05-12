#!/usr/bin/env python3
"""Tests for the learning-quality gate (knowledge_base.learning_has_trigger_condition).

A learning with no `applies_when` trigger condition can never be retrieved on
relevance, so it is treated as non-retrievable: the sweep/extraction paths skip
it, and store_learning() auto-deprecates any that slip through. These tests pin
the predicate that all three call sites share.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import learning_has_trigger_condition


def test_missing_applies_when_is_not_retrievable():
    assert learning_has_trigger_condition({"title": "x", "description": "y"}) is False


def test_none_applies_when_is_not_retrievable():
    assert learning_has_trigger_condition({"applies_when": None}) is False


def test_empty_applies_when_is_not_retrievable():
    assert learning_has_trigger_condition({"applies_when": ""}) is False


def test_whitespace_applies_when_is_not_retrievable():
    assert learning_has_trigger_condition({"applies_when": "   \n\t "}) is False


def test_real_trigger_condition_is_retrievable():
    assert learning_has_trigger_condition(
        {"applies_when": "pod faster-whisper is OOMKilled or memory > 80% of limit"}
    ) is True


def test_non_string_truthy_applies_when_is_retrievable():
    # Defensive: an LLM could hand back a list/dict; anything that stringifies
    # to non-whitespace counts as a (poor but present) trigger condition.
    assert learning_has_trigger_condition({"applies_when": ["cfoperator restart with no k8s event"]}) is True


def test_non_string_empty_applies_when_is_not_retrievable():
    assert learning_has_trigger_condition({"applies_when": []}) is False


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
