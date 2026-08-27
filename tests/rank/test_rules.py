from __future__ import annotations

import pytest

from nostos.rank.rules import RuleRegistry, Signal


def test_rule_registry_decorator_registers_rule() -> None:
    registry = RuleRegistry()

    @registry.rule("pets.allowed", category="amenities", label="Pet friendly")
    def detect_pets(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=0.9, evidence="Pets allowed")

    registered = registry.get("pets.allowed")
    assert registered is not None
    assert registered.key == "pets.allowed"
    assert registered.category == "amenities"
    assert registered.label == "Pet friendly"
    assert registered.detector(object(), object()).fired is True


def test_rule_registry_rejects_duplicate_keys() -> None:
    registry = RuleRegistry()

    @registry.rule("parking.available", category="amenities")
    def first(_: object, __: object) -> Signal:
        return Signal(fired=True, magnitude=1.0, confidence=1.0)

    with pytest.raises(ValueError, match="already registered"):

        @registry.rule("parking.available", category="amenities")
        def second(_: object, __: object) -> Signal:
            return Signal(fired=False, magnitude=0.0, confidence=1.0)
