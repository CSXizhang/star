"""Explicit purchase quantities must survive normalization without coercion."""

import pytest

from stardew_ai_runtime.scheduler import CompanionScheduler, PolicyViolationError


@pytest.mark.parametrize("fields,expected", [
    ({"quantity": 100}, 100), ({"count": 100}, 100),
    ({"quantity": 100, "count": 100}, 100), ({}, 1),
])
def test_purchase_quantity_normalizes_to_native_count(fields, expected):
    assert CompanionScheduler._validate_purchase_items([
        {"item_id": "(O)178", **fields},
    ]) == [{"itemId": "(O)178", "count": expected}]


@pytest.mark.parametrize('field,value', [
    ('quantity', True),
    ('quantity', 1.0),
    ('count', '100'),
    ('quantity', None),
    ('quantity', -1),
])
def test_purchase_quantity_rejects_invalid_explicit_number(field, value):
    with pytest.raises(PolicyViolationError):
        CompanionScheduler._validate_purchase_items([
            {"itemId": "(O)178", field: value},
        ])


def test_purchase_quantity_conflict_rejected():
    with pytest.raises(PolicyViolationError, match="must agree"):
        CompanionScheduler._validate_purchase_items([
            {"itemId": "(O)178", "count": 1, "quantity": 100},
        ])
