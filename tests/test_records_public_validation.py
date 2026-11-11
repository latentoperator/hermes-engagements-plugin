"""The stage-1 and stage-4 halves of validate_ops are callable on their own.

The writer must run stage 1 before reading the record and the value half only
after the global-defect check (TOOLS.md error precedence), so it cannot use the
combined validate_ops.
"""
import pytest
from engagements.errors import EngagementsError
from engagements.records import (
    is_single_line,
    validate_ops,
    validate_shapes,
    validate_values,
)

GOOD = [{"op": "set_property", "key": "status", "value": "paused"}]
BAD_VALUE = [{"op": "set_property", "key": "status", "value": "historical"}]
BAD_SHAPE = [{"op": "set_property", "key": "status"}]


def test_shapes_only_checks_shape():
    validate_shapes(GOOD)
    validate_shapes(BAD_VALUE)  # a wrong-for-kind value is not a shape error
    with pytest.raises(EngagementsError) as exc:
        validate_shapes(BAD_SHAPE)
    assert exc.value.details["reason"] == "missing_field"


def test_values_only_checks_values_for_the_kind():
    validate_values("client", GOOD, client_exists=lambda _: True)
    with pytest.raises(EngagementsError) as exc:
        validate_values("client", BAD_VALUE, client_exists=lambda _: True)
    assert exc.value.details["reason"] == "status_not_allowed_for_kind"


def test_values_rejects_an_unresolved_kind():
    with pytest.raises(ValueError):
        validate_values("other", GOOD, client_exists=lambda _: True)


def test_the_two_halves_equal_validate_ops():
    for ops in (GOOD, BAD_VALUE, BAD_SHAPE):
        outcomes = []
        for run in (lambda o: validate_ops("client", o, client_exists=lambda _: True),
                    lambda o: (validate_shapes(o), validate_values("client", o, client_exists=lambda _: True))):
            try:
                run(ops)
                outcomes.append(None)
            except EngagementsError as error:
                outcomes.append((error.code, error.details))
        assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("value,maximum,expected", [
    ("one line", 200, True),
    ("", 200, False),
    ("   ", 200, False),
    ("two\nlines", 200, False),
    ("x" * 201, 200, False),
    ("ok" * 100, 200, True),
    ("bad\u2028sep", 200, False),
    (None, 200, False),
])
def test_is_single_line(value, maximum, expected):
    assert is_single_line(value, maximum) is expected
