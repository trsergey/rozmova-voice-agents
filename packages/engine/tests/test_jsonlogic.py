from typing import Any

import pytest
from pydantic import JsonValue

from rozmova_engine.flow.jsonlogic import JSONLogicError, evaluate, truthy

SLOTS: dict[str, JsonValue] = {
    "account": {"balance_uah": 3.2, "tariff": "start"},
    "services": ["antispam"],
    "attempts": 2,
}


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"var": "attempts"}, 2),
        ({"var": "account.tariff"}, "start"),
        ({"var": "services.0"}, "antispam"),
        ({"var": ["missing", "default"]}, "default"),
        ({"var": "missing"}, None),
        ({"==": [{"var": "account.tariff"}, "start"]}, True),
        ({"!=": [{"var": "attempts"}, 2]}, False),
        ({"<": [{"var": "account.balance_uah"}, 10]}, True),
        ({"<=": [1, {"var": "attempts"}, 3]}, True),
        ({">": [{"var": "attempts"}, 5]}, False),
        ({">=": [{"var": "attempts"}, 2]}, True),
        ({"<": [{"var": "missing"}, 10]}, False),
        ({"in": ["antispam", {"var": "services"}]}, True),
        ({"in": ["music", {"var": "services"}]}, False),
        ({"in": ["art", "start"]}, True),
        ({"!": {"var": "missing"}}, True),
        ({"and": [{"var": "attempts"}, {"==": [1, 1]}]}, True),
        ({"and": [{"var": "missing"}, {"==": [1, 1]}]}, None),
        ({"or": [{"var": "missing"}, {"var": "attempts"}]}, 2),
    ],
)
def test_supported_operators(rule: JsonValue, expected: Any) -> None:
    assert evaluate(rule, SLOTS) == expected


def test_truthiness_follows_jsonlogic() -> None:
    assert not truthy([])
    assert truthy("0")
    assert not truthy(0)
    assert not truthy(None)


@pytest.mark.parametrize("rule", [{"regex": ["a", "b"]}, {"==": [1, 1], "!=": [1, 2]}])
def test_unsupported_rules_raise(rule: JsonValue) -> None:
    with pytest.raises(JSONLogicError):
        evaluate(rule, SLOTS)
